"""
Unit tests for the Stripe integration (Phase B).

Every test is fully mocked with `respx` — zero real network. The test suite
is the gate that proves: (a) the orchestrator respects the `stripe_enabled`
kill switch, (b) idempotency holds, (c) webhook HMAC verification is
correct including rejection of tampered / expired events, (d) the apply
path mutates the Quote row correctly.

Run with:
    pytest test_stripe.py -q
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
import respx

import stripe_client
import stripe_push
from db import db_session, init_db
from db.models import DEFAULT_ORG_SLUG, Quote, Setting


ORG = DEFAULT_ORG_SLUG
STRIPE_URL_CREATE = "https://api.stripe.com/v1/payment_links"


# =============================================================================
# Fixtures / helpers
# =============================================================================


def _set_setting(key: str, value: str, value_type: str = "string") -> None:
    init_db()
    with db_session() as s:
        row = s.query(Setting).filter_by(organization_slug=ORG, key=key).first()
        if row:
            row.value = value
            row.value_type = value_type
        else:
            s.add(Setting(organization_slug=ORG, key=key, value=value, value_type=value_type))


def _clear_setting(key: str) -> None:
    with db_session() as s:
        row = s.query(Setting).filter_by(organization_slug=ORG, key=key).first()
        if row:
            s.delete(row)


def _make_quote(total: float = 234.56, **overrides) -> Quote:
    """In-memory Quote — not persisted. Sufficient for orchestrator tests
    that read from the row and mutate it; the caller-controlled session
    would commit in production."""
    defaults = dict(
        id=42,
        organization_slug=ORG,
        conversation_id=None,
        product_key="business_cards",
        specs={"quantity": 500, "finish": "gloss", "double_sided": False},
        base_price=190.0,
        final_price_ex_vat=190.0,
        vat_amount=25.65,
        final_price_inc_vat=215.65,
        artwork_cost=0.0,
        total=total,
        status="confirmed",
    )
    defaults.update(overrides)
    return Quote(**defaults)


@pytest.fixture(autouse=True)
def _reset_stripe_settings():
    """Every test starts from a known-clean state: disabled, no keys."""
    for k in ("stripe_enabled", "stripe_secret_key", "stripe_webhook_secret",
              "stripe_currency", "stripe_success_url"):
        _clear_setting(k)
    yield
    for k in ("stripe_enabled", "stripe_secret_key", "stripe_webhook_secret",
              "stripe_currency", "stripe_success_url"):
        _clear_setting(k)


# =============================================================================
# stripe_client.create_payment_link
# =============================================================================


@respx.mock
def test_create_payment_link_sends_form_encoded_with_metadata():
    """Stripe expects application/x-www-form-urlencoded with bracket-nested
    metadata. We also stamp craig_quote_id so webhooks can correlate back."""
    route = respx.post(STRIPE_URL_CREATE).mock(
        return_value=httpx.Response(200, json={
            "id": "plink_test_abc", "url": "https://buy.stripe.com/test_abc",
        })
    )
    result = asyncio.run(stripe_client.create_payment_link(
        api_key="sk_test_x", quote_id=42, amount_eur=215.65,
        product_description="500 biz cards", organization_slug=ORG,
    ))
    assert result["ok"] is True
    assert result["link_id"] == "plink_test_abc"
    assert result["url"] == "https://buy.stripe.com/test_abc"

    call = route.calls[0]
    body = call.request.content.decode()
    # Amount in cents, bracket notation, metadata present. Bracket chars
    # are URL-encoded by urlencode, so check the encoded key forms.
    assert "unit_amount%5D=21565" in body
    assert "%5Bcurrency%5D=eur" in body
    assert "metadata%5Bcraig_quote_id%5D=42" in body
    # Auth via HTTP Basic
    assert call.request.headers["authorization"].startswith("Basic ")


@respx.mock(assert_all_called=False)
def test_create_payment_link_rejects_zero_amount_without_network():
    """Zero-amount quotes must never hit Stripe (they'd return 400)."""
    route = respx.post(STRIPE_URL_CREATE)
    result = asyncio.run(stripe_client.create_payment_link(
        api_key="sk_test_x", quote_id=1, amount_eur=0.0, product_description="nope",
    ))
    assert result["ok"] is False
    assert result["error"] == "amount_must_be_positive"
    assert route.call_count == 0


@respx.mock(assert_all_called=False)
def test_create_payment_link_no_api_key_zero_network():
    route = respx.post(STRIPE_URL_CREATE)
    result = asyncio.run(stripe_client.create_payment_link(
        api_key="", quote_id=1, amount_eur=10.0, product_description="x",
    ))
    assert result["ok"] is False
    assert result["error"] == "no_api_key"
    assert route.call_count == 0


@respx.mock
def test_create_payment_link_401_returns_structured_error():
    respx.post(STRIPE_URL_CREATE).mock(
        return_value=httpx.Response(401, json={"error": {"message": "Invalid API Key"}})
    )
    result = asyncio.run(stripe_client.create_payment_link(
        api_key="sk_bad", quote_id=1, amount_eur=10.0, product_description="x",
    ))
    assert result["ok"] is False
    assert "Invalid API Key" in result["error"]


@respx.mock
def test_create_payment_link_malformed_response_flagged():
    """Stripe shouldn't ever return 200 without an id, but if it does we
    must not persist None as a successful link."""
    respx.post(STRIPE_URL_CREATE).mock(
        return_value=httpx.Response(200, json={"weird": "shape"})
    )
    result = asyncio.run(stripe_client.create_payment_link(
        api_key="sk_test_x", quote_id=1, amount_eur=10.0, product_description="x",
    ))
    assert result["ok"] is False
    assert result["error"] == "malformed_response"


# =============================================================================
# Webhook signature verification
# =============================================================================


def _sig_header(secret: str, payload: bytes, ts: int | None = None) -> tuple[str, int]:
    if ts is None:
        ts = int(time.time())
    signed = f"{ts}.".encode() + payload
    mac = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    return f"t={ts},v1={mac}", ts


def test_verify_webhook_accepts_valid_signature():
    secret = "whsec_test"
    payload = b'{"type":"checkout.session.completed"}'
    header, ts = _sig_header(secret, payload)
    # Must not raise
    stripe_client.verify_webhook_signature(payload, header, secret, now=ts)


def test_verify_webhook_rejects_tampered_payload():
    secret = "whsec_test"
    payload = b'{"type":"checkout.session.completed"}'
    header, ts = _sig_header(secret, payload)
    tampered = b'{"type":"payment_intent.payment_failed"}'
    with pytest.raises(stripe_client.InvalidSignature):
        stripe_client.verify_webhook_signature(tampered, header, secret, now=ts)


def test_verify_webhook_rejects_expired_timestamp():
    secret = "whsec_test"
    payload = b'{"type":"x"}'
    old_ts = int(time.time()) - 10_000  # way outside tolerance
    header, _ = _sig_header(secret, payload, ts=old_ts)
    with pytest.raises(stripe_client.InvalidSignature):
        stripe_client.verify_webhook_signature(payload, header, secret)


def test_verify_webhook_rejects_missing_secret():
    with pytest.raises(stripe_client.InvalidSignature):
        stripe_client.verify_webhook_signature(b"x", "t=1,v1=abc", "")


def test_verify_webhook_rejects_missing_header():
    with pytest.raises(stripe_client.InvalidSignature):
        stripe_client.verify_webhook_signature(b"x", "", "whsec_x")


def test_verify_webhook_accepts_any_of_multiple_v1_sigs():
    """During key rotation Stripe may attach multiple v1= values. One match wins."""
    secret = "whsec_current"
    payload = b'{"ok":1}'
    ts = int(time.time())
    signed = f"{ts}.".encode() + payload
    good = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    header = f"t={ts},v1=deadbeef,v1={good}"
    stripe_client.verify_webhook_signature(payload, header, secret, now=ts)


# =============================================================================
# Orchestrator: stripe_push.create_link_for_quote
# =============================================================================


@respx.mock(assert_all_called=False)
def test_push_disabled_by_default_zero_network():
    """stripe_enabled absent / 'false' → no network, no mutation."""
    route = respx.post(STRIPE_URL_CREATE)
    with db_session() as db:
        q = _make_quote()
        result = stripe_push.create_link_for_quote(db, q, ORG)
    assert result["ok"] is False
    assert result["disabled"] is True
    assert result["error"] == "disabled"
    assert route.call_count == 0
    assert q.stripe_payment_link_id is None


@respx.mock(assert_all_called=False)
def test_push_enabled_but_no_api_key_zero_network():
    _set_setting("stripe_enabled", "true")
    route = respx.post(STRIPE_URL_CREATE)
    with db_session() as db:
        q = _make_quote()
        result = stripe_push.create_link_for_quote(db, q, ORG)
    assert result["ok"] is False
    assert result["disabled"] is False
    assert result["error"] == "no_api_key"
    assert route.call_count == 0


@respx.mock
def test_push_happy_path_persists_link_on_quote():
    _set_setting("stripe_enabled", "true")
    _set_setting("stripe_secret_key", "sk_test_x")
    respx.post(STRIPE_URL_CREATE).mock(
        return_value=httpx.Response(200, json={
            "id": "plink_real", "url": "https://buy.stripe.com/real",
        })
    )
    with db_session() as db:
        q = _make_quote()
        result = stripe_push.create_link_for_quote(db, q, ORG)
    assert result["ok"] is True
    assert result["url"] == "https://buy.stripe.com/real"
    assert q.stripe_payment_link_id == "plink_real"
    assert q.stripe_payment_link_url == "https://buy.stripe.com/real"
    assert q.stripe_payment_status == "unpaid"
    assert q.stripe_last_error is None


@respx.mock(assert_all_called=False)
def test_push_idempotent_when_link_already_exists():
    """Second call must NOT hit Stripe — return the cached link id."""
    _set_setting("stripe_enabled", "true")
    _set_setting("stripe_secret_key", "sk_test_x")
    route = respx.post(STRIPE_URL_CREATE)
    with db_session() as db:
        q = _make_quote()
        q.stripe_payment_link_id = "plink_existing"
        q.stripe_payment_link_url = "https://buy.stripe.com/existing"
        result = stripe_push.create_link_for_quote(db, q, ORG)
    assert result["ok"] is True
    assert result["already_exists"] is True
    assert result["link_id"] == "plink_existing"
    assert route.call_count == 0


@respx.mock
def test_push_persists_error_on_failure():
    _set_setting("stripe_enabled", "true")
    _set_setting("stripe_secret_key", "sk_test_x")
    respx.post(STRIPE_URL_CREATE).mock(
        return_value=httpx.Response(402, json={"error": {"message": "Your card was declined."}})
    )
    with db_session() as db:
        q = _make_quote()
        result = stripe_push.create_link_for_quote(db, q, ORG)
    assert result["ok"] is False
    assert "declined" in (q.stripe_last_error or "")
    assert q.stripe_payment_link_id is None


@respx.mock(assert_all_called=False)
def test_push_zero_amount_quote_short_circuits():
    _set_setting("stripe_enabled", "true")
    _set_setting("stripe_secret_key", "sk_test_x")
    route = respx.post(STRIPE_URL_CREATE)
    with db_session() as db:
        q = _make_quote(total=0.0, final_price_inc_vat=0.0)
        result = stripe_push.create_link_for_quote(db, q, ORG)
    assert result["ok"] is False
    assert result["error"] == "zero_amount"
    assert route.call_count == 0


# =============================================================================
# Webhook apply
# =============================================================================


def test_apply_webhook_marks_quote_paid():
    """checkout.session.completed with our quote_id in metadata → paid."""
    init_db()
    with db_session() as db:
        # Persist a Quote so the lookup succeeds
        q = Quote(
            organization_slug=ORG,
            product_key="x", specs={}, base_price=10, final_price_ex_vat=10,
            vat_amount=2.3, final_price_inc_vat=12.3, total=12.3, status="confirmed",
            stripe_payment_link_id="plink_x", stripe_payment_status="unpaid",
        )
        db.add(q)
        db.flush()
        qid = q.id

        event = {
            "type": "checkout.session.completed",
            "data": {"object": {
                "object": "checkout.session",
                "id": "cs_test_123",
                "metadata": {"craig_quote_id": str(qid)},
            }},
        }
        result = stripe_push.apply_webhook_event(db, event, ORG)
        assert result["handled"] is True
        assert result["status"] == "paid"
        # No refresh — apply mutates q in-memory; refresh would re-SELECT
        # and discard pending changes before commit.
        assert q.stripe_payment_status == "paid"
        assert q.stripe_paid_at is not None
        assert q.stripe_checkout_session_id == "cs_test_123"

        db.delete(q)


def test_apply_webhook_refund_after_paid():
    init_db()
    with db_session() as db:
        q = Quote(
            organization_slug=ORG,
            product_key="x", specs={}, base_price=10, final_price_ex_vat=10,
            vat_amount=2.3, final_price_inc_vat=12.3, total=12.3, status="confirmed",
            stripe_payment_status="paid",
        )
        db.add(q)
        db.flush()
        qid = q.id

        event = {
            "type": "charge.refunded",
            "data": {"object": {"metadata": {"craig_quote_id": str(qid)}}},
        }
        result = stripe_push.apply_webhook_event(db, event, ORG)
        assert result["status"] == "refunded"
        assert q.stripe_payment_status == "refunded"

        db.delete(q)


def test_apply_webhook_ignores_unknown_event_type():
    init_db()
    with db_session() as db:
        q = Quote(
            organization_slug=ORG,
            product_key="x", specs={}, base_price=10, final_price_ex_vat=10,
            vat_amount=2.3, final_price_inc_vat=12.3, total=12.3, status="confirmed",
        )
        db.add(q)
        db.flush()
        qid = q.id

        event = {
            "type": "customer.created",  # unrelated
            "data": {"object": {"metadata": {"craig_quote_id": str(qid)}}},
        }
        result = stripe_push.apply_webhook_event(db, event, ORG)
        assert result["handled"] is False

        db.delete(q)


def test_apply_webhook_no_metadata_is_silent_skip():
    """Stripe sends many events that have nothing to do with us (e.g.
    customer.created). Not finding our metadata is not an error."""
    init_db()
    with db_session() as db:
        event = {"type": "customer.created", "data": {"object": {}}}
        result = stripe_push.apply_webhook_event(db, event, ORG)
        assert result["handled"] is False
        assert "no_quote_id" in (result.get("reason") or "")


def test_apply_webhook_paid_then_paid_again_is_noop():
    """Stripe retries events at-least-once; the second 'paid' must not flip
    anything weird (e.g. overwrite paid_at to a later time we don't want)."""
    init_db()
    with db_session() as db:
        q = Quote(
            organization_slug=ORG,
            product_key="x", specs={}, base_price=10, final_price_ex_vat=10,
            vat_amount=2.3, final_price_inc_vat=12.3, total=12.3, status="confirmed",
            stripe_payment_status="paid",
        )
        db.add(q)
        db.flush()
        qid = q.id

        event = {
            "type": "payment_intent.succeeded",
            "data": {"object": {"metadata": {"craig_quote_id": str(qid)}}},
        }
        result = stripe_push.apply_webhook_event(db, event, ORG)
        assert result["status"] == "paid"
        assert "no_regression" in (result.get("reason") or "")

        db.delete(q)
