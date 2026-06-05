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


def _parse_recipients(raw: Optional[str]) -> list[str]:
    """Parse a `notification_to_address` setting value into a list of
    recipient email addresses.

    Operators can store either a single address or a comma-separated
    list (e.g. ``"sebastian@strategos-ai.com,justin@just-print.ie"``)
    so a single notification fans out to multiple inboxes. Whitespace
    around each address is stripped; empty entries are dropped.

    Returns ``[]`` on empty/None/whitespace-only input, so the
    callers' existing ``if not to_addr`` check still flags the
    "no recipient configured" failure case uniformly on the parsed list.
    """
    if not raw:
        return []
    return [e.strip() for e in str(raw).split(",") if e.strip()]


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


def _build_dashboard_link(
    base: str, org_slug: str, quote_id: int, *, action: Optional[str] = None,
) -> str:
    """Build the deep link straight to this quote in the dashboard.
    Matches the Strategos route shape: `/c/<clientSlug>/a/<agentSlug>/<section>`.
    `?focus_quote=N` is read by QuotesModule on mount and auto-opens
    the matching row's sidebar.

    v34 — optional `action` param. When `action='manual_price'` is set,
    the dashboard auto-expands the manual-pricing form on the focused
    quote so Justin lands directly on the inputs."""
    base = (base or _DEFAULT_DASHBOARD_BASE).rstrip("/")
    qs = f"?focus_quote={quote_id}"
    if action:
        qs += f"&action={action}"
    return f"{base}/c/{org_slug}/a/craig/quotes{qs}"


def _build_engagement_dashboard_link(
    base: str, org_slug: str, conversation_id: int, *,
    action: Optional[str] = None,
) -> str:
    """v37 — deep link for the engagement-approval flow. Routes to the
    Conversations module with `?pending_engagement=N` (mirror of the
    `?focus_quote=N` pattern). Optional `action='reject'` pre-selects
    the reject confirmation dialog."""
    base = (base or _DEFAULT_DASHBOARD_BASE).rstrip("/")
    qs = f"?pending_engagement={conversation_id}"
    if action:
        qs += f"&action={action}"
    return f"{base}/c/{org_slug}/a/craig/conversations{qs}"


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
    to_addr = _parse_recipients(
        _setting(db, "notification_to_address", "", organization_slug=org_slug)
    )
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
            "to": to_addr,
            "subject": subject,
            "html": html_body,
            "text": text_body,
        }
        result = resend.Emails.send(params)
        msg_id = (result or {}).get("id") if isinstance(result, dict) else None
        # Use print() to match the rest of the codebase — Python's
        # logging module isn't configured under uvicorn here and INFO
        # records get dropped. print() is captured by Cloud Run.
        print(
            f"[notifications] sent quote-ready email org={org_slug} "
            f"quote={quote.id} to={to_addr} id={msg_id}",
            flush=True,
        )
        return {"ok": True, "message_id": msg_id, "error": None}
    except Exception as e:
        msg = f"resend_error: {type(e).__name__}: {str(e)[:200]}"
        print(
            f"[notifications] send FAILED org={org_slug} quote={quote.id} err={msg}",
            flush=True,
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
        print(f"[notifications] trigger: quote_not_found id={quote_id} org={org_slug}", flush=True)
        return {"ok": False, "skipped": False, "error": "quote_not_found"}

    if getattr(quote, "notification_sent_at", None) is not None:
        print(
            f"[notifications] trigger: already sent (idempotent skip) "
            f"quote={quote.id} org={org_slug}",
            flush=True,
        )
        return {"ok": True, "skipped": True, "error": None}

    print(f"[notifications] trigger: firing for quote={quote.id} org={org_slug}", flush=True)
    result = send_quote_ready_for_approval(db, quote, org_slug)
    if result.get("ok"):
        # Persist the timestamp so the dashboard's StageTracker fills
        # in 'awaiting_approval'. Use a defensive try/except — if the
        # commit fails we still want to log it; the email already went
        # out so the operator was notified, but the audit field is
        # missing.
        try:
            quote.notification_sent_at = _dt.datetime.utcnow()
            quote.notification_message_id = result.get("message_id")
            quote.notification_last_error = None
            db.commit()
            print(
                f"[notifications] persisted notification_sent_at "
                f"quote={quote.id} msg_id={result.get('message_id')}",
                flush=True,
            )
        except Exception as e:
            db.rollback()
            print(
                f"[notifications] commit FAILED after send "
                f"quote={quote.id} err={type(e).__name__}: {e}",
                flush=True,
            )
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


# ---------------------------------------------------------------------------
# v34 — manual-review variant
# ---------------------------------------------------------------------------
#
# Distinct surface from the v33 approval-required path because:
#   1. Different subject prefix (configurable via setting
#      `manual_review_notification_subject_prefix`) so Justin can filter.
#   2. Body highlights the *reason* and the missing customer info,
#      not a finished price.
#   3. Deep link includes `action=manual_price` so the dashboard
#      sidebar auto-expands the manual-pricing form.
# ---------------------------------------------------------------------------


def _build_manual_review_subject(quote: Quote, prefix: str) -> str:
    return f"{prefix} Quote JP-{quote.id:04d} — manual pricing required"


def _build_html_body_manual_review(
    quote: Quote, conv: Optional[Conversation], dashboard_url: str,
) -> str:
    """HTML body for the v34 'manual pricing required' email."""
    customer_name = _safe(conv.customer_name if conv else None, fallback="(unknown)")
    customer_email = _safe(conv.customer_email if conv else None, fallback="(no email)")
    channel = _safe(conv.channel if conv else "?", fallback="?")
    delivery_method = _safe(conv.delivery_method if conv else None, fallback="—")

    specs = quote.specs or {}
    qty = specs.get("quantity", "—")
    width_mm = specs.get("width_mm")
    height_mm = specs.get("height_mm")
    area_sqm = specs.get("area_sqm")
    finish = _safe(specs.get("finish"), fallback="—")
    sides = "double-sided" if specs.get("double_sided") else "single-sided"
    product_key = _safe(quote.product_key, fallback="—")
    reason = _safe(getattr(quote, "manual_review_reason", None), fallback="manual review required")

    # Dimension summary — surface the ones the LLM passed (if any).
    dim_parts: list[str] = []
    if width_mm and height_mm:
        dim_parts.append(f"{width_mm} × {height_mm} mm per unit")
    if area_sqm:
        dim_parts.append(f"area: {area_sqm} m²")
    dim_summary = " · ".join(dim_parts) if dim_parts else "(none provided yet — Craig is asking)"

    transcript_html = ""
    last = _last_n_messages(conv, n=4)  # one more than approval — pricing context matters
    if last:
        bubbles = []
        for m in last:
            role = m.get("role", "?")
            content = _html.escape((m.get("content") or "")[:500])
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
            'text-transform:uppercase;letter-spacing:0.5px;">Recent transcript</h3>'
            + "".join(bubbles)
        )

    return f"""\
<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>Manual pricing required</title></head>
<body style="margin:0;padding:24px;background:#fef9f2;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;color:#0f172a;">
<table role="presentation" width="100%" style="max-width:600px;margin:0 auto;background:#ffffff;border-radius:12px;overflow:hidden;border:1px solid #fed7aa;">
  <tr>
    <td style="padding:24px;">
      <div style="font-size:11px;color:#9a3412;text-transform:uppercase;letter-spacing:0.5px;margin-bottom:6px;font-weight:600;">
        Just Print · {channel} channel · NEEDS YOUR EYES
      </div>
      <h1 style="margin:0 0 12px;font-size:22px;font-weight:700;color:#9a3412;">
        Quote JP-{quote.id:04d} — manual pricing required
      </h1>
      <div style="background:#fff7ed;border:1px solid #fed7aa;border-radius:8px;padding:12px 16px;margin:0 0 18px;">
        <div style="font-size:11px;color:#9a3412;text-transform:uppercase;letter-spacing:0.5px;margin-bottom:4px;font-weight:600;">Reason</div>
        <div style="font-size:14px;color:#0f172a;">{_html.escape(reason)}</div>
      </div>
      <p style="margin:0 0 18px;color:#475569;font-size:14px;line-height:1.5;">
        Craig refused to auto-quote this product (its catalog price is
        unreliable for the customer's request). Open the dashboard to
        type a price + a note, then approve as usual — the v33 pipeline
        takes over from there.
      </p>
      <a href="{dashboard_url}" style="display:inline-block;background:#9a3412;color:#ffffff;text-decoration:none;padding:12px 20px;border-radius:8px;font-weight:600;font-size:15px;margin-bottom:24px;">
        Price this quote ↗
      </a>

      <h3 style="margin:20px 0 8px;font-size:13px;color:#475569;text-transform:uppercase;letter-spacing:0.5px;">
        Customer
      </h3>
      <table style="width:100%;font-size:14px;border-collapse:collapse;">
        <tr><td style="padding:4px 0;color:#64748b;width:140px;">Name</td><td>{customer_name}</td></tr>
        <tr><td style="padding:4px 0;color:#64748b;">Email</td><td>{customer_email}</td></tr>
        <tr><td style="padding:4px 0;color:#64748b;">Delivery</td><td>{_html.escape(delivery_method)}</td></tr>
      </table>

      <h3 style="margin:20px 0 8px;font-size:13px;color:#475569;text-transform:uppercase;letter-spacing:0.5px;">
        What the customer asked for
      </h3>
      <table style="width:100%;font-size:14px;border-collapse:collapse;">
        <tr><td style="padding:4px 0;color:#64748b;width:140px;">Product</td><td>{_html.escape(str(product_key))}</td></tr>
        <tr><td style="padding:4px 0;color:#64748b;">Quantity</td><td>{_html.escape(str(qty))}</td></tr>
        <tr><td style="padding:4px 0;color:#64748b;">Sides · finish</td><td>{sides} · {finish}</td></tr>
        <tr><td style="padding:4px 0;color:#64748b;">Dimensions</td><td>{_html.escape(dim_summary)}</td></tr>
      </table>

      {transcript_html}

      <div style="margin-top:28px;padding-top:16px;border-top:1px solid #e2e8f0;font-size:11px;color:#94a3b8;">
        Sent automatically by Craig · You're getting this because a
        product flagged manual_review_required came up in chat. The
        customer has been told 'let me check' and is waiting on your
        price.
      </div>
    </td>
  </tr>
</table>
</body>
</html>
"""


def _build_text_body_manual_review(
    quote: Quote, conv: Optional[Conversation], dashboard_url: str,
) -> str:
    """Plain-text fallback for the manual-review email."""
    customer_name = (conv.customer_name if conv else None) or "(unknown)"
    customer_email = (conv.customer_email if conv else None) or "(no email)"
    specs = quote.specs or {}
    qty = specs.get("quantity", "?")
    reason = getattr(quote, "manual_review_reason", None) or "manual review required"
    return (
        f"Quote JP-{quote.id:04d} — manual pricing required\n"
        f"\n"
        f"Reason:    {reason}\n"
        f"Customer:  {customer_name} <{customer_email}>\n"
        f"Channel:   {conv.channel if conv else '?'}\n"
        f"Asked:     {qty} {quote.product_key}\n"
        f"\n"
        f"Open the dashboard to type a price:\n{dashboard_url}\n"
    )


def send_manual_review_required(
    db: Session,
    quote: Quote,
    org_slug: str,
    *,
    dashboard_base_url: Optional[str] = None,
) -> dict:
    """Compose + send the v34 'manual pricing required' email. Returns
    {ok, message_id, error}. Never raises. Mirrors
    `send_quote_ready_for_approval` but with a different prefix and
    body template."""
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
    to_addr = _parse_recipients(
        _setting(db, "notification_to_address", "", organization_slug=org_slug)
    )
    if not to_addr:
        return {"ok": False, "message_id": None, "error": "missing_notification_to_address"}

    prefix = _setting(
        db, "manual_review_notification_subject_prefix",
        "[Just Print — needs your eyes]",
        organization_slug=org_slug,
    )

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
    dashboard_url = _build_dashboard_link(base, org_slug, quote.id, action="manual_price")
    subject = _build_manual_review_subject(quote, prefix)
    html_body = _build_html_body_manual_review(quote, conv, dashboard_url)
    text_body = _build_text_body_manual_review(quote, conv, dashboard_url)

    try:
        import resend
        resend.api_key = api_key
        params = {
            "from": f"{sender_name} <{sender_addr}>",
            "to": to_addr,
            "subject": subject,
            "html": html_body,
            "text": text_body,
        }
        result = resend.Emails.send(params)
        msg_id = (result or {}).get("id") if isinstance(result, dict) else None
        print(
            f"[notifications] sent manual-review email org={org_slug} "
            f"quote={quote.id} to={to_addr} id={msg_id}",
            flush=True,
        )
        return {"ok": True, "message_id": msg_id, "error": None}
    except Exception as e:
        msg = f"resend_error: {type(e).__name__}: {str(e)[:200]}"
        print(
            f"[notifications] manual-review send FAILED org={org_slug} "
            f"quote={quote.id} err={msg}",
            flush=True,
        )
        return {"ok": False, "message_id": None, "error": msg}


def trigger_manual_review_notification(
    db: Session,
    org_slug: str,
    quote_id: int,
) -> dict:
    """Idempotent v34 trigger. Called by the LLM shell when a tool
    returns `manual_review: true` and a Quote with status='needs_revision'
    is auto-created. Bails if `notification_sent_at` is already set.
    Persists the audit fields like the v33 approval trigger.

    Returns {ok, skipped, error}.
    """
    quote = db.query(Quote).filter_by(id=quote_id).first()
    if quote is None:
        print(
            f"[notifications] manual-review trigger: quote_not_found "
            f"id={quote_id} org={org_slug}",
            flush=True,
        )
        return {"ok": False, "skipped": False, "error": "quote_not_found"}

    if getattr(quote, "notification_sent_at", None) is not None:
        print(
            f"[notifications] manual-review trigger: already sent "
            f"(idempotent skip) quote={quote.id} org={org_slug}",
            flush=True,
        )
        return {"ok": True, "skipped": True, "error": None}

    print(
        f"[notifications] manual-review trigger: firing for "
        f"quote={quote.id} org={org_slug}",
        flush=True,
    )
    result = send_manual_review_required(db, quote, org_slug)
    if result.get("ok"):
        try:
            quote.notification_sent_at = _dt.datetime.utcnow()
            quote.notification_message_id = result.get("message_id")
            quote.notification_last_error = None
            db.commit()
            print(
                f"[notifications] manual-review persisted "
                f"notification_sent_at quote={quote.id} "
                f"msg_id={result.get('message_id')}",
                flush=True,
            )
        except Exception as e:
            db.rollback()
            print(
                f"[notifications] manual-review commit FAILED after send "
                f"quote={quote.id} err={type(e).__name__}: {e}",
                flush=True,
            )
        return {"ok": True, "skipped": False, "error": None}
    else:
        try:
            quote.notification_last_error = (result.get("error") or "")[:500]
            db.commit()
        except Exception:
            db.rollback()
        return {"ok": False, "skipped": False, "error": result.get("error")}


# ---------------------------------------------------------------------------
# v35 — generic admin alerts (sebastian@strategos-ai.com or wherever the
# org's admin_alert_email points). Distinct from the operator (Justin)
# notification surface; this is the agency-side feedback loop.
# Triggers on:
#   - customer-reported issues from the widget
#   - Justin flagging a price wrong in Pricing Verification
#   - Justin commenting on a price row in Pricing Verification
# ---------------------------------------------------------------------------


def send_admin_alert(
    db: Session,
    *,
    org_slug: str,
    kind: str,
    title: str,
    body_html: str,
    body_text: str,
    dashboard_url: Optional[str] = None,
) -> dict:
    """Send a one-off admin-feedback email via Resend. Returns
    {ok, message_id, error}. Never raises — admin alerts are
    best-effort + must not block customer/operator flows.

    `kind` is a short tag included in the subject ("issue_reported",
    "price_flagged_wrong", "price_comment", "other"). The receiving
    inbox can filter by kind if needed.
    """
    enabled = _setting(
        db, "notifications_enabled", "true", organization_slug=org_slug,
    ).lower() == "true"
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
    to_addr = _parse_recipients(_setting(
        db, "admin_alert_email", "sebastian@strategos-ai.com",
        organization_slug=org_slug,
    ))
    if not to_addr:
        return {"ok": False, "message_id": None, "error": "missing_admin_alert_email"}

    prefix = _setting(
        db, "admin_alert_subject_prefix", "[Strategos]",
        organization_slug=org_slug,
    )
    subject = f"{prefix} {title}"

    # Optional dashboard link footer
    if dashboard_url:
        body_html += (
            f'<div style="margin-top:24px;">'
            f'<a href="{_html.escape(dashboard_url)}" '
            f'style="color:#040f2a;text-decoration:underline;">'
            f'Open in dashboard ↗</a></div>'
        )
        body_text += f"\n\nDashboard: {dashboard_url}\n"

    try:
        import resend
        resend.api_key = api_key
        params = {
            "from": f"{sender_name} <{sender_addr}>",
            "to": to_addr,
            "subject": subject,
            "html": body_html,
            "text": body_text,
        }
        result = resend.Emails.send(params)
        msg_id = (result or {}).get("id") if isinstance(result, dict) else None
        print(
            f"[notifications] admin-alert sent kind={kind} org={org_slug} "
            f"to={to_addr} id={msg_id}",
            flush=True,
        )
        return {"ok": True, "message_id": msg_id, "error": None}
    except Exception as e:
        msg = f"resend_error: {type(e).__name__}: {str(e)[:200]}"
        print(
            f"[notifications] admin-alert FAILED kind={kind} org={org_slug} err={msg}",
            flush=True,
        )
        return {"ok": False, "message_id": None, "error": msg}


def send_admin_alert_for_issue(
    db: Session, issue, org_slug: str,
) -> dict:
    """Compose + send the v35 issue-reported email. `issue` is an
    `IssueReport` row. Uses `send_admin_alert` under the hood."""
    cust_name = _safe(issue.customer_name, fallback="(anonymous)")
    cust_email = _safe(issue.customer_email, fallback="(no email provided)")
    channel = _safe(issue.channel, fallback="?")
    msg_safe = _html.escape(issue.message or "")

    base = _setting(
        db, "dashboard_base_url", _DEFAULT_DASHBOARD_BASE,
        organization_slug=org_slug,
    )
    dashboard_url = (
        f"{base.rstrip('/')}/c/{org_slug}/a/craig/conversations"
        + (f"?focus={issue.conversation_id}" if issue.conversation_id else "")
    )

    body_html = (
        f'<div style="font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif;'
        f'max-width:600px;margin:0 auto;color:#0f172a;">'
        f'<h2 style="margin:0 0 12px;color:#9a3412;">Customer reported an issue</h2>'
        f'<p style="margin:0 0 18px;color:#475569;font-size:14px;">'
        f'A customer used the &ldquo;Report an issue&rdquo; link to flag a problem '
        f'with their interaction. The conversation transcript is preserved in '
        f'the dashboard so you can review what went wrong.'
        f'</p>'
        f'<table style="width:100%;font-size:14px;border-collapse:collapse;margin:12px 0;">'
        f'<tr><td style="padding:4px 0;color:#64748b;width:140px;">Customer</td><td>{cust_name}</td></tr>'
        f'<tr><td style="padding:4px 0;color:#64748b;">Email</td><td>{cust_email}</td></tr>'
        f'<tr><td style="padding:4px 0;color:#64748b;">Channel</td><td>{channel}</td></tr>'
        f'<tr><td style="padding:4px 0;color:#64748b;vertical-align:top;">Conversation</td>'
        f'<td>{issue.conversation_id if issue.conversation_id else "(standalone)"}</td></tr>'
        f'</table>'
        f'<h3 style="margin:18px 0 6px;font-size:13px;color:#475569;text-transform:uppercase;'
        f'letter-spacing:0.5px;">Customer message</h3>'
        f'<div style="background:#fff7ed;border:1px solid #fed7aa;border-radius:8px;padding:12px;'
        f'white-space:pre-wrap;font-size:13px;line-height:1.5;">{msg_safe}</div>'
        f'</div>'
    )
    body_text = (
        f"Customer reported an issue\n\n"
        f"Customer:    {cust_name}\n"
        f"Email:       {cust_email}\n"
        f"Channel:     {channel}\n"
        f"Conversation: {issue.conversation_id or '(standalone)'}\n"
        f"\n"
        f"Message:\n{issue.message or ''}\n"
    )

    return send_admin_alert(
        db,
        org_slug=org_slug,
        kind="issue_reported",
        title=f"Customer issue — {cust_name}",
        body_html=body_html,
        body_text=body_text,
        dashboard_url=dashboard_url,
    )


def send_admin_alert_for_price_flag(
    db: Session,
    *,
    org_slug: str,
    product_key: str,
    quantity: int,
    spec_key: str,
    flagged_wrong: bool,
    comment: Optional[str],
    flagged_by: Optional[str],
) -> dict:
    """Send the v35 price-flag/comment email. Fired from the
    PUT /pricing-verification/flag endpoint when Justin marks a row
    wrong or leaves a note."""
    base = _setting(
        db, "dashboard_base_url", _DEFAULT_DASHBOARD_BASE,
        organization_slug=org_slug,
    )
    dashboard_url = f"{base.rstrip('/')}/c/{org_slug}/a/craig/catalog"

    if flagged_wrong:
        kind = "price_flagged_wrong"
        title = f"Price flagged wrong — {product_key} qty {quantity}"
    else:
        kind = "price_comment"
        title = f"Price comment — {product_key} qty {quantity}"

    spec_part = f" · spec=<code>{_html.escape(spec_key)}</code>" if spec_key else ""
    flag_badge = (
        '<span style="display:inline-block;background:#fee2e2;color:#991b1b;'
        'padding:2px 8px;border-radius:4px;font-size:12px;font-weight:600;">'
        'FLAGGED WRONG</span>'
        if flagged_wrong else
        '<span style="display:inline-block;background:#fef3c7;color:#92400e;'
        'padding:2px 8px;border-radius:4px;font-size:12px;font-weight:600;">'
        'COMMENT</span>'
    )

    by_safe = _html.escape(flagged_by or "(unknown)")
    cmt_safe = _html.escape(comment or "(no comment)")

    body_html = (
        f'<div style="font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif;'
        f'max-width:600px;margin:0 auto;color:#0f172a;">'
        f'<h2 style="margin:0 0 12px;">Pricing review note</h2>'
        f'<p style="margin:0 0 18px;color:#475569;font-size:14px;">'
        f'Justin (or another operator) flagged or commented on a row in the '
        f'Pricing Verification table. {flag_badge}'
        f'</p>'
        f'<table style="width:100%;font-size:14px;border-collapse:collapse;margin:12px 0;">'
        f'<tr><td style="padding:4px 0;color:#64748b;width:140px;">Product</td>'
        f'<td><code>{_html.escape(product_key)}</code></td></tr>'
        f'<tr><td style="padding:4px 0;color:#64748b;">Quantity</td><td>{quantity}{spec_part}</td></tr>'
        f'<tr><td style="padding:4px 0;color:#64748b;">By</td><td>{by_safe}</td></tr>'
        f'</table>'
        f'<h3 style="margin:18px 0 6px;font-size:13px;color:#475569;text-transform:uppercase;'
        f'letter-spacing:0.5px;">Comment</h3>'
        f'<div style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:8px;padding:12px;'
        f'white-space:pre-wrap;font-size:13px;line-height:1.5;">{cmt_safe}</div>'
        f'</div>'
    )
    body_text = (
        f"Pricing review note ({'FLAGGED WRONG' if flagged_wrong else 'COMMENT'})\n\n"
        f"Product:  {product_key}\n"
        f"Quantity: {quantity}{(' · spec=' + spec_key) if spec_key else ''}\n"
        f"By:       {flagged_by or '(unknown)'}\n"
        f"\n"
        f"Comment:\n{comment or '(no comment)'}\n"
    )

    return send_admin_alert(
        db,
        org_slug=org_slug,
        kind=kind,
        title=title,
        body_html=body_html,
        body_text=body_text,
        dashboard_url=dashboard_url,
    )


# ---------------------------------------------------------------------------
# v37 — engagement-approval gate
# ---------------------------------------------------------------------------
#
# Why distinct from quote approval: this fires BEFORE Craig has even
# touched the conversation. The classifier returned a confidence below
# the per-tenant `engagement_confidence_threshold` setting (default
# 0.85), so we don't yet have a Quote — only a Conversation in
# `pending_engagement_approval` status with the inbound email body
# preview + the classifier verdict cached in
# `Conversation.engagement_classification` (JSON).
#
# Justin clicks one of two buttons:
#   - Approve  → admin endpoint flips status to engagement_approved,
#                replays the deferred Craig run + posts the Missive draft
#   - Reject   → admin endpoint flips status to engagement_rejected,
#                Craig stays silent on this thread forever
# ---------------------------------------------------------------------------


def _build_engagement_subject(classification: dict) -> str:
    """Subject for the engagement-approval email. Confidence shown as
    a percentage so Justin can eyeball the urgency at a glance."""
    conf = float(classification.get("confidence", 0.0) or 0.0)
    pct = int(round(conf * 100))
    subj = (classification.get("subject") or "").strip() or "(no subject)"
    return f"[Just Print] Should Craig respond? {pct}% — {subj[:80]}"


def _build_html_body_engagement(
    conv: Conversation,
    approve_url: str,
    reject_url: str,
) -> str:
    """v37 — HTML body for the 'should Craig respond?' email. Mirrors
    the visual language of the manual-review template (amber-tinted
    'needs your eyes' style) but with two action buttons."""
    classification = conv.engagement_classification or {}
    from_addr = _safe(classification.get("from"), fallback="(unknown sender)")
    subject_line = _safe(classification.get("subject"), fallback="(no subject)")
    body_preview = _safe(classification.get("body_preview"), fallback="(no body)")
    reason = _safe(classification.get("reason"), fallback="(classifier gave no reason)")
    confidence = float(classification.get("confidence", 0.0) or 0.0)
    pct = int(round(confidence * 100))

    # Cap preview at ~1500 chars in the email body — long enough for
    # context, short enough to scroll comfortably on mobile.
    preview_safe = _html.escape(body_preview)[:1500]

    # v37.1 — pre-rendered Craig reply. Justin reads what Craig WOULD
    # send and decides Approve vs Don't engage with full context. The
    # approve endpoint ships this exact text — no drift between preview
    # and what the customer sees.
    proposed_reply = (classification.get("proposed_reply") or "").strip()
    proposed_quote_id = classification.get("proposed_quote_id")
    proposed_block_html = ""
    if proposed_reply:
        # Render line breaks visually but escape HTML.
        body_lines = _html.escape(proposed_reply).replace("\n", "<br>")
        quote_chip = ""
        if proposed_quote_id:
            quote_chip = (
                f'<span style="display:inline-block;background:#fef3c7;'
                f'color:#92400e;padding:2px 8px;border-radius:9999px;'
                f'font-size:11px;font-weight:600;margin-left:8px;">'
                f'Quote JP-{int(proposed_quote_id):04d} attached</span>'
            )
        proposed_block_html = (
            '<h3 style="margin:24px 0 8px;font-size:13px;color:#475569;'
            'text-transform:uppercase;letter-spacing:0.5px;">'
            f'Craig\'s proposed reply (NOT sent yet){quote_chip}'
            '</h3>'
            '<div style="padding:14px 16px;background:#f0fdf4;border:1px solid #bbf7d0;'
            'border-radius:8px;font-size:13px;line-height:1.55;color:#0f172a;">'
            f'{body_lines}'
            '</div>'
            '<div style="margin-top:6px;font-size:11px;color:#64748b;font-style:italic;">'
            'Approve below to send this exact reply to the customer. '
            "Don’t engage drops it; Craig writes nothing."
            '</div>'
        )

    return f"""\
<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>Should Craig respond?</title></head>
<body style="margin:0;padding:24px;background:#fefce8;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;color:#0f172a;">
<table role="presentation" width="100%" style="max-width:600px;margin:0 auto;background:#ffffff;border-radius:12px;overflow:hidden;border:1px solid #fde68a;">
  <tr>
    <td style="padding:24px;">
      <div style="font-size:11px;color:#92400e;text-transform:uppercase;letter-spacing:0.5px;margin-bottom:6px;font-weight:600;">
        Just Print · Missive · NEW INBOUND — UNCERTAIN
      </div>
      <h1 style="margin:0 0 12px;font-size:22px;font-weight:700;color:#92400e;">
        Should Craig respond to this email?
      </h1>
      <div style="background:#fef9c3;border:1px solid #fde68a;border-radius:8px;padding:12px 16px;margin:0 0 18px;">
        <div style="font-size:11px;color:#92400e;text-transform:uppercase;letter-spacing:0.5px;margin-bottom:4px;font-weight:600;">Classifier verdict</div>
        <div style="font-size:14px;color:#0f172a;">
          Confidence <strong>{pct}%</strong> — below the {int(round(confidence * 100)) if False else '85'}% auto-respond threshold.<br/>
          <span style="color:#475569;">Reason: {_html.escape(reason)}</span>
        </div>
      </div>
      <p style="margin:0 0 18px;color:#475569;font-size:14px;line-height:1.5;">
        Craig isn't sure this is a real quote request. He's drafted a
        reply but hasn't sent it. Read his draft below — Approve sends
        it as-is, Don't engage drops it and Craig stays silent.
      </p>

      <table style="width:100%;border-collapse:collapse;margin:0 0 24px;">
        <tr>
          <td style="padding-right:8px;">
            <a href="{approve_url}" style="display:block;background:#15803d;color:#ffffff;text-decoration:none;padding:12px 20px;border-radius:8px;font-weight:600;font-size:15px;text-align:center;">
              ✓ Approve — let Craig respond
            </a>
          </td>
          <td style="padding-left:8px;">
            <a href="{reject_url}" style="display:block;background:#475569;color:#ffffff;text-decoration:none;padding:12px 20px;border-radius:8px;font-weight:600;font-size:15px;text-align:center;">
              ✗ Don't engage
            </a>
          </td>
        </tr>
      </table>

      <h3 style="margin:20px 0 8px;font-size:13px;color:#475569;text-transform:uppercase;letter-spacing:0.5px;">
        Inbound email
      </h3>
      <table style="width:100%;font-size:14px;border-collapse:collapse;">
        <tr><td style="padding:4px 0;color:#64748b;width:90px;">From</td><td>{from_addr}</td></tr>
        <tr><td style="padding:4px 0;color:#64748b;">Subject</td><td>{subject_line}</td></tr>
      </table>
      <div style="margin-top:12px;padding:12px 14px;background:#f8fafc;border:1px solid #e2e8f0;border-radius:8px;font-size:13px;line-height:1.55;white-space:pre-wrap;color:#0f172a;">{preview_safe}</div>

      {proposed_block_html}

      <div style="margin-top:28px;padding-top:16px;border-top:1px solid #e2e8f0;font-size:11px;color:#94a3b8;">
        Sent automatically by Craig · You're getting this because the
        triage classifier gave a low confidence score. Tune the threshold
        in dashboard → Settings → engagement_confidence_threshold.
      </div>
    </td>
  </tr>
</table>
</body>
</html>
"""


def _build_text_body_engagement(
    conv: Conversation, approve_url: str, reject_url: str,
) -> str:
    """Plain-text fallback for the engagement-approval email."""
    c = conv.engagement_classification or {}
    pct = int(round(float(c.get("confidence", 0.0) or 0.0) * 100))
    proposed = (c.get("proposed_reply") or "").strip()
    proposed_block = ""
    if proposed:
        quote_id = c.get("proposed_quote_id")
        head = "--- Craig's proposed reply (NOT sent yet) ---"
        if quote_id:
            head += f" [Quote JP-{int(quote_id):04d} attached]"
        proposed_block = (
            f"\n{head}\n{proposed}\n--- end ---\n"
        )
    return (
        f"Should Craig respond to this email? (confidence {pct}%)\n"
        f"\n"
        f"From:    {c.get('from') or '(unknown)'}\n"
        f"Subject: {c.get('subject') or '(no subject)'}\n"
        f"Reason:  {c.get('reason') or '(none)'}\n"
        f"\n"
        f"--- inbound body preview ---\n"
        f"{(c.get('body_preview') or '')[:1500]}\n"
        f"--- end ---\n"
        f"{proposed_block}"
        f"\n"
        f"Approve (sends Craig's reply above):\n{approve_url}\n"
        f"\n"
        f"Don't engage (Craig stays silent):\n{reject_url}\n"
    )


def send_engagement_ready_for_approval(
    db: Session,
    conv: Conversation,
    org_slug: str,
    *,
    dashboard_base_url: Optional[str] = None,
) -> dict:
    """Compose + send the v37 'should Craig respond?' email. Returns
    {ok, message_id, error}. Never raises."""
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
    to_addr = _parse_recipients(
        _setting(db, "notification_to_address", "", organization_slug=org_slug)
    )
    if not to_addr:
        return {"ok": False, "message_id": None, "error": "missing_notification_to_address"}

    base = dashboard_base_url or _setting(
        db, "dashboard_base_url", _DEFAULT_DASHBOARD_BASE,
        organization_slug=org_slug,
    )

    approve_url = _build_engagement_dashboard_link(base, org_slug, conv.id)
    reject_url = _build_engagement_dashboard_link(base, org_slug, conv.id, action="reject")

    classification = conv.engagement_classification or {}
    subject = _build_engagement_subject(classification)
    html_body = _build_html_body_engagement(conv, approve_url, reject_url)
    text_body = _build_text_body_engagement(conv, approve_url, reject_url)

    try:
        import resend
        resend.api_key = api_key
        params = {
            "from": f"{sender_name} <{sender_addr}>",
            "to": to_addr,
            "subject": subject,
            "html": html_body,
            "text": text_body,
        }
        result = resend.Emails.send(params)
        msg_id = (result or {}).get("id") if isinstance(result, dict) else None
        print(
            f"[notifications] sent engagement-approval email org={org_slug} "
            f"conv={conv.id} to={to_addr} id={msg_id}",
            flush=True,
        )
        return {"ok": True, "message_id": msg_id, "error": None}
    except Exception as e:
        msg = f"resend_error: {type(e).__name__}: {str(e)[:200]}"
        print(
            f"[notifications] engagement-approval send FAILED org={org_slug} "
            f"conv={conv.id} err={msg}",
            flush=True,
        )
        return {"ok": False, "message_id": None, "error": msg}


def trigger_engagement_approval_notification(
    db: Session,
    org_slug: str,
    conversation_id: int,
) -> dict:
    """v37 — idempotent trigger for the engagement-approval email.
    Called from the Missive webhook when classifier confidence is
    above the junk floor but below the auto-respond threshold.

    Idempotency is gated on
    `Conversation.engagement_classification.notification_sent_at`
    (kept inside the JSON blob so we don't have to ALTER TABLE every
    time a new audit field appears).

    Returns {ok, skipped, error}.
    """
    conv = db.query(Conversation).filter_by(id=conversation_id).first()
    if conv is None:
        print(
            f"[notifications] engagement trigger: conversation_not_found "
            f"id={conversation_id} org={org_slug}",
            flush=True,
        )
        return {"ok": False, "skipped": False, "error": "conversation_not_found"}

    classification = dict(conv.engagement_classification or {})
    if classification.get("notification_sent_at"):
        print(
            f"[notifications] engagement trigger: already sent (idempotent skip) "
            f"conv={conv.id} org={org_slug}",
            flush=True,
        )
        return {"ok": True, "skipped": True, "error": None}

    print(
        f"[notifications] engagement trigger: firing for conv={conv.id} "
        f"org={org_slug}",
        flush=True,
    )
    result = send_engagement_ready_for_approval(db, conv, org_slug)
    if result.get("ok"):
        try:
            classification["notification_sent_at"] = _dt.datetime.utcnow().isoformat(timespec="seconds")
            classification["notification_message_id"] = result.get("message_id")
            classification["notification_last_error"] = None
            conv.engagement_classification = classification
            db.commit()
            print(
                f"[notifications] engagement persisted notification_sent_at "
                f"conv={conv.id} msg_id={result.get('message_id')}",
                flush=True,
            )
        except Exception as e:
            db.rollback()
            print(
                f"[notifications] engagement commit FAILED after send "
                f"conv={conv.id} err={type(e).__name__}: {e}",
                flush=True,
            )
        return {"ok": True, "skipped": False, "error": None}
    else:
        try:
            classification["notification_last_error"] = (result.get("error") or "")[:500]
            conv.engagement_classification = classification
            db.commit()
        except Exception:
            db.rollback()
        return {"ok": False, "skipped": False, "error": result.get("error")}
