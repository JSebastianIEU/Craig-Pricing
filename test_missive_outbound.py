"""
Tests for missive_outbound.send_quote_draft.

Covers all the short-circuit branches (disabled, no token, no email,
already drafted, no from address) plus the happy path with a mocked
missive.create_new_thread_draft, plus error capture.
"""

from __future__ import annotations

import os
from unittest.mock import patch, AsyncMock

os.environ.setdefault(
    "STRATEGOS_JWT_SECRET",
    "test-secret-32-bytes-long-padding-enough-now",
)

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from db.models import (
    Base, Conversation, Quote, Setting, Product, PriceTier,
    DEFAULT_ORG_SLUG,
)
import missive_outbound


_engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
)
Base.metadata.create_all(bind=_engine)
_TestSession = sessionmaker(autocommit=False, autoflush=False, bind=_engine)


def _fresh():
    Base.metadata.drop_all(bind=_engine)
    Base.metadata.create_all(bind=_engine)


def _seed_settings(db, *, enabled="true", auto_enabled="true",
                   token="test-token", from_addr="info@just-print.ie"):
    """Helper: bulk-seed the four Settings the module reads."""
    pairs = [
        ("missive_enabled",           enabled),
        ("missive_auto_draft_enabled", auto_enabled),
        ("missive_api_token",          token),
        ("missive_from_address",       from_addr),
        ("missive_from_name",          "Justin"),
    ]
    for k, v in pairs:
        db.add(Setting(
            organization_slug=DEFAULT_ORG_SLUG,
            key=k, value=v, value_type="string",
        ))
    db.flush()


def _new_quote(db, *, with_email=True, status="confirmed", with_pay_url=True):
    """Helper: build a Conversation + Quote with optional email/pay URL."""
    conv = Conversation(
        organization_slug=DEFAULT_ORG_SLUG,
        external_id="test", channel="web",
        customer_name="Sebastian Test" if with_email else None,
        customer_email="seb@example.com" if with_email else None,
        messages=[],
    )
    db.add(conv); db.flush()

    q = Quote(
        organization_slug=DEFAULT_ORG_SLUG,
        conversation_id=conv.id,
        product_key="business_cards",
        specs={"quantity": 500},
        base_price=190.0, surcharges=[],
        final_price_ex_vat=205.0, vat_amount=27.68,
        final_price_inc_vat=232.68, artwork_cost=0.0,
        total=232.68, status=status,
        stripe_payment_link_url=(
            "https://buy.stripe.com/test_xxx" if with_pay_url else None
        ),
    )
    db.add(q); db.flush()
    return q, conv


# ---------------------------------------------------------------------------
# Short-circuit branches
# ---------------------------------------------------------------------------


def test_disabled_when_missive_enabled_false():
    _fresh()
    db = _TestSession()
    try:
        _seed_settings(db, enabled="false")
        q, _ = _new_quote(db)
        result = missive_outbound.send_quote_draft(db, q, DEFAULT_ORG_SLUG)
        assert result["skipped"] is True
        assert result["skip_reason"] == "disabled"
        assert result["draft_id"] is None
    finally:
        db.close()


def test_disabled_when_auto_draft_off():
    _fresh()
    db = _TestSession()
    try:
        _seed_settings(db, auto_enabled="false")
        q, _ = _new_quote(db)
        result = missive_outbound.send_quote_draft(db, q, DEFAULT_ORG_SLUG)
        assert result["skipped"] is True
        assert result["skip_reason"] == "disabled"
    finally:
        db.close()


def test_skips_when_no_token():
    _fresh()
    db = _TestSession()
    try:
        _seed_settings(db, token="")
        q, _ = _new_quote(db)
        result = missive_outbound.send_quote_draft(db, q, DEFAULT_ORG_SLUG)
        assert result["skipped"] is True
        assert result["skip_reason"] == "no_token"
    finally:
        db.close()


def test_skips_when_no_from_address():
    _fresh()
    db = _TestSession()
    try:
        _seed_settings(db, from_addr="")
        q, _ = _new_quote(db)
        result = missive_outbound.send_quote_draft(db, q, DEFAULT_ORG_SLUG)
        assert result["skipped"] is True
        assert result["skip_reason"] == "no_from_address"
    finally:
        db.close()


def test_skips_when_no_customer_email():
    _fresh()
    db = _TestSession()
    try:
        _seed_settings(db)
        q, _ = _new_quote(db, with_email=False)
        result = missive_outbound.send_quote_draft(db, q, DEFAULT_ORG_SLUG)
        assert result["skipped"] is True
        assert result["skip_reason"] == "no_email"
    finally:
        db.close()


def test_idempotency_short_circuits_when_already_drafted():
    """A retry of confirm_order shouldn't double-create a draft."""
    _fresh()
    db = _TestSession()
    try:
        _seed_settings(db)
        q, _ = _new_quote(db)
        q.missive_draft_id = "draft_existing_xyz"
        db.flush()

        with patch(
            "missive.create_new_thread_draft",
            new=AsyncMock(return_value={"drafts": {"id": "should_not_fire"}}),
        ) as mock_draft:
            result = missive_outbound.send_quote_draft(db, q, DEFAULT_ORG_SLUG)

        assert result["ok"] is True
        assert result["skipped"] is True
        assert result["skip_reason"] == "already_drafted"
        assert result["draft_id"] == "draft_existing_xyz"
        assert mock_draft.await_count == 0, "Must NOT call Missive when already drafted"
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_happy_path_creates_draft_and_persists_id():
    _fresh()
    db = _TestSession()
    try:
        _seed_settings(db)
        q, conv = _new_quote(db)

        with patch(
            "missive.create_new_thread_draft",
            new=AsyncMock(return_value={"drafts": {"id": "draft_new_abc"}}),
        ) as mock_draft, patch(
            # Avoid running the real PDF generator (reportlab) in unit tests
            "missive_outbound._build_attachments",
            return_value=[{"filename": "x.pdf", "base64_data": "AAA"}],
        ):
            result = missive_outbound.send_quote_draft(db, q, DEFAULT_ORG_SLUG)

        assert result["ok"] is True
        assert result["skipped"] is False
        assert result["draft_id"] == "draft_new_abc"

        # Persisted on the row
        db.refresh(q)
        assert q.missive_draft_id == "draft_new_abc"
        assert q.missive_drafted_at is not None
        assert q.missive_last_error is None

        # Verify the call shape
        assert mock_draft.await_count == 1
        kwargs = mock_draft.await_args.kwargs
        assert kwargs["from_address"] == "info@just-print.ie"
        assert kwargs["from_name"] == "Justin"
        assert kwargs["to_fields"] == [
            {"address": "seb@example.com", "name": "Sebastian Test"},
        ]
        assert kwargs["subject"] == f"Your quote from Just Print — JP-{q.id:04d}"
        # Body should mention the payment URL we seeded on the quote
        assert "buy.stripe.com/test_xxx" in kwargs["html_body"]
        # And the total
        assert "232.68" in kwargs["html_body"]
    finally:
        db.close()


def test_happy_path_without_payment_link_still_drafts():
    """Missive draft must still go out even when Stripe is disabled
    (or the link couldn't be created)."""
    _fresh()
    db = _TestSession()
    try:
        _seed_settings(db)
        q, _ = _new_quote(db, with_pay_url=False)

        with patch(
            "missive.create_new_thread_draft",
            new=AsyncMock(return_value={"drafts": {"id": "draft_no_stripe"}}),
        ) as mock_draft, patch(
            "missive_outbound._build_attachments",
            return_value=None,  # Even with no PDF, we still go
        ):
            result = missive_outbound.send_quote_draft(db, q, DEFAULT_ORG_SLUG)

        assert result["ok"] is True
        assert result["draft_id"] == "draft_no_stripe"
        kwargs = mock_draft.await_args.kwargs
        assert "buy.stripe.com" not in kwargs["html_body"]
        assert kwargs.get("attachments") is None
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Error capture
# ---------------------------------------------------------------------------


def test_missive_api_failure_persists_error_and_returns_clean_dict():
    """If Missive returns 4xx, we capture the error on the row but
    return a clean structured dict (never raise to caller)."""
    _fresh()
    db = _TestSession()
    try:
        _seed_settings(db)
        q, _ = _new_quote(db)

        with patch(
            "missive.create_new_thread_draft",
            new=AsyncMock(side_effect=RuntimeError("Missive 400: bad payload")),
        ), patch(
            "missive_outbound._build_attachments",
            return_value=[{"filename": "x.pdf", "base64_data": "AAA"}],
        ):
            result = missive_outbound.send_quote_draft(db, q, DEFAULT_ORG_SLUG)

        assert result["ok"] is False
        assert result["skipped"] is False
        assert result["error"] is not None
        assert "RuntimeError" in result["error"] or "Missive 400" in result["error"]

        db.refresh(q)
        assert q.missive_draft_id is None
        assert q.missive_last_error is not None
        assert "Missive 400" in q.missive_last_error or "RuntimeError" in q.missive_last_error
    finally:
        db.close()


def test_response_with_top_level_id_is_accepted():
    """Some Missive endpoints return {id: ...} instead of {drafts: {id: ...}}.
    Both shapes must be handled."""
    _fresh()
    db = _TestSession()
    try:
        _seed_settings(db)
        q, _ = _new_quote(db)

        with patch(
            "missive.create_new_thread_draft",
            new=AsyncMock(return_value={"id": "top_level_id_format"}),
        ), patch(
            "missive_outbound._build_attachments", return_value=None,
        ):
            result = missive_outbound.send_quote_draft(db, q, DEFAULT_ORG_SLUG)

        assert result["ok"] is True
        assert result["draft_id"] == "top_level_id_format"
    finally:
        db.close()
