"""
Thin client for the Missive REST API (missiveapp.com).

Intentionally minimal — just the three operations Craig needs:

    verify_webhook(body, signature_header, secret) -> bool
        HMAC-SHA256 check for incoming `POST /webhook/missive/...` requests.

    get_message(message_id, token) -> dict
        Pull the full message body. Missive's outgoing webhooks only include
        a 140-char `preview`, so before we hand the text to the LLM we fetch
        the real thing.

    create_draft(conversation_id, html_body, from_address, from_name,
                 to_fields, token) -> dict
        POST /v1/drafts with `send=false`. The draft is attached to the
        existing conversation so Justin sees it inline in Missive and can
        edit before sending.

Docs referenced:
  https://missiveapp.com/docs/developers/rest-api/endpoints
  https://missiveapp.com/docs/developers/webhooks
"""

from __future__ import annotations

import hmac
import hashlib
from typing import Any

import httpx

MISSIVE_BASE = "https://public.missiveapp.com/v1"
# Missive retries a failed webhook up to 5 times over ~8 minutes and gives up
# the request after 15s — so outbound calls we make FROM the webhook handler
# must leave plenty of budget. We use a generous client-side timeout on the
# REST calls since they run in a BackgroundTask (off the 15s ack path).
_REST_TIMEOUT = httpx.Timeout(20.0, connect=5.0)


# ---------------------------------------------------------------------------
# Inbound: signature verification
# ---------------------------------------------------------------------------


def verify_webhook(body: bytes, signature_header: str, secret: str) -> bool:
    """
    Verify an incoming Missive webhook.

    Missive computes HMAC-SHA256 over the raw request body using the shared
    secret configured on the rule's Webhook action, and sends the hex digest
    in `X-Hook-Signature`. Some installations prefix it with `sha256=` — we
    accept either.

    Use `hmac.compare_digest` so bad-faith callers can't time-attack us.
    """
    if not signature_header or not secret:
        return False
    # Strip optional algorithm prefix (`sha256=...`)
    provided = signature_header.split("=", 1)[1] if "=" in signature_header else signature_header
    provided = provided.strip().lower()

    expected = hmac.new(
        key=secret.encode("utf-8"),
        msg=body,
        digestmod=hashlib.sha256,
    ).hexdigest()

    return hmac.compare_digest(expected, provided)


# ---------------------------------------------------------------------------
# Outbound: REST calls
# ---------------------------------------------------------------------------


def _auth_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


async def get_message(message_id: str, token: str) -> dict[str, Any]:
    """Fetch a full message by ID. Used to pull the real body — webhook
    payloads only ship a truncated preview."""
    async with httpx.AsyncClient(timeout=_REST_TIMEOUT) as client:
        r = await client.get(
            f"{MISSIVE_BASE}/messages/{message_id}",
            headers=_auth_headers(token),
        )
        r.raise_for_status()
        return r.json()


async def create_new_thread_draft(
    *,
    html_body: str,
    from_address: str,
    from_name: str,
    to_fields: list[dict[str, str]],
    token: str,
    subject: str,
    attachments: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """
    Create a draft in a BRAND-NEW Missive conversation (thread).

    Use this when there's no existing thread to reply to — e.g. the
    customer chatted with Craig in the web widget and we want to send
    them an email confirmation with the quote PDF + payment link.

    Per Missive's API, omitting `conversation` from the payload creates
    a new thread keyed off `to_fields` + `from_field`. `send=False` keeps
    it as a draft for Justin to review in his Missive inbox before
    actually firing it to the customer.

    Returns the draft object Missive returns (we persist `drafts.id` on
    the Quote so we don't double-create on a retry).
    """
    payload: dict[str, Any] = {
        "drafts": {
            "subject": subject,
            "body": html_body,
            "from_field": {"address": from_address, "name": from_name},
            "to_fields": to_fields,
            "send": False,
        }
    }
    if attachments:
        payload["drafts"]["attachments"] = attachments

    async with httpx.AsyncClient(timeout=_REST_TIMEOUT) as client:
        r = await client.post(
            f"{MISSIVE_BASE}/drafts",
            headers=_auth_headers(token),
            json=payload,
        )
        if r.status_code >= 400:
            detail = r.text[:1500] if r.text else "<empty response body>"
            raise RuntimeError(
                f"Missive create_new_thread_draft {r.status_code}: {detail}"
            )
        return r.json()


async def create_draft(
    *,
    conversation_id: str,
    html_body: str,
    from_address: str,
    from_name: str,
    to_fields: list[dict[str, str]],
    token: str,
    subject: str | None = None,
    attachments: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """
    Create a DRAFT reply on an existing Missive conversation.

    `send` defaults to `false` server-side — the draft appears in the thread
    for a human to review and send manually. That's exactly the human-in-the-
    loop flow we want for v1.

    `attachments`, if passed, should be a list of
        {"filename": "...", "base64_data": "<b64>", "content_type": "application/pdf"}
    Missive decodes base64 server-side and attaches the file to the draft.
    """
    payload: dict[str, Any] = {
        "drafts": {
            "conversation": conversation_id,
            "body": html_body,
            "from_field": {"address": from_address, "name": from_name},
            "to_fields": to_fields,
            "send": False,
        }
    }
    if subject:
        payload["drafts"]["subject"] = subject
    if attachments:
        payload["drafts"]["attachments"] = attachments

    async with httpx.AsyncClient(timeout=_REST_TIMEOUT) as client:
        r = await client.post(
            f"{MISSIVE_BASE}/drafts",
            headers=_auth_headers(token),
            json=payload,
        )
        if r.status_code >= 400:
            # Missive responds with a JSON body explaining what went wrong —
            # without this context the caller only sees "400 Bad Request"
            # and has no way to fix the mis-shaped payload.
            detail = r.text[:1500] if r.text else "<empty response body>"
            raise RuntimeError(
                f"Missive create_draft {r.status_code}: {detail}"
            )
        return r.json()


# ---------------------------------------------------------------------------
# Webhook payload helpers
# ---------------------------------------------------------------------------


def extract_inbound_email(payload: dict[str, Any]) -> dict[str, Any] | None:
    """
    Pull the inbound-message fields Craig cares about out of a webhook body.

    Returns `None` if the event isn't an inbound email we should reply to
    (e.g. an outbound message Justin just sent, a label change, etc.).

    Shape returned:
        {
            "conversation_id": "...",
            "message_id": "...",
            "from_address": "...",
            "from_name": "...",
            "subject": "...",
            "preview": "...",
        }
    """
    conv = payload.get("conversation") or {}
    # Real Missive webhooks ship the event under `message`; some older docs
    # and rule-type variants (e.g. label_change) use `latest_message`. Accept
    # either so we're robust to both.
    msg = payload.get("message") or payload.get("latest_message") or {}
    if not conv.get("id") or not msg.get("id"):
        return None
    # Only inbound email types. Missive sends `type` like "email" / "sms" /
    # "chat"; outbound vs inbound is inferred from `from_field` identity
    # matching the team's own addresses — but in practice the rule itself
    # should be scoped so we only receive inbound events. We still guard
    # here to avoid self-reply loops when Justin's manual replies fire
    # their own webhooks.
    if msg.get("type") not in ("email", None):
        return None
    from_field = msg.get("from_field") or {}
    return {
        "conversation_id": str(conv["id"]),
        "message_id": str(msg["id"]),
        "from_address": from_field.get("address") or "",
        "from_name": from_field.get("name") or "",
        "subject": conv.get("subject") or "",
        "preview": msg.get("preview") or "",
    }
