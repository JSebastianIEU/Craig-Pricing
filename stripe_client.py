"""
Thin async client for Stripe — Payment Links + webhook signature verification.

Phase B of Craig's order pipeline. When a customer confirms a quote, we
create a Stripe Payment Link so they can pay immediately without needing
a separate invoicing round-trip with Justin.

Design principles (same shape as printlogic.py):

  1. NEVER raise to callers. Every function returns a normalised
     `{"ok": bool, ...}` dict so the orchestrator (`stripe_push.py`) can
     do clean state-machine transitions on `Quote`.

  2. Stripe keys are tenant-scoped. Callers pass `api_key` explicitly
     every call — no module-level state, no cross-tenant leakage risk.

  3. Default-disabled — the `stripe_enabled` Setting defaults to
     "false". If it's false the orchestrator short-circuits before we're
     ever called. This file only talks to the Stripe API; it doesn't
     know or care about the enabled flag.

  4. API-key-in-Authorization-header — Stripe uses HTTP Basic with the
     secret key as the username and blank password. Encoded server-side;
     never logged.

  5. Webhook signature verification is constant-time (hmac.compare_digest)
     and tolerates the 5-minute replay window Stripe recommends. Reject
     anything we can't verify.

Stripe API version pinned to "2024-06-20" in the `Stripe-Version` header so
a future breaking change at Stripe doesn't silently alter our behaviour.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from typing import Any
from urllib.parse import urlencode

import httpx


STRIPE_BASE = "https://api.stripe.com/v1"
STRIPE_API_VERSION = "2024-06-20"
TIMEOUT = httpx.Timeout(15.0, connect=5.0)

# Stripe recommends rejecting webhook events whose timestamp is more than
# 5 minutes old. Protects against replay attacks even if someone snags a
# valid payload + signature off the wire.
WEBHOOK_TOLERANCE_SECONDS = 300


def _auth(api_key: str) -> tuple[str, str]:
    """Stripe uses HTTP Basic with the secret key as username."""
    return (api_key, "")


def _headers() -> dict[str, str]:
    return {
        "Stripe-Version": STRIPE_API_VERSION,
        "Content-Type": "application/x-www-form-urlencoded",
    }


def _flatten(prefix: str, value: Any, out: dict[str, str]) -> None:
    """
    Stripe's API takes form-encoded params with bracket notation for
    nested structures — e.g. `line_items[0][price_data][product_data][name]`.
    This recursively flattens a dict/list into that shape.
    """
    if isinstance(value, dict):
        for k, v in value.items():
            _flatten(f"{prefix}[{k}]", v, out)
    elif isinstance(value, list):
        for i, v in enumerate(value):
            _flatten(f"{prefix}[{i}]", v, out)
    elif isinstance(value, bool):
        out[prefix] = "true" if value else "false"
    elif value is None:
        # Stripe treats missing keys and empty-string-values differently.
        # Skip None to mean "not set".
        return
    else:
        out[prefix] = str(value)


def _encode_form(params: dict[str, Any]) -> str:
    flat: dict[str, str] = {}
    for k, v in params.items():
        _flatten(k, v, flat)
    return urlencode(flat)


# =============================================================================
# PAYMENT LINKS
# =============================================================================


async def create_payment_link(
    *,
    api_key: str,
    quote_id: int,
    amount_eur: float,
    product_description: str,
    currency: str = "eur",
    organization_slug: str = "",
    customer_email: str | None = None,
    success_url: str | None = None,
    account_id: str | None = None,
) -> dict:
    """
    Create a Stripe Payment Link for a confirmed quote.

    Two auth modes, both pass through this single function:

      1. **Connect (default for Connect tenants):** caller passes
         `account_id="acct_xxx"` and `api_key=<platform_key>`. We add a
         `Stripe-Account: acct_xxx` header so Stripe routes the call
         on-behalf-of the connected tenant. Money flows directly to the
         tenant's bank.

      2. **Direct (legacy / test):** `account_id=None`. We use `api_key`
         (the tenant's own `sk_***`) as plain Basic auth. Money flows to
         whoever owns that key. Kept for backwards compat + simple test
         setups, but production is Mode 1.

    Uses the "inline price" form of Payment Links — we pass `price_data`
    directly instead of pre-creating a Price object, because each quote is
    one-off and we don't want to litter the tenant's Stripe dashboard with
    thousands of single-use Price rows.

    `amount_eur` is in major units (euros). Stripe works in minor units
    (cents), so we multiply by 100 and round to int. Rejects amounts that
    are zero/negative before hitting the network.

    Returns:
        {ok: bool, link_id: str|None, url: str|None, error: str|None, raw: dict|None}
    """
    if amount_eur <= 0:
        return {"ok": False, "link_id": None, "url": None, "error": "amount_must_be_positive", "raw": None}
    if not api_key:
        return {"ok": False, "link_id": None, "url": None, "error": "no_api_key", "raw": None}

    amount_cents = int(round(amount_eur * 100))

    # Metadata flows through to webhook events — we use it to correlate
    # payment_intent.succeeded / checkout.session.completed back to our
    # Quote row without a second lookup.
    metadata = {
        "craig_quote_id": str(quote_id),
        "craig_org_slug": organization_slug or "",
    }

    params: dict[str, Any] = {
        "line_items": [
            {
                "quantity": 1,
                "price_data": {
                    "currency": currency.lower(),
                    "unit_amount": amount_cents,
                    "product_data": {
                        "name": (product_description or f"Quote #{quote_id}")[:250],
                    },
                },
            },
        ],
        "metadata": metadata,
        # Surface metadata on the resulting PaymentIntent too — belt + braces.
        "payment_intent_data": {
            "metadata": metadata,
        },
    }
    if customer_email:
        # Pre-fill the checkout form so the customer doesn't retype.
        params["customer_creation"] = "always"

    if success_url:
        params["after_completion"] = {
            "type": "redirect",
            "redirect": {"url": success_url},
        }

    body = _encode_form(params)

    headers = _headers()
    if account_id:
        # Connect mode: act on-behalf-of the tenant. Stripe routes the
        # resulting Payment Link to live in their account, money flows
        # to their bank.
        headers["Stripe-Account"] = account_id

    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            resp = await client.post(
                f"{STRIPE_BASE}/payment_links",
                headers=headers,
                auth=_auth(api_key),
                content=body,
            )
    except httpx.TimeoutException:
        return {"ok": False, "link_id": None, "url": None, "error": "timeout", "raw": None}
    except httpx.HTTPError as e:
        return {"ok": False, "link_id": None, "url": None, "error": f"http:{type(e).__name__}", "raw": None}

    try:
        data = resp.json()
    except Exception:
        data = {"raw_text": resp.text[:500]}

    if resp.status_code >= 400:
        # Stripe returns {"error": {"message": "...", "type": "...", "code": "..."}}
        err = (data.get("error") or {}) if isinstance(data, dict) else {}
        msg = err.get("message") or f"http_{resp.status_code}"
        return {
            "ok": False,
            "link_id": None,
            "url": None,
            "error": msg,
            "raw": data,
        }

    link_id = data.get("id")
    url = data.get("url")
    if not link_id or not url:
        return {
            "ok": False,
            "link_id": None,
            "url": None,
            "error": "malformed_response",
            "raw": data,
        }

    return {"ok": True, "link_id": link_id, "url": url, "error": None, "raw": data}


async def deactivate_payment_link(
    api_key: str,
    link_id: str,
    *,
    account_id: str | None = None,
) -> dict:
    """
    Make a Payment Link no longer accept payments. Stripe's API doesn't
    let you delete Payment Links — you POST `active=false` instead. Used
    for the dashboard "Cancel" action.

    `account_id` follows the same rules as create_payment_link: when set,
    we use the platform key + Stripe-Account header (Connect mode).
    Otherwise plain Basic auth with `api_key` (legacy/test mode).
    """
    if not api_key:
        return {"ok": False, "error": "no_api_key"}
    if not link_id:
        return {"ok": False, "error": "no_link_id"}

    body = _encode_form({"active": False})
    headers = _headers()
    if account_id:
        headers["Stripe-Account"] = account_id

    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            resp = await client.post(
                f"{STRIPE_BASE}/payment_links/{link_id}",
                headers=headers,
                auth=_auth(api_key),
                content=body,
            )
    except httpx.HTTPError as e:
        return {"ok": False, "error": f"http:{type(e).__name__}"}

    if resp.status_code >= 400:
        try:
            data = resp.json()
        except Exception:
            data = {}
        return {"ok": False, "error": (data.get("error") or {}).get("message", f"http_{resp.status_code}")}

    return {"ok": True, "error": None}


# =============================================================================
# WEBHOOK SIGNATURE VERIFICATION
# =============================================================================


class InvalidSignature(Exception):
    """Raised by `verify_webhook_signature` on any verification failure.

    Caught at the endpoint boundary and turned into a 400. Never bubbled
    up further — we don't want stack traces leaking header contents.
    """


def verify_webhook_signature(
    payload: bytes,
    sig_header: str,
    secret: str,
    *,
    tolerance: int = WEBHOOK_TOLERANCE_SECONDS,
    now: float | None = None,
) -> None:
    """
    Verify a Stripe-Signature header against the raw request body.

    Stripe's scheme (v1): header looks like
        `t=1492774577,v1=5257a8...,v1=other_alt_sig`
    where `t` is the unix timestamp of signing and each `v1` is
    `HMAC_SHA256(f"{t}.{payload}", secret)`. Multiple v1 entries can
    appear during key rotation — any one matching wins.

    Raises `InvalidSignature` with a short, non-leaky reason on any failure.
    Returns None on success.

    `tolerance` and `now` are parameterised so tests can pin time without
    monkeypatching the time module.
    """
    if not secret:
        raise InvalidSignature("no_secret_configured")
    if not sig_header:
        raise InvalidSignature("missing_signature_header")

    parts = {}
    v1_sigs: list[str] = []
    for chunk in sig_header.split(","):
        chunk = chunk.strip()
        if not chunk or "=" not in chunk:
            continue
        k, v = chunk.split("=", 1)
        if k == "v1":
            v1_sigs.append(v)
        else:
            parts[k] = v

    ts_raw = parts.get("t")
    if not ts_raw:
        raise InvalidSignature("missing_timestamp")
    try:
        ts = int(ts_raw)
    except ValueError:
        raise InvalidSignature("malformed_timestamp") from None

    current = now if now is not None else time.time()
    if abs(current - ts) > tolerance:
        raise InvalidSignature("timestamp_outside_tolerance")

    if not v1_sigs:
        raise InvalidSignature("missing_v1_signature")

    signed_payload = f"{ts}.".encode("utf-8") + payload
    expected = hmac.new(
        secret.encode("utf-8"),
        signed_payload,
        hashlib.sha256,
    ).hexdigest()

    for candidate in v1_sigs:
        if hmac.compare_digest(expected, candidate):
            return  # success

    raise InvalidSignature("no_matching_v1_signature")
