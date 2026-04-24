"""
The "create a Stripe Payment Link for a confirmed quote" orchestrator.

Single public entry point: `create_link_for_quote(db, quote, organization_slug)`.

Called from:
  1. `llm/craig_agent.py::_exec_tool` when `confirm_order` fires (auto).
  2. `admin_api.py` from the dashboard "Create payment link" button (manual).

Safety invariants (matching the PrintLogic orchestrator's shape):

  - **Default-disabled.** Unless the tenant's `stripe_enabled` Setting is
    literally `"true"`, we short-circuit with `{ok:False, error:"disabled"}`
    and never hit the network. This is why pasting the secret key alone
    doesn't enable anything — you also have to flip the switch.

  - **Idempotent.** If `quote.stripe_payment_link_id` is already set, we
    return the existing URL instead of creating a duplicate link. Retries
    from the dashboard are always safe.

  - **Never raises.** All errors returned in the result dict and persisted
    to `quote.stripe_last_error`. Caller does commit/rollback.

  - **Key-safe logging.** Audit line contains quote_id, link_id, status,
    http_status — never the api_key or the raw Stripe response.

Schema writes on success:
  - quote.stripe_payment_link_id
  - quote.stripe_payment_link_url
  - quote.stripe_payment_status = "unpaid"   (webhook flips to "paid")
  - quote.stripe_last_error = None           (clears any prior error)

Schema writes on failure:
  - quote.stripe_last_error = short string
  - link id / url remain null
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import json
from typing import Any

import stripe_client
from db.models import Conversation, Quote
from pricing_engine import _get_setting


def _truthy(val: str | None) -> bool:
    if not val:
        return False
    return val.strip().lower() in ("true", "1", "yes", "on")


def _build_description(quote: Quote) -> str:
    """Short line shown on the customer-facing Stripe checkout page.
    Short on purpose — Stripe caps product_data.name at 250 chars."""
    specs = quote.specs or {}
    parts: list[str] = []
    qty = specs.get("quantity")
    if qty:
        parts.append(str(qty))
    if quote.product_key:
        parts.append(quote.product_key.replace("_", " "))
    if specs.get("finish"):
        parts.append(specs["finish"])
    base = " ".join(parts) if parts else f"Quote #{quote.id}"
    return f"Just Print — {base} (ref JP-{quote.id:04d})"


def _customer_email_for(db, quote: Quote) -> str | None:
    """Pull the linked conversation's customer_email for Stripe prefill."""
    if not quote.conversation_id:
        return None
    conv = db.query(Conversation).filter(Conversation.id == quote.conversation_id).first()
    if not conv:
        return None
    return conv.customer_email or None


def _audit(action: str, **fields: Any) -> None:
    """One-line JSON audit log to stdout. Cloud Logging picks it up.
    NEVER includes the api_key or the full Stripe payload."""
    line = {"ts": _dt.datetime.utcnow().isoformat(timespec="seconds"), "component": "stripe", "action": action, **fields}
    print(json.dumps(line), flush=True)


def create_link_for_quote(db, quote: Quote, organization_slug: str) -> dict:
    """
    Create (or return the existing) Stripe Payment Link for a Quote.

    Returns a dict with:
      ok: bool
      url: str|None         — public checkout URL
      link_id: str|None     — Stripe 'plink_...' id
      already_exists: bool  — True when we hit the idempotency guard
      disabled: bool        — True when stripe_enabled != 'true'
      error: str|None
    """
    # ── 1. Read tenant settings ─────────────────────────────────────────
    enabled = _get_setting(db, "stripe_enabled", default="false", organization_slug=organization_slug)
    if not _truthy(enabled):
        return {
            "ok": False, "url": None, "link_id": None,
            "already_exists": False, "disabled": True,
            "error": "disabled",
        }

    api_key = _get_setting(db, "stripe_secret_key", default="", organization_slug=organization_slug)
    currency = _get_setting(db, "stripe_currency", default="eur", organization_slug=organization_slug) or "eur"
    success_url = _get_setting(db, "stripe_success_url", default="", organization_slug=organization_slug) or None

    if not api_key:
        return {
            "ok": False, "url": None, "link_id": None,
            "already_exists": False, "disabled": False,
            "error": "no_api_key",
        }

    # ── 2. Idempotency guard ────────────────────────────────────────────
    if quote.stripe_payment_link_id and quote.stripe_payment_link_url:
        return {
            "ok": True,
            "url": quote.stripe_payment_link_url,
            "link_id": quote.stripe_payment_link_id,
            "already_exists": True,
            "disabled": False,
            "error": None,
        }

    # ── 3. Build payload ────────────────────────────────────────────────
    amount = float(quote.total or quote.final_price_inc_vat or 0.0)
    if amount <= 0:
        return {
            "ok": False, "url": None, "link_id": None,
            "already_exists": False, "disabled": False,
            "error": "zero_amount",
        }

    customer_email = _customer_email_for(db, quote)
    description = _build_description(quote)

    # ── 4. Call Stripe ──────────────────────────────────────────────────
    try:
        result = asyncio.run(stripe_client.create_payment_link(
            api_key=api_key,
            quote_id=quote.id,
            amount_eur=amount,
            product_description=description,
            currency=currency,
            organization_slug=organization_slug,
            customer_email=customer_email,
            success_url=success_url,
        ))
    except Exception as e:
        result = {"ok": False, "link_id": None, "url": None,
                  "error": f"crash:{type(e).__name__}", "raw": None}

    # ── 5. Persist outcome on the Quote row ─────────────────────────────
    if result.get("ok"):
        quote.stripe_payment_link_id = result["link_id"]
        quote.stripe_payment_link_url = result["url"]
        quote.stripe_payment_status = "unpaid"
        quote.stripe_last_error = None
        _audit(
            "create_payment_link",
            quote_id=quote.id,
            org=organization_slug,
            link_id=result["link_id"],
            amount_cents=int(round(amount * 100)),
            currency=currency,
            ok=True,
        )
        return {
            "ok": True,
            "url": result["url"],
            "link_id": result["link_id"],
            "already_exists": False,
            "disabled": False,
            "error": None,
        }

    # Failure path — persist error, keep link fields null.
    quote.stripe_last_error = str(result.get("error") or "unknown")[:500]
    _audit(
        "create_payment_link",
        quote_id=quote.id,
        org=organization_slug,
        ok=False,
        error=quote.stripe_last_error,
    )
    return {
        "ok": False,
        "url": None,
        "link_id": None,
        "already_exists": False,
        "disabled": False,
        "error": quote.stripe_last_error,
    }


# =============================================================================
# WEBHOOK HANDLER
# =============================================================================


def apply_webhook_event(db, event: dict, organization_slug: str) -> dict:
    """
    Apply a parsed Stripe event to our Quote table.

    Called by the admin_api webhook endpoint AFTER the signature has been
    verified. Returns `{handled: bool, quote_id: int|None, status: str|None}`
    for logging. Never raises.

    Handled event types:
      - checkout.session.completed  → stripe_payment_status = "paid"
      - payment_intent.succeeded    → stripe_payment_status = "paid"
      - charge.refunded             → stripe_payment_status = "refunded"
      - payment_intent.payment_failed → stripe_payment_status = "failed"

    Correlation strategy: we stamped `metadata.craig_quote_id` on both
    the PaymentLink and its PaymentIntent when we created the link, so
    every relevant event carries it. If we can't find it, we log and
    return without error — never block Stripe's retry loop.
    """
    etype = event.get("type", "")
    data = (event.get("data") or {}).get("object") or {}
    metadata = data.get("metadata") or {}
    qid_raw = metadata.get("craig_quote_id")

    if not qid_raw:
        return {"handled": False, "quote_id": None, "status": None, "reason": "no_quote_id_in_metadata"}

    try:
        qid = int(qid_raw)
    except (TypeError, ValueError):
        return {"handled": False, "quote_id": None, "status": None, "reason": "bad_quote_id"}

    quote = db.query(Quote).filter(
        Quote.id == qid,
        Quote.organization_slug == organization_slug,
    ).first()
    if not quote:
        # Could be a cross-env event (test webhook hitting prod). Silent skip.
        return {"handled": False, "quote_id": qid, "status": None, "reason": "quote_not_found"}

    new_status: str | None = None
    if etype in ("checkout.session.completed", "payment_intent.succeeded"):
        new_status = "paid"
    elif etype == "charge.refunded":
        new_status = "refunded"
    elif etype == "payment_intent.payment_failed":
        new_status = "failed"
    else:
        return {"handled": False, "quote_id": qid, "status": None, "reason": f"ignored_type:{etype}"}

    # Don't regress a 'paid' back to 'unpaid' if events arrive out of order.
    if quote.stripe_payment_status == "paid" and new_status != "refunded":
        return {"handled": True, "quote_id": qid, "status": "paid", "reason": "already_paid_no_regression"}

    quote.stripe_payment_status = new_status
    if new_status == "paid":
        quote.stripe_paid_at = _dt.datetime.utcnow()
        # Capture the session id if the event carries one — helps debug refunds later.
        if data.get("object") == "checkout.session":
            quote.stripe_checkout_session_id = data.get("id")

    _audit(
        "webhook_apply",
        quote_id=qid,
        org=organization_slug,
        event_type=etype,
        new_status=new_status,
    )

    return {"handled": True, "quote_id": qid, "status": new_status, "reason": etype}
