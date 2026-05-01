"""
Outbound Missive draft creation — Phase C.

Bridges the web-widget customer journey to Missive: when a customer
confirms an order in the chat (`confirm_order` runs), we ALSO push a
draft email to that customer's address with:

  - PDF of the quote (same generator the inbound-email path uses)
  - Stripe payment link (if one was created on the same Quote)
  - A short body so Justin can review + send from his Missive inbox

This lives in Missive as a brand-new thread (the customer never wrote
to info@just-print.ie — they came in via the widget). `send=False`
keeps it as a draft so Justin reviews before the customer sees it,
matching the inbound-email behaviour.

Public entry point:
  send_quote_draft(db, quote, organization_slug) -> dict

Returns a dict shape mirroring printlogic_push / stripe_push:
  {
    "ok":            bool,
    "draft_id":      str | None,    # Missive draft id, persisted on Quote
    "skipped":       bool,          # short-circuit reason
    "skip_reason":   str | None,    # one of: disabled, no_token, no_email,
                                    #         already_drafted, no_from_address
    "error":         str | None,
  }

Safety invariants:
  - Idempotent: if Quote.missive_draft_id is non-null, returns
    `already_drafted` immediately.
  - Never raises. Stripe / PrintLogic hooks must remain isolated from
    Missive failure — confirm_order would otherwise rollback the
    customer-facing reply.
  - Tenant-scoped: every Setting lookup carries organization_slug.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import html as _html
from typing import Any

import missive
from db import parse_artwork_files
from db.models import Conversation, Quote
from pricing_engine import _get_setting


def _setting(db, key: str, default: str, *, organization_slug: str) -> str:
    """Wrapper so the call sites read cleanly."""
    return _get_setting(db, key, default, organization_slug=organization_slug)


def _build_html_body(quote: Quote, conv: Conversation | None) -> str:
    """
    Plain-but-readable HTML body for the Missive draft. Mirrors the
    Craig email tone (short, signed off as Justin) and includes the
    Stripe payment URL inline so the customer can pay in one click.
    """
    name = ""
    if conv and (conv.customer_name or "").strip():
        name = conv.customer_name.strip().split()[0]
    greeting = f"Hi {_html.escape(name)}," if name else "Hi,"

    goods_total = float(quote.final_price_inc_vat or 0)
    shipping_inc = float(getattr(quote, "shipping_cost_inc_vat", 0) or 0)
    total = float(quote.total or (goods_total + shipping_inc))
    pay_url = (quote.stripe_payment_link_url or "").strip()
    ref = f"JP-{quote.id:04d}"

    # Phase F — render delivery vs collection differently in the body
    # so the customer sees what they signed up for (and Justin reviewing
    # the draft sees it too at a glance).
    delivery_method = (getattr(conv, "delivery_method", None) or "").strip().lower()
    delivery_address = getattr(conv, "delivery_address", None) or {}

    parts = [f"<p>{greeting}</p>"]
    parts.append(
        "<p>Thanks for confirming your order with Just Print. The full "
        f"branded quote ({_html.escape(ref)}, total "
        f"&euro;{total:.2f} including VAT) is attached as a PDF for "
        "your records.</p>"
    )

    # Quote breakdown (only if shipping is non-zero — keeps simple
    # collect-from-shop emails clean).
    if shipping_inc > 0:
        parts.append(
            "<p>Breakdown:<br>"
            f"&nbsp;&nbsp;Goods: &euro;{goods_total:.2f}<br>"
            f"&nbsp;&nbsp;Just Print Delivery: &euro;{shipping_inc:.2f}<br>"
            f"&nbsp;&nbsp;<strong>Total: &euro;{total:.2f}</strong>"
            "</p>"
        )

    # Delivery / collection block
    if delivery_method == "delivery" and isinstance(delivery_address, dict):
        addr_lines = ", ".join(
            v.strip()
            for v in (
                delivery_address.get("address1"),
                delivery_address.get("address2"),
                delivery_address.get("address3"),
                delivery_address.get("address4"),
                delivery_address.get("postcode"),
            )
            if v and v.strip()
        )
        if addr_lines:
            parts.append(
                f"<p>Delivery to: {_html.escape(addr_lines)}</p>"
            )
    elif delivery_method == "collect":
        parts.append(
            "<p>You'll be collecting from our shop at "
            "Ballymount Cross Business Park, 7, Ballymount, Dublin, D24 E5NH. "
            "We'll let you know when it's ready to pick up.</p>"
        )

    if pay_url:
        parts.append(
            "<p>You can pay securely here: "
            f'<a href="{_html.escape(pay_url)}">{_html.escape(pay_url)}</a>'
            "</p>"
        )
    parts.append(
        "<p>Turnaround is 3-5 working days from when we have print-ready "
        "artwork. Reply to this email if you need any adjustments, and we&rsquo;ll "
        "get things moving on our side.</p>"
    )
    parts.append("<p>Best,<br>Justin<br>Just Print</p>")
    return "".join(parts)


def _build_subject(quote: Quote) -> str:
    """Subject line for the new Missive thread."""
    return f"Your quote from Just Print — JP-{quote.id:04d}"


def _build_attachments(quote: Quote) -> list[dict[str, str]] | None:
    """
    Build the attachment list for the draft. Returns None if PDF
    generation fails (the draft still goes out, just without the
    attachment — better than no draft at all).

    Phase F: when the customer uploaded their own artwork (via the chat
    widget's [ARTWORK_UPLOAD] flow), we fetch it from Cloud Storage and
    add it as a SECOND attachment so Justin's email goes out with both
    the branded quote PDF and the customer's print-ready file in one
    shot — the customer has everything they need without bouncing back
    and forth.
    """
    attachments: list[dict[str, str]] = []
    try:
        from pdf_generator import generate_quote_pdf
        import base64

        pdf_bytes = generate_quote_pdf(quote)
        attachments.append({
            "filename": f"JustPrint-Quote-JP-{quote.id:04d}.pdf",
            "base64_data": base64.b64encode(pdf_bytes).decode("ascii"),
        })
    except Exception as e:
        print(
            f"[missive_outbound] PDF generation failed for quote {quote.id}: {e}",
            flush=True,
        )
        # Don't return None yet — we still want to try the artwork
        # attachment below. PDF-less draft is better than no draft.

    # ── Customer-uploaded artwork (Phase F + G) ────────────────────
    # Phase G — loop over EACH file in artwork_files (multi-file
    # support). Falls back to the singular column for old quotes
    # that haven't been backfilled. Best-effort per-file: a single
    # bad file doesn't block the rest from being attached.
    artwork_files = parse_artwork_files(getattr(quote, "artwork_files", None))
    if not artwork_files:
        # Backfill case — old quote with only the singular columns set.
        legacy_url = (getattr(quote, "artwork_file_url", None) or "").strip()
        legacy_name = (getattr(quote, "artwork_file_name", None) or "").strip()
        if legacy_url and legacy_name:
            artwork_files = [{
                "url": legacy_url,
                "filename": legacy_name,
                "content_type": "application/octet-stream",
            }]

    import base64 as _b64
    import os as _os

    for entry in artwork_files:
        if not isinstance(entry, dict):
            continue
        url = (entry.get("url") or "").strip()
        name = (entry.get("filename") or "artwork").strip()
        if not url:
            continue
        try:
            if url.startswith("/artwork-local/"):
                local_dir = _os.environ.get(
                    "CRAIG_ARTWORK_LOCAL_DIR", "/tmp/craig-artwork",
                )
                fname = url.rsplit("/", 1)[-1]
                with open(_os.path.join(local_dir, fname), "rb") as f:
                    art_bytes = f.read()
            elif url.startswith("gs://"):
                # Phase G — fetch authenticated via the Cloud Run
                # service account's bucket-level read permission.
                from google.cloud import storage  # type: ignore[import-not-found]
                rest = url[len("gs://"):]
                bucket_name, _, blob_name = rest.partition("/")
                client = storage.Client()
                blob = client.bucket(bucket_name).blob(blob_name)
                art_bytes = blob.download_as_bytes()
            else:
                # Legacy: assume HTTP-fetchable URL. Phase F's signed
                # URLs / public URLs would land here.
                import httpx
                with httpx.Client(timeout=httpx.Timeout(30.0, connect=5.0)) as c:
                    r = c.get(url)
                    r.raise_for_status()
                    art_bytes = r.content

            attachments.append({
                "filename": name,
                "base64_data": _b64.b64encode(art_bytes).decode("ascii"),
            })
            print(
                f"[missive_outbound] artwork attached: quote={quote.id} "
                f"name={name!r} size={len(art_bytes)}",
                flush=True,
            )
        except Exception as e:
            print(
                f"[missive_outbound] artwork attach FAILED for quote "
                f"{quote.id} url={url!r}: {e}",
                flush=True,
            )

    return attachments or None


def send_quote_draft(db, quote: Quote, organization_slug: str) -> dict[str, Any]:
    """
    Create a brand-new Missive draft email to the customer for `quote`.

    See module docstring for the full contract. Short-circuits cleanly
    on every "shouldn't fire" condition rather than half-creating the
    draft, so the audit log clearly shows WHY a draft didn't go out.
    """
    # ── 1. Settings + enablement ─────────────────────────────────────
    enabled = _setting(db, "missive_enabled", "false", organization_slug=organization_slug)
    auto_enabled = _setting(db, "missive_auto_draft_enabled", "true", organization_slug=organization_slug)
    if enabled.strip().lower() != "true" or auto_enabled.strip().lower() != "true":
        return {"ok": False, "skipped": True, "skip_reason": "disabled",
                "draft_id": None, "error": None}

    token = _setting(db, "missive_api_token", "", organization_slug=organization_slug)
    if not token:
        return {"ok": False, "skipped": True, "skip_reason": "no_token",
                "draft_id": None, "error": None}

    from_addr = _setting(db, "missive_from_address", "", organization_slug=organization_slug)
    if not from_addr:
        return {"ok": False, "skipped": True, "skip_reason": "no_from_address",
                "draft_id": None, "error": None}

    from_name = _setting(db, "missive_from_name", "Justin",
                         organization_slug=organization_slug)

    # ── 2. Idempotency ───────────────────────────────────────────────
    existing_draft = (quote.missive_draft_id or "").strip()
    if existing_draft:
        return {"ok": True, "skipped": True, "skip_reason": "already_drafted",
                "draft_id": existing_draft, "error": None}

    # ── 3. Customer email lookup ─────────────────────────────────────
    conv = None
    if quote.conversation_id:
        conv = db.query(Conversation).filter_by(id=quote.conversation_id).first()
    customer_email = (getattr(conv, "customer_email", None) or "").strip()
    if not customer_email:
        return {"ok": False, "skipped": True, "skip_reason": "no_email",
                "draft_id": None, "error": None}
    customer_name = (getattr(conv, "customer_name", None) or customer_email).strip()

    # ── 4. Build draft contents ──────────────────────────────────────
    html_body = _build_html_body(quote, conv)
    subject = _build_subject(quote)
    attachments = _build_attachments(quote)

    # ── 5. Fire ──────────────────────────────────────────────────────
    try:
        result = asyncio.run(missive.create_new_thread_draft(
            html_body=html_body,
            from_address=from_addr,
            from_name=from_name,
            to_fields=[{"address": customer_email, "name": customer_name}],
            token=token,
            subject=subject,
            attachments=attachments,
        ))
    except Exception as e:
        err = f"{type(e).__name__}: {str(e)[:200]}"
        quote.missive_last_error = err
        db.flush()
        # Audit log so Cloud Run logs surface the failure even when the
        # confirm_order reply itself succeeds (Missive is best-effort).
        print(
            f"[missive_outbound] create_new_thread_draft FAILED for quote "
            f"{quote.id} (org={organization_slug}): {err}",
            flush=True,
        )
        return {"ok": False, "skipped": False, "skip_reason": None,
                "draft_id": None, "error": err}

    draft_id = ""
    if isinstance(result, dict):
        # Missive responses are shaped { "drafts": { "id": ..., ... } }
        # in some endpoints and { "id": ... } in others — accept either.
        d = result.get("drafts")
        if isinstance(d, dict):
            draft_id = str(d.get("id") or "")
        if not draft_id:
            draft_id = str(result.get("id") or "")

    quote.missive_draft_id = draft_id or None
    quote.missive_drafted_at = _dt.datetime.utcnow()
    quote.missive_last_error = None
    db.flush()

    print(
        f"[missive_outbound] draft created for quote {quote.id}: "
        f"draft_id={draft_id!r} to={customer_email!r}",
        flush=True,
    )
    return {"ok": True, "skipped": False, "skip_reason": None,
            "draft_id": draft_id or None, "error": None}
