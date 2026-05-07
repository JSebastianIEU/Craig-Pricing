"""
v33 — transactional email notifications to the operator (Justin).

Why this exists: when a customer commits to a quote (form submit on
the web widget OR `confirm_order` tool fires on the email channel),
Justin needs to know in his inbox so he can hit Approve in the
dashboard. Before v33 there was no notification — Justin had to
refresh the dashboard or look for a Missive draft. After v33 the
operator side is a single click ("Approve") triggered from a real
email in his inbox.

Channel: Resend (resend.com). Free tier covers our volume (<100/day).
DKIM/SPF set on `notifications.<sender-domain>`. The API key is a
Cloud Run secret (RESEND_API_KEY) mounted as an env var.

Two callable surfaces:

  trigger_approval_notification(db, org_slug, quote_id) -> None
      Called from the customer-side handlers (widget_api +
      _handle_missive_event). Idempotent: bails if the quote already
      has a non-null `notification_sent_at`. Catches its own
      exceptions so the customer flow isn't blocked by an email
      provider hiccup.

  send_quote_ready_for_approval(db, quote, org_slug) -> dict
      The lower-level helper that actually composes + posts the
      email. Returns {ok, message_id, error}. Doesn't raise.
      Useful for tests.
"""

from __future__ import annotations

import datetime as _dt
import html as _html
import logging
import os
from typing import Any, Optional

from sqlalchemy.orm import Session

from db.models import Conversation, Quote


_log = logging.getLogger("craig.notifications")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


_RESEND_API_KEY_ENV = "RESEND_API_KEY"
_DEFAULT_DASHBOARD_BASE = "https://strategos-dashboard.vercel.app"


def _setting(db: Session, key: str, default: str, *, organization_slug: str) -> str:
    """Tiny wrapper around pricing_engine._get_setting to avoid a
    cyclic import at module-load time."""
    from pricing_engine import _get_setting
    val = _get_setting(db, key, default=default, organization_slug=organization_slug)
    return str(val) if val is not None else default


# ---------------------------------------------------------------------------
# Email body composition
# ---------------------------------------------------------------------------


def _format_money(amount: Optional[float]) -> str:
    if amount is None:
        return "—"
    try:
        return f"€{float(amount):.2f}"
    except (TypeError, ValueError):
        return "—"


def _build_subject(quote: Quote) -> str:
    total = _format_money(getattr(quote, "final_price_inc_vat", 0))
    return f"[Just Print] Quote JP-{quote.id:04d} ready — {total} inc VAT"


def _build_dashboard_link(base: str, org_slug: str, quote_id: int) -> str:
    base = (base or _DEFAULT_DASHBOARD_BASE).rstrip("/")
    return (
        f"{base}/dashboard?agent=craig&client={org_slug}"
        f"&module=quotes&focus_quote={quote_id}"
    )


def _last_n_messages(conv: Optional[Conversation], n: int = 3) -> list[dict]:
    if conv is None or not conv.messages:
        return []
    msgs = [m for m in conv.messages if isinstance(m, dict)]
    return msgs[-n:]


def _safe(s: Any, fallback: str = "—") -> str:
    """Render any value as escaped HTML-safe text. None / empty → fallback."""
    if s is None:
        return fallback
    s = str(s).strip()
    return _html.escape(s) if s else fallback


def _build_html_body(
    quote: Quote, conv: Optional[Conversation], dashboard_url: str,
) -> str:
    """Build the HTML body. Inline styles only (lots of email clients
    strip <style>). Mobile-friendly (single column, max-width 600)."""
    customer_name = _safe(conv.customer_name if conv else None, fallback="(unknown)")
    customer_email = _safe(conv.customer_email if conv else None, fallback="(no email)")
    channel = _safe(conv.channel if conv else "?", fallback="?")
    is_company = "Company" if (conv and getattr(conv, "is_company", False)) else "Individual"
    returning = (
        "Returning customer" if (conv and getattr(conv, "is_returning_customer", False))
        else "New customer"
    )
    delivery_method = _safe(conv.delivery_method if conv else None, fallback="—")

    specs = quote.specs or {}
    qty = specs.get("quantity", "—")
    sides = "double-sided" if specs.get("double_sided") else "single-sided"
    finish = _safe(specs.get("finish"), fallback="—")
    product_key = _safe(quote.product_key, fallback="—")

    total_inc = _format_money(quote.final_price_inc_vat)
    total_ex = _format_money(quote.final_price_ex_vat)
    shipping_inc = _format_money(getattr(quote, "shipping_cost_inc_vat", 0))
    artwork_cost = _format_money(getattr(quote, "artwork_cost", 0))

    transcript_html = ""
    last = _last_n_messages(conv, n=3)
    if last:
        bubbles = []
        for m in last:
            role = m.get("role", "?")
            content = _html.escape((m.get("content") or "")[:400])
            bg = "#f1f5f9" if role == "assistant" else "#eaf3ff"
            label = "Craig" if role == "assistant" else (role.upper() if role != "user" else "Customer")
            bubbles.append(
                f'<div style="margin:6px 0;padding:8px 10px;border-radius:8px;'
                f'background:{bg};font-size:13px;line-height:1.5;">'
                f'<div style="font-size:10px;color:#64748b;text-transform:uppercase;'
                f'letter-spacing:0.5px;margin-bottom:4px;">{_html.escape(label)}</div>'
                f'<div style="white-space:pre-wrap;">{content}</div>'
                f'</div>'
            )
        transcript_html = (
            '<h3 style="margin:24px 0 8px;font-size:13px;color:#475569;'
            'text-transform:uppercase;letter-spacing:0.5px;">Last messages</h3>'
            + "".join(bubbles)
        )

    return f"""\
<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>Quote ready</title></head>
<body style="margin:0;padding:24px;background:#f8fafc;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;color:#0f172a;">
<table role="presentation" width="100%" style="max-width:600px;margin:0 auto;background:#ffffff;border-radius:12px;overflow:hidden;border:1px solid #e2e8f0;">
  <tr>
    <td style="padding:24px;">
      <div style="font-size:11px;color:#64748b;text-transform:uppercase;letter-spacing:0.5px;margin-bottom:6px;">
        Just Print · {channel} channel
      </div>
      <h1 style="margin:0 0 12px;font-size:22px;font-weight:700;">
        Quote JP-{quote.id:04d} ready for approval
      </h1>
      <p style="margin:0 0 18px;color:#475569;font-size:14px;line-height:1.5;">
        {customer_name} just committed to a quote. Click below to review the
        details and send them the payment link.
      </p>
      <a href="{dashboard_url}" style="display:inline-block;background:#040f2a;color:#ffffff;text-decoration:none;padding:12px 20px;border-radius:8px;font-weight:600;font-size:15px;margin-bottom:24px;">
        Open in dashboard ↗
      </a>

      <h3 style="margin:20px 0 8px;font-size:13px;color:#475569;text-transform:uppercase;letter-spacing:0.5px;">
        Customer
      </h3>
      <table style="width:100%;font-size:14px;border-collapse:collapse;">
        <tr><td style="padding:4px 0;color:#64748b;width:140px;">Name</td><td>{customer_name}</td></tr>
        <tr><td style="padding:4px 0;color:#64748b;">Email</td><td>{customer_email}</td></tr>
        <tr><td style="padding:4px 0;color:#64748b;">Type</td><td>{is_company} · {returning}</td></tr>
        <tr><td style="padding:4px 0;color:#64748b;">Delivery</td><td>{_html.escape(delivery_method)}</td></tr>
      </table>

      <h3 style="margin:20px 0 8px;font-size:13px;color:#475569;text-transform:uppercase;letter-spacing:0.5px;">
        Quote
      </h3>
      <table style="width:100%;font-size:14px;border-collapse:collapse;">
        <tr><td style="padding:4px 0;color:#64748b;width:140px;">Product</td><td>{_html.escape(str(product_key))}</td></tr>
        <tr><td style="padding:4px 0;color:#64748b;">Quantity</td><td>{_html.escape(str(qty))} · {sides} · {finish}</td></tr>
        <tr><td style="padding:4px 0;color:#64748b;">Goods (ex VAT)</td><td>{total_ex}</td></tr>
        <tr><td style="padding:4px 0;color:#64748b;">Shipping</td><td>{shipping_inc}</td></tr>
        <tr><td style="padding:4px 0;color:#64748b;">Artwork</td><td>{artwork_cost}</td></tr>
        <tr><td style="padding:8px 0 4px;color:#0f172a;font-weight:700;border-top:1px solid #e2e8f0;">Total inc VAT</td><td style="padding:8px 0 4px;font-weight:700;border-top:1px solid #e2e8f0;">{total_inc}</td></tr>
      </table>

      {transcript_html}

      <div style="margin-top:28px;padding-top:16px;border-top:1px solid #e2e8f0;font-size:11px;color:#94a3b8;">
        Sent automatically by Craig · You're getting this because you're the
        operator on Just Print's Craig instance. Reply to this email and
        Justin sees it (Justin is on this thread).
      </div>
    </td>
  </tr>
</table>
</body>
</html>
"""


def _build_text_body(quote: Quote, conv: Optional[Conversation], dashboard_url: str) -> str:
    """Plain-text fallback. Some clients prefer this; Resend sends both."""
    customer_name = (conv.customer_name if conv else None) or "(unknown)"
    customer_email = (conv.customer_email if conv else None) or "(no email)"
    specs = quote.specs or {}
    qty = specs.get("quantity", "?")
    sides = "double-sided" if specs.get("double_sided") else "single-sided"
    finish = specs.get("finish") or "?"
    return (
        f"Quote JP-{quote.id:04d} ready for approval\n"
        f"\n"
        f"Customer:  {customer_name} <{customer_email}>\n"
        f"Channel:   {conv.channel if conv else '?'}\n"
        f"Specs:     {qty} {quote.product_key} · {sides} · {finish}\n"
        f"Total:     {_format_money(quote.final_price_inc_vat)} inc VAT\n"
        f"\n"
        f"Approve in the dashboard:\n{dashboard_url}\n"
    )


# ---------------------------------------------------------------------------
# Resend client
# ---------------------------------------------------------------------------


def send_quote_ready_for_approval(
    db: Session,
    quote: Quote,
    org_slug: str,
    *,
    dashboard_base_url: Optional[str] = None,
) -> dict:
    """Compose + send the 'quote ready' email. Returns
    {ok: bool, message_id: str|None, error: str|None}. Never raises."""

    # Resolve config
    enabled = _setting(db, "notifications_enabled", "true", organization_slug=org_slug).lower() == "true"
    if not enabled:
        return {"ok": False, "message_id": None, "error": "notifications_disabled"}

    api_key = os.environ.get(_RESEND_API_KEY_ENV, "").strip()
    if not api_key:
        return {"ok": False, "message_id": None, "error": "missing_RESEND_API_KEY"}

    sender_addr = _setting(
        db, "notification_sender_address",
        "craig@notifications.strategos-ai.com",
        organization_slug=org_slug,
    )
    sender_name = _setting(
        db, "notification_sender_name",
        "Craig (Just Print)",
        organization_slug=org_slug,
    )
    to_addr = _setting(db, "notification_to_address", "", organization_slug=org_slug)
    if not to_addr:
        return {"ok": False, "message_id": None, "error": "missing_notification_to_address"}

    base = dashboard_base_url or _setting(
        db, "dashboard_base_url", _DEFAULT_DASHBOARD_BASE,
        organization_slug=org_slug,
    )

    conv = (
        db.query(Conversation)
        .filter_by(id=quote.conversation_id)
        .first()
        if quote.conversation_id else None
    )
    dashboard_url = _build_dashboard_link(base, org_slug, quote.id)
    subject = _build_subject(quote)
    html_body = _build_html_body(quote, conv, dashboard_url)
    text_body = _build_text_body(quote, conv, dashboard_url)

    # Send via Resend (lazy-import so the module loads even if the
    # package isn't installed in the dev env)
    try:
        import resend
        resend.api_key = api_key
        params = {
            "from": f"{sender_name} <{sender_addr}>",
            "to": [to_addr],
            "subject": subject,
            "html": html_body,
            "text": text_body,
        }
        result = resend.Emails.send(params)
        msg_id = (result or {}).get("id") if isinstance(result, dict) else None
        _log.info(
            "notifications: sent quote-ready email org=%s quote=%s to=%s id=%s",
            org_slug, quote.id, to_addr, msg_id,
        )
        return {"ok": True, "message_id": msg_id, "error": None}
    except Exception as e:
        msg = f"resend_error: {type(e).__name__}: {str(e)[:200]}"
        _log.warning(
            "notifications: send failed org=%s quote=%s err=%s",
            org_slug, quote.id, msg,
        )
        return {"ok": False, "message_id": None, "error": msg}


# ---------------------------------------------------------------------------
# Idempotent trigger — single source of truth for both channels
# ---------------------------------------------------------------------------


def trigger_approval_notification(
    db: Session,
    org_slug: str,
    quote_id: int,
) -> dict:
    """
    Idempotent. Called from both `_handle_missive_event` (after
    confirm_order) and `submit_customer_info` (web widget form submit).
    Bails if the quote already has `notification_sent_at` set. Persists
    the message id + error on the row for audit. Never raises.

    Returns {ok, skipped, error}.
    """
    quote = db.query(Quote).filter_by(id=quote_id).first()
    if quote is None:
        return {"ok": False, "skipped": False, "error": "quote_not_found"}

    if getattr(quote, "notification_sent_at", None) is not None:
        return {"ok": True, "skipped": True, "error": None}

    result = send_quote_ready_for_approval(db, quote, org_slug)
    if result.get("ok"):
        quote.notification_sent_at = _dt.datetime.utcnow()
        quote.notification_message_id = result.get("message_id")
        quote.notification_last_error = None
        db.commit()
        return {"ok": True, "skipped": False, "error": None}
    else:
        # Persist the error so the dashboard can surface it. Don't
        # block the customer flow — they already have their PDF
        # (web channel) or their auto-sent ack (email channel).
        try:
            quote.notification_last_error = (result.get("error") or "")[:500]
            db.commit()
        except Exception:
            db.rollback()
        return {"ok": False, "skipped": False, "error": result.get("error")}
