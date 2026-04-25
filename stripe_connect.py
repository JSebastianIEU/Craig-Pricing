"""
Stripe Connect — OAuth flow + platform credential management.

Why this module exists: lets tenants connect their Stripe account via a
single "Connect with Stripe" button instead of pasting `sk_live_***` and
`whsec_***` into our dashboard. Two material wins:

  1. **Zero secret custody.** We never see, store, or transmit the
     tenant's secret API key. Stripe issues us OAuth tokens scoped to
     their account; we mostly use those by passing `Stripe-Account: acct_xxx`
     headers along with our own platform key.

  2. **Standard SaaS UX.** Same flow Shopify, Substack, Lemon Squeezy
     use — tenants recognize it and trust it.

## Platform credentials (env vars, NOT per-tenant)

  - `STRATEGOS_STRIPE_PLATFORM_KEY`          — sk_live_strategos_*** (or sk_test_***)
  - `STRATEGOS_STRIPE_CONNECT_CLIENT_ID`     — ca_*** (the OAuth app id from Stripe)
  - `STRATEGOS_STRIPE_CONNECT_WEBHOOK_SECRET` — whsec_*** (platform-mode webhook signing secret)

These come from Roi's Stripe Connect platform setup. Cloud Run mounts
them from Secret Manager (same pattern as `CRAIG_SECRETS_KEY`).

## State signing (CSRF protection)

OAuth's `state` query param round-trips between us, Stripe, and back. To
make sure we initiated the flow (not an attacker), we HMAC-sign the
state with `STRATEGOS_JWT_SECRET` before sending and verify on callback.

The state encodes:
  - `org`     — tenant slug, so the callback knows which row to update
  - `exp`     — unix timestamp; reject if past (5-min TTL)
  - `n`       — random nonce, ensures every link is single-use-ish

Wire format: `<base64url(json(payload))>.<hex(hmac_sha256(payload, secret))>`

## Failure modes (what callers see)

Every public function returns `{ok: bool, ...}` dicts — never raises to
the caller (matches the rest of our HTTP-client modules). The only
exception is `verify_state()` which raises `InvalidState` because callers
need to distinguish "user tampered with the URL" from "Stripe rejected
the code".
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets as stdlib_secrets
import time
from typing import Any
from urllib.parse import urlencode

import httpx


# ---------------------------------------------------------------------------
# Platform credentials — read at module init from env vars
# ---------------------------------------------------------------------------

PLATFORM_KEY = os.environ.get("STRATEGOS_STRIPE_PLATFORM_KEY", "").strip()
CONNECT_CLIENT_ID = os.environ.get("STRATEGOS_STRIPE_CONNECT_CLIENT_ID", "").strip()
PLATFORM_WEBHOOK_SECRET = os.environ.get("STRATEGOS_STRIPE_CONNECT_WEBHOOK_SECRET", "").strip()
_JWT_SECRET = os.environ.get("STRATEGOS_JWT_SECRET", "").strip()

# Stripe URLs
OAUTH_AUTHORIZE_URL = "https://connect.stripe.com/oauth/authorize"
OAUTH_TOKEN_URL = "https://connect.stripe.com/oauth/token"
OAUTH_DEAUTH_URL = "https://connect.stripe.com/oauth/deauthorize"

# State TTL — short enough that a stolen URL is mostly useless, long
# enough that a slow user finishing the consent screen doesn't see an
# "expired" error.
STATE_TTL_SECONDS = 300

# Timeout for OAuth POSTs to Stripe. 15s is generous — these are
# typically <1s — but accommodates network blips.
TIMEOUT = httpx.Timeout(15.0, connect=5.0)


def is_configured() -> bool:
    """True iff all 3 platform credentials are present.

    Called by admin endpoints + the integrations_status check before
    even attempting the OAuth dance. Returns False on partial config —
    a half-configured state is more dangerous than a fully-disabled one
    because users would see "Connect with Stripe" but get cryptic
    Stripe errors after redirect.
    """
    return bool(PLATFORM_KEY and CONNECT_CLIENT_ID and PLATFORM_WEBHOOK_SECRET)


# ---------------------------------------------------------------------------
# State signing (HMAC-SHA256, no new dep)
# ---------------------------------------------------------------------------


class InvalidState(Exception):
    """Raised when state verification fails. Caller should 400 the request.

    The `reason` attribute is short, non-leaky, and safe to surface to
    Cloud Logging without revealing what the state contained. Don't
    return the reason to the user-facing browser as-is — could enable
    oracle attacks.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _b64url_encode(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def _b64url_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def sign_state(payload: dict) -> str:
    """Encode + HMAC-sign a state payload for the OAuth flow.

    Returns `<b64url-payload>.<hex-mac>` ready for use as a `state` query
    param. Raises ValueError if `_JWT_SECRET` is missing — fail loud
    rather than emit a state that won't validate.
    """
    if not _JWT_SECRET:
        raise ValueError("STRATEGOS_JWT_SECRET not configured — cannot sign state")
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    body_b64 = _b64url_encode(body)
    mac = hmac.new(_JWT_SECRET.encode("utf-8"), body_b64.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{body_b64}.{mac}"


def verify_state(token: str) -> dict:
    """Verify HMAC + expiry, return the parsed payload.

    Raises InvalidState on any failure. We deliberately use one
    constant-time compare (hmac.compare_digest) and don't short-circuit
    on partial matches — timing oracles would be embarrassing here.
    """
    if not _JWT_SECRET:
        raise InvalidState("server_misconfigured")
    if not token or "." not in token:
        raise InvalidState("malformed")

    body_b64, mac_hex = token.rsplit(".", 1)
    expected_mac = hmac.new(
        _JWT_SECRET.encode("utf-8"), body_b64.encode("ascii"), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected_mac, mac_hex):
        raise InvalidState("bad_signature")

    try:
        payload = json.loads(_b64url_decode(body_b64).decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as e:
        raise InvalidState("malformed_payload") from e

    if not isinstance(payload, dict):
        raise InvalidState("payload_not_dict")
    exp = payload.get("exp")
    if not isinstance(exp, (int, float)) or exp < int(time.time()):
        raise InvalidState("expired")
    if not payload.get("org"):
        raise InvalidState("missing_org")

    return payload


# ---------------------------------------------------------------------------
# OAuth helpers
# ---------------------------------------------------------------------------


def build_authorize_url(org_slug: str, redirect_uri: str) -> str:
    """Compose the Stripe OAuth authorize URL the user is redirected to.

    `redirect_uri` MUST exactly match one of the URLs registered in
    Stripe Connect → Integration → Redirects. Mismatch → Stripe rejects
    the request with a generic "redirect_uri mismatch" error before
    showing the consent screen.
    """
    if not is_configured():
        raise ValueError("Stripe Connect platform credentials not configured")

    state = sign_state({
        "org": org_slug,
        "exp": int(time.time()) + STATE_TTL_SECONDS,
        "n": stdlib_secrets.token_hex(8),
    })
    qs = urlencode({
        "response_type": "code",
        "client_id": CONNECT_CLIENT_ID,
        "scope": "read_write",
        "state": state,
        "redirect_uri": redirect_uri,
    })
    return f"{OAUTH_AUTHORIZE_URL}?{qs}"


async def exchange_code(code: str) -> dict:
    """
    Exchange an OAuth `code` for tokens.

    Stripe's response on success looks like:
        {
            "access_token": "sk_acct_***",
            "refresh_token": "rt_***" (rare),
            "token_type": "bearer",
            "scope": "read_write",
            "stripe_publishable_key": "pk_***",
            "stripe_user_id": "acct_***",   # this is the connected account id
            "livemode": bool
        }

    On error: `{"error": "invalid_grant", "error_description": "..."}`

    Returns `{ok, account_id, access_token, publishable_key, livemode,
    error}`. Never raises.
    """
    if not is_configured():
        return {"ok": False, "error": "platform_not_configured"}
    if not code:
        return {"ok": False, "error": "no_code"}

    body = {
        "client_secret": PLATFORM_KEY,
        "code": code,
        "grant_type": "authorization_code",
    }
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            r = await client.post(OAUTH_TOKEN_URL, data=body)
    except httpx.HTTPError as e:
        return {"ok": False, "error": f"network:{type(e).__name__}"}

    try:
        data = r.json()
    except Exception:
        return {"ok": False, "error": f"http_{r.status_code}_unparsable"}

    if r.status_code >= 400 or "error" in data:
        return {
            "ok": False,
            "error": data.get("error_description") or data.get("error") or f"http_{r.status_code}",
        }

    account_id = data.get("stripe_user_id")
    access_token = data.get("access_token")
    if not account_id or not access_token:
        return {"ok": False, "error": "missing_account_or_token"}

    return {
        "ok": True,
        "account_id": account_id,
        "access_token": access_token,
        "publishable_key": data.get("stripe_publishable_key", ""),
        "livemode": bool(data.get("livemode", False)),
        "error": None,
    }


async def deauthorize(account_id: str) -> dict:
    """
    Revoke our platform's access to the connected account.

    Called when the tenant clicks Disconnect. After this, the
    `stripe_user_id` we previously stored will reject any
    `Stripe-Account: acct_xxx` calls we make. Caller is responsible for
    deleting the local stored credentials.

    Returns `{ok, error}`. Never raises.
    """
    if not is_configured():
        return {"ok": False, "error": "platform_not_configured"}
    if not account_id:
        return {"ok": False, "error": "no_account_id"}

    body = {
        "client_id": CONNECT_CLIENT_ID,
        "stripe_user_id": account_id,
    }
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            r = await client.post(
                OAUTH_DEAUTH_URL,
                data=body,
                auth=(PLATFORM_KEY, ""),  # Bearer-style basic auth
            )
    except httpx.HTTPError as e:
        return {"ok": False, "error": f"network:{type(e).__name__}"}

    try:
        data = r.json()
    except Exception:
        data = {}

    if r.status_code >= 400 or "error" in data:
        # 400 with "This application is not connected" means the user
        # already disconnected from Stripe's side — treat as success
        # so the local state can be cleared.
        msg = data.get("error_description") or data.get("error", "")
        if "not connected" in msg.lower():
            return {"ok": True, "error": None, "note": "already_disconnected"}
        return {"ok": False, "error": msg or f"http_{r.status_code}"}

    return {"ok": True, "error": None}


# ---------------------------------------------------------------------------
# Test hooks — let test code reset module state without monkeypatching
# ---------------------------------------------------------------------------


def _reset_for_tests(
    *,
    platform_key: str = "",
    client_id: str = "",
    webhook_secret: str = "",
    jwt_secret: str = "",
) -> None:
    """Repoint the module-level constants. Tests use this to flip
    between "configured" and "not configured" states deterministically."""
    global PLATFORM_KEY, CONNECT_CLIENT_ID, PLATFORM_WEBHOOK_SECRET, _JWT_SECRET
    PLATFORM_KEY = platform_key
    CONNECT_CLIENT_ID = client_id
    PLATFORM_WEBHOOK_SECRET = webhook_secret
    _JWT_SECRET = jwt_secret
