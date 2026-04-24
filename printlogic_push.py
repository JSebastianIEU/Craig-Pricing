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
from db.models import Conversation, Quote
from pricing_engine import _get_setting


def _truthy(val: str | None) -> bool:
    if not val:
        return False
    return val.strip().lower() in ("true", "1", "yes", "on")


def _build_item_desc(quote: Quote) -> str:
    """Best-effort description of what's being ordered — ends up in
    PrintLogic's `item_desc` field. Short and human-friendly."""
    specs = quote.specs or {}
    parts: list[str] = []
    qty = specs.get("quantity")
    if qty:
        parts.append(str(qty))
    if quote.product_key:
        parts.append(quote.product_key.replace("_", " "))
    if specs.get("finish"):
        parts.append(specs["finish"])
    if specs.get("double_sided"):
        parts.append("DS")
    else:
        parts.append("SS")
    return " ".join(parts) if parts else "Craig quote"


def _build_item_detail(quote: Quote) -> str:
    """Longer detail line — paper weight, format, pages, cover, etc."""
    specs = quote.specs or {}
    bits: list[str] = []
    if quote.product_key and quote.product_key.startswith("flyers_"):
        bits.append("170gsm")
    if quote.product_key == "business_cards":
        bits.append("400gsm silk")
    if specs.get("finish"):
        bits.append(f"{specs['finish']} finish")
    if specs.get("double_sided") is not None:
        bits.append("double-sided" if specs["double_sided"] else "single-sided")
    if specs.get("pages"):
        bits.append(f"{specs['pages']}pp")
    if specs.get("cover_type"):
        bits.append(str(specs["cover_type"]).replace("_", " "))
    if specs.get("binding"):
        bits.append(str(specs["binding"]).replace("_", " "))
    return ", ".join(bits)


def _build_payload(quote: Quote, conv: Conversation | None) -> dict[str, Any]:
    """
    Construct a PrintLogic create_order body from the Quote + Conversation.
    The CRAIG-PUSH marker in `order_description` is deliberate — it lets
    Justin spot Craig-originated orders in his PrintLogic UI and filter
    or clean them up if anything ever goes wrong.
    """
    specs = quote.specs or {}
    qty = int(specs.get("quantity", 1)) if specs.get("quantity") else 1

    cust_name = (getattr(conv, "customer_name", None) or "").strip()
    cust_email = (getattr(conv, "customer_email", None) or "").strip()
    cust_phone = (getattr(conv, "customer_phone", None) or "").strip()

    short = _build_item_desc(quote)
    detail = _build_item_detail(quote)

    # Per PrintLogic spec: item_vat can be "23" / "13.5" etc. (percentage as string)
    # Use the effective rate from the stored quote.
    # We compute it from vat_amount / final_price_ex_vat to avoid a lookup.
    try:
        vat_rate_pct = round(
            (float(quote.vat_amount) / float(quote.final_price_ex_vat or 1)) * 100, 1
        )
    except (TypeError, ZeroDivisionError):
        vat_rate_pct = 23.0

    return {
        "customer_name": cust_name or "Craig customer",
        "customer_email": cust_email,
        "customer_phone": cust_phone,
        "customer_address1": "",
        "customer_address2": "",
        "customer_address3": "",
        "customer_address4": "",
        "customer_postcode": "",
        "order_description": f"[CRAIG-PUSH qid={quote.id}] {short}",
        "contact_name": cust_name,
        "order_po": "",
        "delivery_address1": "",
        "delivery_address2": "",
        "delivery_address3": "",
        "delivery_address4": "",
        "order_items": [
            {
                "item_quantity": str(qty),
                "item_desc": short,
                "item_price": f"{float(quote.final_price_ex_vat or 0):.2f}",
                "item_vat": f"{vat_rate_pct}",
                "item_custom_data": json.dumps({
                    "craig_quote_id": quote.id,
                    "craig_specs": specs,
                }),
                "item_detail": detail,
                "item_code": (quote.product_key or "")[:80],
                "item_part_number": "",
            },
        ],
    }


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
    payload = _build_payload(quote, conv)
    if existing_customer_uid:
        payload["customer_uid"] = existing_customer_uid

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
