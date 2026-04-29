"""
End-to-end persistence-path test for the Stripe Connect OAuth callback.

What this proves: when Stripe returns a valid code-for-token exchange,
our callback endpoint correctly:
  1. Verifies the signed state
  2. Calls exchange_code (here mocked with a happy-path response)
  3. Persists 5 Setting rows (account_id, access_token, publishable_key,
     connected_at, user_email)
  4. Encrypts the access_token at rest (stored as `enc::v1::...`)
  5. Returns a 302 redirect to the dashboard with `?stripe=connected`
  6. The /connect-status endpoint then sees the tenant as connected

This is the success path that we can't hit in production without a real
Stripe authorization. With this test, we validate every line of the
callback's success branch deterministically.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

# Force consistent secrets BEFORE module imports (same pattern as the
# other Stripe Connect tests).
os.environ.setdefault("STRATEGOS_JWT_SECRET", "test-jwt-secret-32-bytes-long-padding-enough-now")
os.environ.setdefault("CRAIG_SECRETS_KEY", "test-key-very-stable-just-for-pytest-do-not-use-in-prod")
os.environ.setdefault("STRATEGOS_STRIPE_PLATFORM_KEY", "sk_test_platform_dummy")
os.environ.setdefault("STRATEGOS_STRIPE_CONNECT_CLIENT_ID", "ca_test_client_id_dummy")
os.environ.setdefault("STRATEGOS_STRIPE_CONNECT_WEBHOOK_SECRET", "whsec_test_platform_dummy")

import asyncio
import time
from unittest.mock import patch

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app import app
from db import db_session, init_db
from db.models import DEFAULT_ORG_SLUG, Setting
import stripe_connect
import secrets_crypto


@pytest.fixture(autouse=True)
def _reset():
    """Wipe the 5 Connect setting rows before + after every test so we
    can assert insert-from-empty cleanly."""
    init_db()
    keys = (
        "stripe_account_id", "stripe_access_token", "stripe_publishable_key",
        "stripe_connected_at", "stripe_user_email",
    )
    with db_session() as db:
        for k in keys:
            row = (
                db.query(Setting)
                .filter_by(organization_slug=DEFAULT_ORG_SLUG, key=k)
                .first()
            )
            if row:
                row.value = ""

    stripe_connect._reset_for_tests(
        platform_key="sk_test_platform_dummy",
        client_id="ca_test_client_id_dummy",
        webhook_secret="whsec_test_platform_dummy",
        jwt_secret=os.environ["STRATEGOS_JWT_SECRET"],
    )
    yield
    # Teardown: clear again so other test suites aren't polluted
    with db_session() as db:
        for k in keys:
            row = (
                db.query(Setting)
                .filter_by(organization_slug=DEFAULT_ORG_SLUG, key=k)
                .first()
            )
            if row:
                row.value = ""


def _make_signed_state() -> str:
    """A real, currently-valid signed state token for org=just-print."""
    return stripe_connect.sign_state({
        "org": DEFAULT_ORG_SLUG,
        "exp": int(time.time()) + 300,
        "n": "test-nonce-deterministic",
    })


@respx.mock
def test_oauth_callback_success_persists_all_five_rows():
    """
    Full happy path:
      1. Stripe responds to /oauth/token with valid tokens
      2. Stripe responds to /v1/account with the connected user_email
      3. Our callback persists 5 Setting rows
      4. stripe_access_token is encrypted at rest
      5. Redirect lands on dashboard with ?stripe=connected
    """
    # Mock Stripe's OAuth token exchange — happy path
    respx.post("https://connect.stripe.com/oauth/token").mock(
        return_value=httpx.Response(200, json={
            "access_token": "sk_acct_test_real_returned_by_stripe",
            "token_type": "bearer",
            "scope": "read_write",
            "stripe_publishable_key": "pk_test_real_returned_by_stripe",
            "stripe_user_id": "acct_1ABCDEF_just_print",
            "livemode": False,
        })
    )

    # Mock Stripe's /v1/account call (the optional email lookup our
    # callback does after exchange, to populate stripe_user_email)
    respx.get("https://api.stripe.com/v1/account").mock(
        return_value=httpx.Response(200, json={
            "id": "acct_1ABCDEF_just_print",
            "email": "info@just-print.ie",
            "country": "IE",
            "charges_enabled": True,
        })
    )

    state = _make_signed_state()
    client = TestClient(app, follow_redirects=False)

    # Hit the callback with valid state + a code (the code is opaque to us;
    # respx will intercept the actual Stripe call regardless)
    resp = client.get(
        f"/admin/api/oauth/stripe/callback?code=mock_code_xyz&state={state}"
    )

    # Should redirect to dashboard with ?stripe=connected
    assert resp.status_code == 302, f"Expected 302, got {resp.status_code}: {resp.text}"
    location = resp.headers.get("location", "")
    print(f"\nRedirect URL: {location}")
    assert "stripe=connected" in location, f"Missing ?stripe=connected in: {location}"
    assert "/c/just-print/a/craig/settings" in location, \
        f"Wrong dashboard path in redirect: {location}"

    # Verify the 5 Setting rows landed
    with db_session() as db:
        rows = {
            r.key: r.value
            for r in db.query(Setting)
            .filter_by(organization_slug=DEFAULT_ORG_SLUG)
            .filter(Setting.key.in_([
                "stripe_account_id", "stripe_access_token",
                "stripe_publishable_key", "stripe_connected_at",
                "stripe_user_email",
            ]))
            .all()
        }

    print(f"\nPersisted Setting rows for {DEFAULT_ORG_SLUG}:")
    for k, v in sorted(rows.items()):
        # Truncate long values for readability
        display = v[:50] + "..." if len(v) > 50 else v
        print(f"  {k} = {display!r}")

    # Plaintext fields — should match what Stripe returned
    assert rows["stripe_account_id"] == "acct_1ABCDEF_just_print"
    assert rows["stripe_publishable_key"] == "pk_test_real_returned_by_stripe"
    assert rows["stripe_user_email"] == "info@just-print.ie"
    # Connected_at should be a recent ISO timestamp
    assert rows["stripe_connected_at"].startswith("20"), \
        f"Expected ISO timestamp, got: {rows['stripe_connected_at']}"

    # CRITICAL: access_token must be encrypted at rest
    raw_token = rows["stripe_access_token"]
    assert raw_token.startswith("enc::v1::"), \
        f"access_token NOT encrypted at rest! Stored as: {raw_token!r}"
    assert "sk_acct_test_real_returned_by_stripe" not in raw_token, \
        "Plaintext token leaked into the encrypted column"

    # And it should decrypt back to the original
    decrypted = secrets_crypto.decrypt(raw_token)
    assert decrypted == "sk_acct_test_real_returned_by_stripe", \
        f"Decryption failed: got {decrypted!r}"

    print("\n[OK] All 5 rows persisted correctly. access_token encrypted.")


@respx.mock
def test_connect_status_endpoint_reflects_persisted_state():
    """After OAuth completes, /integrations/stripe/connect-status should
    show connected=true with the right account_id + email."""
    # Same mocks as above
    respx.post("https://connect.stripe.com/oauth/token").mock(
        return_value=httpx.Response(200, json={
            "access_token": "sk_acct_test_xxx",
            "stripe_publishable_key": "pk_test_xxx",
            "stripe_user_id": "acct_TEST_999",
            "livemode": False,
        })
    )
    respx.get("https://api.stripe.com/v1/account").mock(
        return_value=httpx.Response(200, json={
            "id": "acct_TEST_999",
            "email": "demo@example.com",
            "country": "IE",
            "charges_enabled": True,
        })
    )

    state = _make_signed_state()
    client = TestClient(app, follow_redirects=False)

    # Run the callback — same as success test, persists rows
    resp = client.get(f"/admin/api/oauth/stripe/callback?code=x&state={state}")
    assert resp.status_code == 302

    # Now hit /connect-status with a valid JWT
    import jwt
    token = jwt.encode({
        "email": "test@example.com",
        "org_slug": DEFAULT_ORG_SLUG,
        "role": "client_owner",
        "iat": int(time.time()),
        "exp": int(time.time()) + 300,
        "iss": "strategos-dashboard",
        "sub": "test@example.com",
    }, os.environ["STRATEGOS_JWT_SECRET"], algorithm="HS256")

    r = client.get(
        f"/admin/api/orgs/{DEFAULT_ORG_SLUG}/integrations/stripe/connect-status",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    print(f"\nconnect-status response: {body}")

    assert body["connected"] is True
    assert body["account_id"] == "acct_TEST_999"
    assert body["user_email"] == "demo@example.com"
    assert body["publishable_key"] == "pk_test_xxx"
    assert body["connected_at"] is not None

    print("\n[OK] /connect-status correctly reports connected=True with the persisted data.")


@respx.mock
def test_oauth_callback_handles_user_cancelled_gracefully():
    """When the user clicks 'Deny' on Stripe's consent screen, Stripe
    redirects back with ?error=access_denied. We should NOT call
    exchange_code — just redirect to dashboard with error."""
    route = respx.post("https://connect.stripe.com/oauth/token")
    state = _make_signed_state()
    client = TestClient(app, follow_redirects=False)
    resp = client.get(
        f"/admin/api/oauth/stripe/callback"
        f"?error=access_denied"
        f"&error_description=The+user+denied+your+request"
        f"&state={state}"
    )
    assert resp.status_code == 302
    location = resp.headers["location"]
    print(f"\nRedirect URL on user-cancel: {location}")
    assert "stripe=error" in location
    assert "denied" in location.lower()
    # Critical: we should NOT have called Stripe
    assert route.call_count == 0, "exchange_code should NOT run when user cancelled"

    print("\n[OK] User-cancelled path redirects cleanly without calling Stripe.")
