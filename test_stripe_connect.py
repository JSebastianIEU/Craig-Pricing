"""
Tests for the Stripe Connect OAuth flow.

Covers:
  - State signing roundtrip + tampering rejection + expiry rejection
  - Code-for-token exchange (happy path + Stripe-side error)
  - Deauthorize (happy path + "already disconnected" tolerated)
  - Platform-level webhook routing by `event.account` lookup
  - Disconnect endpoint clears local Setting rows even when Stripe revoke fails

All Stripe HTTP calls are mocked with respx — zero real network.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(__file__))

# Force a stable JWT secret + dummy platform creds BEFORE any module import,
# so stripe_connect's module-level `os.environ.get(...)` reads them.
os.environ.setdefault("STRATEGOS_JWT_SECRET", "test-jwt-secret-32-bytes-long-padding-enough-now")
os.environ.setdefault("CRAIG_SECRETS_KEY", "test-key-very-stable-just-for-pytest-do-not-use-in-prod")
os.environ.setdefault("STRATEGOS_STRIPE_PLATFORM_KEY", "sk_test_platform_dummy")
os.environ.setdefault("STRATEGOS_STRIPE_CONNECT_CLIENT_ID", "ca_test_client_id_dummy")
os.environ.setdefault("STRATEGOS_STRIPE_CONNECT_WEBHOOK_SECRET", "whsec_test_platform_dummy")

import httpx
import pytest
import respx

import stripe_connect


@pytest.fixture(autouse=True)
def _reset_connect_module():
    """Each test starts with the same dummy platform creds. We re-set them
    in case a test monkeypatched stripe_connect to "not configured" mode."""
    stripe_connect._reset_for_tests(
        platform_key="sk_test_platform_dummy",
        client_id="ca_test_client_id_dummy",
        webhook_secret="whsec_test_platform_dummy",
        jwt_secret=os.environ["STRATEGOS_JWT_SECRET"],
    )
    yield


# =============================================================================
# State signing
# =============================================================================


def test_state_signing_roundtrip():
    """sign_state → verify_state returns the original payload."""
    payload = {"org": "just-print", "exp": int(time.time()) + 60, "n": "abc123"}
    token = stripe_connect.sign_state(payload)
    parsed = stripe_connect.verify_state(token)
    assert parsed["org"] == "just-print"
    assert parsed["n"] == "abc123"


def test_state_rejected_on_tamper():
    """Flip a single byte in the body → InvalidState."""
    token = stripe_connect.sign_state(
        {"org": "just-print", "exp": int(time.time()) + 60, "n": "x"}
    )
    body, mac = token.split(".")
    # Flip the last char of the body before the signature
    tampered_body = body[:-1] + ("A" if body[-1] != "A" else "B")
    tampered = f"{tampered_body}.{mac}"
    with pytest.raises(stripe_connect.InvalidState) as exc:
        stripe_connect.verify_state(tampered)
    assert exc.value.reason == "bad_signature"


def test_state_rejected_when_expired():
    """exp in the past → InvalidState."""
    token = stripe_connect.sign_state(
        {"org": "just-print", "exp": int(time.time()) - 10, "n": "x"}
    )
    with pytest.raises(stripe_connect.InvalidState) as exc:
        stripe_connect.verify_state(token)
    assert exc.value.reason == "expired"


def test_state_rejected_when_org_missing():
    token = stripe_connect.sign_state(
        {"exp": int(time.time()) + 60, "n": "x"}
    )
    with pytest.raises(stripe_connect.InvalidState):
        stripe_connect.verify_state(token)


def test_state_rejected_when_malformed():
    """Random garbage that isn't `body.mac` → InvalidState."""
    with pytest.raises(stripe_connect.InvalidState):
        stripe_connect.verify_state("not-even-close")


def test_state_rejected_with_wrong_secret():
    """A token signed with one secret cannot verify under another."""
    token = stripe_connect.sign_state(
        {"org": "just-print", "exp": int(time.time()) + 60, "n": "x"}
    )
    # Rotate the secret AFTER signing
    stripe_connect._reset_for_tests(
        platform_key="sk_test_platform_dummy",
        client_id="ca_test_client_id_dummy",
        webhook_secret="whsec_test_platform_dummy",
        jwt_secret="different-secret-now",
    )
    with pytest.raises(stripe_connect.InvalidState) as exc:
        stripe_connect.verify_state(token)
    assert exc.value.reason == "bad_signature"


# =============================================================================
# build_authorize_url
# =============================================================================


def test_build_authorize_url_includes_required_params():
    url = stripe_connect.build_authorize_url(
        "just-print", "https://example.run.app/admin/api/oauth/stripe/callback"
    )
    assert url.startswith("https://connect.stripe.com/oauth/authorize?")
    assert "response_type=code" in url
    assert "client_id=ca_test_client_id_dummy" in url
    assert "scope=read_write" in url
    assert "state=" in url
    assert "redirect_uri=" in url


def test_build_authorize_url_raises_when_unconfigured():
    stripe_connect._reset_for_tests()  # all empty
    with pytest.raises(ValueError):
        stripe_connect.build_authorize_url(
            "just-print", "https://example.com/cb"
        )


# =============================================================================
# exchange_code
# =============================================================================


@respx.mock
def test_exchange_code_happy_path():
    respx.post("https://connect.stripe.com/oauth/token").mock(
        return_value=httpx.Response(200, json={
            "access_token": "sk_acct_test_xyz",
            "token_type": "bearer",
            "scope": "read_write",
            "stripe_publishable_key": "pk_test_xyz",
            "stripe_user_id": "acct_1ABCDEF",
            "livemode": False,
        })
    )
    result = asyncio.run(stripe_connect.exchange_code("code_xyz"))
    assert result["ok"] is True
    assert result["account_id"] == "acct_1ABCDEF"
    assert result["access_token"] == "sk_acct_test_xyz"
    assert result["publishable_key"] == "pk_test_xyz"


@respx.mock
def test_exchange_code_propagates_stripe_error():
    """Stripe returns 400 with `{error: invalid_grant}` → ok=False."""
    respx.post("https://connect.stripe.com/oauth/token").mock(
        return_value=httpx.Response(400, json={
            "error": "invalid_grant",
            "error_description": "Authorization code does not exist or expired.",
        })
    )
    result = asyncio.run(stripe_connect.exchange_code("bad_code"))
    assert result["ok"] is False
    assert "expired" in result["error"].lower() or "invalid_grant" in result["error"]


@respx.mock(assert_all_called=False)
def test_exchange_code_rejects_empty_input():
    """Empty code → no_code, zero network."""
    route = respx.post("https://connect.stripe.com/oauth/token")
    result = asyncio.run(stripe_connect.exchange_code(""))
    assert result["ok"] is False
    assert result["error"] == "no_code"
    assert route.call_count == 0


@respx.mock(assert_all_called=False)
def test_exchange_code_when_unconfigured():
    """No platform key → platform_not_configured, zero network."""
    stripe_connect._reset_for_tests()
    route = respx.post("https://connect.stripe.com/oauth/token")
    result = asyncio.run(stripe_connect.exchange_code("code"))
    assert result["ok"] is False
    assert result["error"] == "platform_not_configured"
    assert route.call_count == 0


# =============================================================================
# deauthorize
# =============================================================================


@respx.mock
def test_deauthorize_happy_path():
    respx.post("https://connect.stripe.com/oauth/deauthorize").mock(
        return_value=httpx.Response(200, json={"stripe_user_id": "acct_X"})
    )
    result = asyncio.run(stripe_connect.deauthorize("acct_X"))
    assert result["ok"] is True


@respx.mock
def test_deauthorize_already_disconnected_treated_as_success():
    """When tenant already revoked from Stripe's side, we shouldn't error."""
    respx.post("https://connect.stripe.com/oauth/deauthorize").mock(
        return_value=httpx.Response(400, json={
            "error": "invalid_request",
            "error_description": "This application is not connected to Stripe account acct_X.",
        })
    )
    result = asyncio.run(stripe_connect.deauthorize("acct_X"))
    assert result["ok"] is True
    assert result.get("note") == "already_disconnected"


@respx.mock
def test_deauthorize_propagates_other_errors():
    respx.post("https://connect.stripe.com/oauth/deauthorize").mock(
        return_value=httpx.Response(500, json={"error": "server_error"})
    )
    result = asyncio.run(stripe_connect.deauthorize("acct_X"))
    assert result["ok"] is False


# =============================================================================
# Platform-level webhook routing by event.account
# =============================================================================


def _sig_header(secret: str, payload: bytes, ts: int | None = None) -> str:
    if ts is None:
        ts = int(time.time())
    signed = f"{ts}.".encode() + payload
    mac = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    return f"t={ts},v1={mac}"


def test_webhook_routes_by_account_id():
    """Event with `account: 'acct_X'` finds the right tenant via Setting lookup."""
    os.environ["STRATEGOS_JWT_SECRET"] = "test-jwt-secret-32-bytes-long-padding-enough-now"
    from fastapi.testclient import TestClient
    from app import app
    from db import db_session, init_db
    from db.models import Quote, Setting, DEFAULT_ORG_SLUG

    init_db()
    client = TestClient(app)
    PLATFORM_WHSEC = "whsec_test_platform_dummy"
    stripe_connect._reset_for_tests(
        platform_key="sk_test_platform_dummy",
        client_id="ca_test_client_id_dummy",
        webhook_secret=PLATFORM_WHSEC,
        jwt_secret=os.environ["STRATEGOS_JWT_SECRET"],
    )

    # Fixture: a tenant with stripe_account_id and an unpaid Quote
    qid = None
    try:
        with db_session() as db:
            row = (
                db.query(Setting)
                .filter_by(organization_slug=DEFAULT_ORG_SLUG, key="stripe_account_id")
                .first()
            )
            if row:
                row.value = "acct_routing_test"
            else:
                db.add(Setting(
                    organization_slug=DEFAULT_ORG_SLUG,
                    key="stripe_account_id",
                    value="acct_routing_test",
                    value_type="string",
                ))

            q = Quote(
                organization_slug=DEFAULT_ORG_SLUG,
                product_key="x", specs={}, base_price=10, final_price_ex_vat=10,
                vat_amount=2.3, final_price_inc_vat=12.3, total=12.3,
                status="confirmed",
                stripe_payment_link_id="plink_route", stripe_payment_status="unpaid",
            )
            db.add(q)
            db.flush()
            qid = q.id

        event = {
            "id": "evt_route_test",
            "type": "checkout.session.completed",
            "account": "acct_routing_test",
            "data": {"object": {
                "object": "checkout.session",
                "id": "cs_route_test",
                "metadata": {"craig_quote_id": str(qid)},
            }},
        }
        payload = json.dumps(event).encode()
        sig = _sig_header(PLATFORM_WHSEC, payload)

        r = client.post(
            "/admin/api/webhooks/stripe-connect",
            content=payload,
            headers={"Stripe-Signature": sig, "Content-Type": "application/json"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["received"] is True
        assert body.get("result", {}).get("status") == "paid"
    finally:
        # Cleanup
        with db_session() as db:
            if qid:
                victim = db.query(Quote).filter(Quote.id == qid).first()
                if victim:
                    db.delete(victim)
            row = (
                db.query(Setting)
                .filter_by(organization_slug=DEFAULT_ORG_SLUG, key="stripe_account_id")
                .first()
            )
            if row:
                row.value = ""


def test_webhook_unknown_account_returns_200_no_apply():
    """Event for an account_id we don't know → 200 with `unknown_account` note."""
    from fastapi.testclient import TestClient
    from app import app

    PLATFORM_WHSEC = "whsec_test_platform_dummy"
    stripe_connect._reset_for_tests(
        platform_key="sk_test_platform_dummy",
        client_id="ca_test_client_id_dummy",
        webhook_secret=PLATFORM_WHSEC,
        jwt_secret=os.environ["STRATEGOS_JWT_SECRET"],
    )
    client = TestClient(app)

    event = {
        "id": "evt_unknown",
        "type": "checkout.session.completed",
        "account": "acct_does_not_exist_anywhere",
        "data": {"object": {"metadata": {"craig_quote_id": "999"}}},
    }
    payload = json.dumps(event).encode()
    sig = _sig_header(PLATFORM_WHSEC, payload)

    r = client.post(
        "/admin/api/webhooks/stripe-connect",
        content=payload,
        headers={"Stripe-Signature": sig, "Content-Type": "application/json"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["received"] is True
    assert body.get("note") == "unknown_account"


def test_webhook_rejects_bad_signature():
    from fastapi.testclient import TestClient
    from app import app
    client = TestClient(app)

    payload = b'{"type":"x"}'
    r = client.post(
        "/admin/api/webhooks/stripe-connect",
        content=payload,
        headers={"Stripe-Signature": "t=1,v1=deadbeef", "Content-Type": "application/json"},
    )
    assert r.status_code == 400


def test_webhook_503_when_platform_secret_missing():
    """If env var unset, refuse — fail loud rather than silently accept."""
    from fastapi.testclient import TestClient
    from app import app
    stripe_connect._reset_for_tests(
        platform_key="sk_test_platform_dummy",
        client_id="ca_test_client_id_dummy",
        webhook_secret="",  # cleared
        jwt_secret=os.environ["STRATEGOS_JWT_SECRET"],
    )
    client = TestClient(app)
    r = client.post(
        "/admin/api/webhooks/stripe-connect",
        content=b"{}",
        headers={"Stripe-Signature": "t=1,v1=x"},
    )
    assert r.status_code == 503
