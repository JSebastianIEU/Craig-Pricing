"""
The "push a Craig quote to PrintLogic" orchestrator.

Composes:
  - tenant settings lookup (api_key, dry_run flag) from pricing_engine._get_setting
  - customer dedup via printlogic.find_customer
  - payload construction from Quote + linked Conversation
  - printlogic.create_order with dry_run honored
  - persistence of the returned order_id / error back onto the Quote row
  - an audit log line on every real (non-dry) call

The single public entry point is `push_quote(db, quote, organization_slug)`.
It is called from two places:
  1. `llm/craig_agent.py::_exec_tool` when confirm_order fires
  2. `admin_api.py` from the dashboard "Push to PrintLogic" button

Safety invariants:
  - Idempotent: if the Quote already has a real (non-DRY) order_id, do
    nothing and return the existing id.
  - Never raises: all errors are captured and returned in the dict.
  - Writes to the Quote row happen in the caller's session — caller is
    responsible for commit/rollback.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import json
from typing import Any

import printlogic
import printlogic_payload
from db.models import Conversation, Quote
from pricing_engine import _get_setting


def _truthy(val: str | None) -> bool:
    if not val:
        return False
    return val.strip().lower() in ("true", "1", "yes", "on")


# Backwards-compat shim — these helpers used to live here. Tests + any
# external imports should now use `printlogic_payload` directly. We keep
# `_build_payload` as a thin alias so existing call sites keep working.


def _build_payload(quote: Quote, conv: Conversation | None) -> dict[str, Any]:
    """
    Construct a PrintLogic create_order body from the Quote + Conversation.

    Delegates to `printlogic_payload.build_payload_from_quote` which sets
    the rich per-item fields PrintLogic supports (width_mm/height_mm,
    finished_size_text, pages, colors, paper_description,
    finishing_description) plus the order-level `contact_*` fields and
    `order_date_due` that we used to leave blank.
    """
    return printlogic_payload.build_payload_from_quote(quote, conv)


def push_quote(db, quote: Quote, organization_slug: str) -> dict[str, Any]:
    """
    Push a Quote to PrintLogic. Returns a structured dict so callers can
    surface state to the dashboard / LLM / logs.

    Return shape:
        {
            "ok": bool,                      # push succeeded (incl. dry-run)
            "dry_run": bool,                 # was this a simulated call?
            "order_id": str | None,          # real or DRY-xxxx
            "customer_id": str | None,
            "ambiguous": bool,               # 200 OK but no order_id
            "error": str | None,             # error class if not ok
            "already_pushed": bool,          # idempotency short-circuit
        }
    """
    # ── 1. Settings lookup ───────────────────────────────────────────
    api_key = _get_setting(db, "printlogic_api_key", default="", organization_slug=organization_slug)
    dry_run_setting = _get_setting(db, "printlogic_dry_run", default="true", organization_slug=organization_slug)
    dry_run = _truthy(dry_run_setting)

    # Even with an empty api_key, dry_run still works (synthetic id only).
    if not api_key and not dry_run:
        return {
            "ok": False, "dry_run": False, "order_id": None, "customer_id": None,
            "ambiguous": False, "error": "no_api_key", "already_pushed": False,
        }

    # ── 2. Idempotency ───────────────────────────────────────────────
    existing = (quote.printlogic_order_id or "").strip()
    if existing and not existing.startswith("DRY-"):
        # Already pushed for real — never duplicate.
        return {
            "ok": True, "dry_run": False, "order_id": existing,
            "customer_id": quote.printlogic_customer_id,
            "ambiguous": False, "error": None, "already_pushed": True,
        }
    # Dry-run id → allow overwrite (this is how we "promote" DRY to real)

    # ── 3. Load conversation for customer info ───────────────────────
    conv = None
    if quote.conversation_id:
        conv = db.query(Conversation).filter_by(id=quote.conversation_id).first()

    # ── 4. Customer dedup lookup (optional — silently skip on any error) ─
    existing_customer_uid: str | None = None
    if not dry_run and conv and (conv.customer_email or conv.customer_phone):
        try:
            lookup = asyncio.run(printlogic.find_customer(
                api_key,
                email=(conv.customer_email or None),
                phone=(conv.customer_phone or None),
            ))
            if lookup.get("ok") and lookup.get("customer"):
                c = lookup["customer"]
                existing_customer_uid = str(
                    c.get("customer_uid") or c.get("customer_id") or c.get("id") or ""
                ) or None
        except Exception as e:
            print(f"[printlogic_push] find_customer failed (non-fatal): {e}", flush=True)

    # ── 5. Build payload ─────────────────────────────────────────────
    payload = printlogic_payload.build_payload_from_quote(
        quote, conv,
        customer_uid=existing_customer_uid or "",
    )

    # ── 6. Fire create_order (or simulate) ───────────────────────────
    result = asyncio.run(printlogic.create_order(
        payload, api_key, dry_run=dry_run, quote_id_for_dry=quote.id,
    ))

    # ── 7. Persist outcome on the Quote row ──────────────────────────
    quote.printlogic_push_attempts = (quote.printlogic_push_attempts or 0) + 1

    if result.get("ok"):
        quote.printlogic_order_id = result["order_id"]
        if result.get("customer_id"):
            quote.printlogic_customer_id = result["customer_id"]
        quote.printlogic_pushed_at = _dt.datetime.utcnow()
        quote.printlogic_last_error = None
    else:
        # Preserve the DRY-* id if we had one (a failed real push doesn't
        # invalidate the dry-run record). Store the error either way.
        quote.printlogic_last_error = result.get("error")

    db.flush()

    # ── 8. Audit log (real calls only — dry-run already logged itself) ─
    if not dry_run:
        print(json.dumps({
            "event": "printlogic_push",
            "ts": _dt.datetime.utcnow().isoformat(timespec="seconds"),
            "org": organization_slug,
            "quote_id": quote.id,
            "order_id": result.get("order_id"),
            "ok": result.get("ok"),
            "ambiguous": result.get("ambiguous"),
            "error": result.get("error"),
            "attempts": quote.printlogic_push_attempts,
        }), flush=True)

    return {
        "ok": bool(result.get("ok")),
        "dry_run": bool(result.get("dry_run")),
        "order_id": result.get("order_id"),
        "customer_id": result.get("customer_id"),
        "ambiguous": bool(result.get("ambiguous")),
        "error": result.get("error"),
        "already_pushed": False,
    }


def cancel_pushed_order(db, quote: Quote, organization_slug: str) -> dict[str, Any]:
    """
    Rollback path — mark an order as Cancelled in PrintLogic when we
    pushed it by mistake. Only works on REAL order ids (skipping DRY-*).
    """
    api_key = _get_setting(db, "printlogic_api_key", default="", organization_slug=organization_slug)
    order_id = (quote.printlogic_order_id or "").strip()
    if not order_id:
        return {"ok": False, "error": "no_order_id"}
    if order_id.startswith("DRY-"):
        # Clearing a dry-run id is purely a local op — nothing to cancel upstream
        quote.printlogic_order_id = None
        db.flush()
        return {"ok": True, "error": None, "note": "cleared_dry_run_id_only"}
    if not api_key:
        return {"ok": False, "error": "no_api_key"}

    result = asyncio.run(printlogic.update_order_status(order_id, "Cancelled", api_key))
    # Audit log
    print(json.dumps({
        "event": "printlogic_cancel",
        "ts": _dt.datetime.utcnow().isoformat(timespec="seconds"),
        "org": organization_slug,
        "quote_id": quote.id,
        "order_id": order_id,
        "ok": result.get("ok"),
        "error": result.get("error"),
    }), flush=True)
    return result
