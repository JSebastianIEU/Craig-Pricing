"""
Integration tests that hit REAL external services.

Skipped by default (`pytest` ignores `@pytest.mark.slow`); run explicitly
with `pytest -m slow`. Two purposes:

  1. **Catch "I forgot to set timeout / wrong header / wrong auth"
     regressions** that mocked unit tests can't catch — these are tests
     against real network primitives we use.

  2. **Validate the Stripe webhook round-trip with a real signature**
     against a production-shaped event payload. Mocked tests verify the
     verify_webhook_signature function in isolation; this test runs the
     full FastAPI request pipeline (rate limit → HMAC verify → JSON
     parse → apply_webhook_event → DB write) end-to-end.

Why not gate on credentials? Because we don't need any. httpbin.org is
public, and the Stripe round-trip uses our own webhook secret to sign a
canned payload then POST it back to ourselves.

Run:
    pytest test_integration_real.py -m slow -v
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

import httpx
import pytest

import stripe_client


# =============================================================================
# Real-network: httpbin smoke
# =============================================================================


@pytest.mark.slow
def test_httpx_async_client_against_httpbin_with_our_timeout_config():
    """
    Validates that the timeout + header config we use in printlogic.py /
    stripe_client.py actually behaves correctly against a real network.

    httpbin.org returns whatever you POST to it under `json` and echoes
    headers — so we can inspect what httpx is actually sending.

    Catches: typos in header names, accidental TLS misconfig, wrong
    timeout type (int vs httpx.Timeout), DNS / connectivity issues that
    affect just our environment.
    """
    timeout = httpx.Timeout(15.0, connect=5.0)
    body = {"hello": "from-craig-test"}

    async def go() -> dict:
        async with httpx.AsyncClient(timeout=timeout) as c:
            resp = await c.post(
                "https://httpbin.org/anything",
                json=body,
                headers={"X-Craig-Test": "real-net"},
            )
            resp.raise_for_status()
            return resp.json()

    try:
        result = asyncio.run(go())
    except (httpx.HTTPError, httpx.TimeoutException) as e:
        pytest.skip(f"httpbin unreachable from this network: {e}")
        return

    # httpbin echoes back what we sent
    assert result["headers"]["X-Craig-Test"] == "real-net"
    assert result["json"] == body


# =============================================================================
# Real-pipeline: Stripe webhook round-trip with a valid signature
# =============================================================================


def _build_signed_event(secret: str, payload_dict: dict) -> tuple[bytes, str]:
    """Build a payload + Stripe-Signature header pair that
    `stripe_client.verify_webhook_signature` will accept."""
    payload = json.dumps(payload_dict).encode()
    ts = int(time.time())
    signed = f"{ts}.".encode() + payload
    mac = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    return payload, f"t={ts},v1={mac}"


@pytest.mark.slow
def test_stripe_webhook_endpoint_full_pipeline():
    """
    Round-trip test: build a real Stripe-style signed event for a Quote
    that exists in our DB, POST it to /admin/api/webhooks/stripe/just-print,
    assert the Quote flips to 'paid'.

    Exercises EVERY layer:
      - FastAPI routing
      - Rate limiter dependency
      - Stripe-Signature HMAC verification
      - JSON deserialization
      - apply_webhook_event() → quote mutation
      - Commit to the real DB engine

    Pre-conditions: stripe_webhook_secret set on the just-print tenant.
    """
    os.environ.setdefault("STRATEGOS_JWT_SECRET", "test-secret-32-bytes-long-padding-enough-now")

    # Ensure the tenant has a webhook_secret. We set + restore so we don't
    # leak state into other suites.
    from db import db_session, init_db
    from db.models import Setting, DEFAULT_ORG_SLUG, Quote
    init_db()

    SECRET = "whsec_realtest_" + "a" * 32
    saved_secret_value = None
    qid = None
    try:
        with db_session() as db:
            row = (
                db.query(Setting)
                .filter_by(organization_slug=DEFAULT_ORG_SLUG, key="stripe_webhook_secret")
                .first()
            )
            saved_secret_value = row.value if row else None
            if row:
                row.value = SECRET
            else:
                db.add(Setting(
                    organization_slug=DEFAULT_ORG_SLUG,
                    key="stripe_webhook_secret",
                    value=SECRET,
                    value_type="string",
                ))

            # Create a Quote we can flip
            q = Quote(
                organization_slug=DEFAULT_ORG_SLUG,
                product_key="business_cards",
                specs={"quantity": 100},
                base_price=38.0,
                final_price_ex_vat=38.0,
                vat_amount=5.13,
                final_price_inc_vat=43.13,
                total=43.13,
                status="confirmed",
                stripe_payment_link_id="plink_real_test",
                stripe_payment_status="unpaid",
            )
            db.add(q)
            db.flush()
            qid = q.id

        from fastapi.testclient import TestClient
        from app import app
        client = TestClient(app)

        event = {
            "id": "evt_real_test",
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "object": "checkout.session",
                    "id": "cs_real_test_abc",
                    "metadata": {"craig_quote_id": str(qid)},
                },
            },
        }
        payload, sig_header = _build_signed_event(SECRET, event)

        resp = client.post(
            f"/admin/api/webhooks/stripe/{DEFAULT_ORG_SLUG}",
            content=payload,
            headers={
                "Stripe-Signature": sig_header,
                "Content-Type": "application/json",
            },
        )
        assert resp.status_code == 200, f"webhook rejected: {resp.text}"
        assert resp.json()["received"] is True

        # Quote should now be paid
        with db_session() as db:
            persisted = db.query(Quote).filter(Quote.id == qid).first()
            assert persisted is not None
            assert persisted.stripe_payment_status == "paid"

    finally:
        # Cleanup — restore the original webhook secret + remove our test Quote
        with db_session() as db:
            row = (
                db.query(Setting)
                .filter_by(organization_slug=DEFAULT_ORG_SLUG, key="stripe_webhook_secret")
                .first()
            )
            if row:
                if saved_secret_value is None:
                    db.delete(row)
                else:
                    row.value = saved_secret_value
            if qid is not None:
                victim = db.query(Quote).filter(Quote.id == qid).first()
                if victim:
                    db.delete(victim)
