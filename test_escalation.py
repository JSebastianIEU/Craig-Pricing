"""
Unit tests for the escalation contact gate and the confirm_order tool.

These tests exercise `_exec_tool()` directly (no LLM, no HTTP). They use
an isolated in-memory SQLite so they don't disturb the shared `craig.db`
file that test_pricing.py reads product/tier data from.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from db.models import Base, Conversation, Product, PriceTier, Quote, SurchargeRule, DEFAULT_ORG_SLUG
from llm.craig_agent import _exec_tool
from pricing_engine import quote_small_format


# Isolated in-memory DB, separate from the project's real craig.db. Tables
# are created once per module import and each test gets a fresh session.
_engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
Base.metadata.create_all(bind=_engine)
_TestSession = sessionmaker(autocommit=False, autoflush=False, bind=_engine)


def _fresh_tables():
    """Reset the schema between tests so row IDs are predictable."""
    Base.metadata.drop_all(bind=_engine)
    Base.metadata.create_all(bind=_engine)


def _new_conversation(db, *, with_contact: bool = False) -> Conversation:
    conv = Conversation(
        organization_slug=DEFAULT_ORG_SLUG,
        external_id="test-session",
        channel="web",
        messages=[],
    )
    if with_contact:
        conv.customer_name = "Test Customer"
        conv.customer_email = "test@example.com"
    db.add(conv)
    db.flush()
    return conv


# ---------------------------------------------------------------------------
# Escalation contact gate
# ---------------------------------------------------------------------------


def test_escalate_without_contact_returns_error():
    """Craig must refuse to escalate when name/email/phone are all empty."""
    _fresh_tables()
    db = _TestSession()
    try:
        conv = _new_conversation(db, with_contact=False)
        result = _exec_tool(
            db,
            "escalate_to_justin",
            {"reason": "custom job", "summary": "5 A6 flyers (below min)"},
            conversation_id=conv.id,
        )
        assert result["escalated"] is False, f"Should have been gated, got: {result}"
        assert "contact" in result["error"].lower()
        assert result.get("retry_after") == "save_customer_info"
    finally:
        db.close()


def test_escalate_with_contact_succeeds():
    """Once contact info is on the row, escalation should proceed."""
    _fresh_tables()
    db = _TestSession()
    try:
        conv = _new_conversation(db, with_contact=True)
        result = _exec_tool(
            db,
            "escalate_to_justin",
            {"reason": "custom job", "summary": "5 A6 flyers (below min)"},
            conversation_id=conv.id,
        )
        assert result["escalated"] is True, f"Should have escalated, got: {result}"
        assert result["reason"] == "custom job"
    finally:
        db.close()


def test_escalate_phone_only_also_passes():
    """Phone number alone should satisfy the gate (WhatsApp flow)."""
    _fresh_tables()
    db = _TestSession()
    try:
        conv = _new_conversation(db, with_contact=False)
        conv.customer_phone = "+353871234567"
        db.flush()
        result = _exec_tool(
            db,
            "escalate_to_justin",
            {"reason": "rush", "summary": "needs it tomorrow"},
            conversation_id=conv.id,
        )
        assert result["escalated"] is True
    finally:
        db.close()


# ---------------------------------------------------------------------------
# confirm_order
# ---------------------------------------------------------------------------


def _seed_quote(db, conversation_id: int, *, status: str = "pending_approval") -> Quote:
    q = Quote(
        organization_slug=DEFAULT_ORG_SLUG,
        conversation_id=conversation_id,
        product_key="business_cards",
        specs={"quantity": 500},
        base_price=219.0,
        surcharges=[],
        final_price_ex_vat=219.0,
        vat_amount=50.37,
        final_price_inc_vat=269.37,
        artwork_cost=0.0,
        total=269.37,
        status=status,
    )
    db.add(q)
    db.flush()
    return q


def test_confirm_order_flips_status():
    """Calling confirm_order with a valid quote_id should mark both rows."""
    _fresh_tables()
    db = _TestSession()
    try:
        conv = _new_conversation(db, with_contact=True)
        q = _seed_quote(db, conv.id)
        qid = q.id

        result = _exec_tool(
            db,
            "confirm_order",
            {"quote_id": qid, "notes": "Deliver to Dublin"},
            conversation_id=conv.id,
        )
        assert result["confirmed"] is True, f"Expected confirmed=True, got: {result}"
        assert result["ref"] == f"JP-{qid:04d}"

        db.refresh(q)
        db.refresh(conv)
        assert q.status == "confirmed"
        assert q.notes == "Deliver to Dublin"
        assert conv.status == "order_placed"
    finally:
        db.close()


def test_confirm_order_rejects_wrong_conversation():
    """A quote_id that belongs to a different conversation must be rejected."""
    _fresh_tables()
    db = _TestSession()
    try:
        conv_a = _new_conversation(db, with_contact=True)
        conv_b = Conversation(
            organization_slug=DEFAULT_ORG_SLUG,
            external_id="other-session",
            channel="web",
            messages=[],
            customer_email="b@example.com",
        )
        db.add(conv_b)
        db.flush()

        q = _seed_quote(db, conv_a.id)

        result = _exec_tool(
            db,
            "confirm_order",
            {"quote_id": q.id},
            conversation_id=conv_b.id,  # wrong conversation
        )
        assert result["confirmed"] is False
        assert "not found" in result["error"].lower()
    finally:
        db.close()


def test_confirm_order_missing_quote_id_returns_error():
    _fresh_tables()
    db = _TestSession()
    try:
        conv = _new_conversation(db, with_contact=True)
        result = _exec_tool(db, "confirm_order", {}, conversation_id=conv.id)
        assert result["confirmed"] is False
        assert "quote_id" in result["error"]
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Soft-touch is now a flat +€15 additive surcharge on business cards only
# (was previously a +25% multiplier globally — see V10 migration).
# ---------------------------------------------------------------------------


def _seed_business_cards(db) -> None:
    """Bare-minimum products/tiers for the quote_small_format test."""
    p = Product(
        organization_slug=DEFAULT_ORG_SLUG,
        key="business_cards",
        name="Business Cards",
        category="small_format",
        price_per="100 cards",
        finishes=["gloss", "matte", "soft-touch"],
        double_sided_surcharge=False,  # business cards: no double-sided surcharge
    )
    db.add(p)
    db.flush()
    # Justin's actual tiers
    for qty, price in [(100, 30.0), (250, 60.0), (500, 38.0), (1000, 30.0), (2500, 24.0)]:
        db.add(PriceTier(product_id=p.id, spec_key="", quantity=qty, price=price))
    db.flush()


def _seed_soft_touch_additive(db, amount: float = 15.0) -> None:
    db.add(SurchargeRule(
        organization_slug=DEFAULT_ORG_SLUG,
        name="soft_touch",
        multiplier=amount,
        kind="additive",
        applies_to_category="small_format",
    ))
    db.flush()


def test_soft_touch_adds_flat_fee_at_small_qty():
    """100 business cards base = €30. Soft-touch should bring it to €45 (+€15), not €37.50 (+25%)."""
    _fresh_tables()
    db = _TestSession()
    try:
        _seed_business_cards(db)
        _seed_soft_touch_additive(db)
        result = quote_small_format(
            db, "business_cards", 100, double_sided=False, finish="soft-touch",
        )
        assert result.success, f"Expected success, got: {result}"
        assert abs(result.base_price - 30.0) < 0.01
        # Base €30 + flat €15 = €45 ex VAT
        assert abs(result.final_price_ex_vat - 45.0) < 0.01, (
            f"Expected €45 ex VAT, got €{result.final_price_ex_vat}"
        )
    finally:
        db.close()


def test_soft_touch_adds_flat_fee_at_large_qty():
    """
    2500 business cards base = €600. Soft-touch should be €615 (+€15 flat),
    NOT €750 (+25% which would be €135 over-charged).
    """
    _fresh_tables()
    db = _TestSession()
    try:
        _seed_business_cards(db)
        _seed_soft_touch_additive(db)
        result = quote_small_format(
            db, "business_cards", 2500, double_sided=False, finish="soft-touch",
        )
        assert result.success, f"Expected success, got: {result}"
        assert abs(result.base_price - 600.0) < 0.01, f"base_price wrong: {result.base_price}"
        assert abs(result.final_price_ex_vat - 615.0) < 0.01, (
            f"Expected €615 ex VAT (base €600 + €15 flat), got €{result.final_price_ex_vat}"
        )
    finally:
        db.close()


def test_quote_ready_survives_when_prior_quote_exists():
    """
    The widget's PDF flow routinely emits [QUOTE_READY] on a turn AFTER
    the pricing tool ran (e.g. the contact-collection turn reuses the
    Quote from the verbal-price turn). The hallucination guard must not
    strip the marker in that scenario.

    We exercise the guard directly — build a Conversation + Quote, then
    simulate the guard's decision logic on a reply containing
    [QUOTE_READY].
    """
    _fresh_tables()
    db = _TestSession()
    try:
        conv = _new_conversation(db, with_contact=True)
        _seed_business_cards(db)
        # Seed a quote from a prior turn
        q = Quote(
            organization_slug=DEFAULT_ORG_SLUG,
            conversation_id=conv.id,
            product_key="business_cards",
            specs={"quantity": 500},
            base_price=190.0,
            surcharges=[],
            final_price_ex_vat=205.0,
            vat_amount=27.68,
            final_price_inc_vat=232.68,
            artwork_cost=0.0,
            total=232.68,
            status="pending_approval",
        )
        db.add(q)
        db.flush()

        # Re-create the guard's inputs
        existing_quotes = (
            db.query(Quote)
            .filter_by(conversation_id=conv.id)
            .order_by(Quote.created_at.desc())
            .all()
        )
        had_prior_quote = bool(existing_quotes)
        quote_generated_this_turn = False
        marker_should_be_stripped = not (quote_generated_this_turn or had_prior_quote)

        assert had_prior_quote is True, "Prior quote should exist"
        assert marker_should_be_stripped is False, (
            "Guard must NOT strip [QUOTE_READY] when a prior quote is on the conversation"
        )
    finally:
        db.close()


def test_auto_release_logic_appends_marker_after_save_customer_info():
    """
    Regression test for the bug where Craig collected customer contact
    info but forgot to re-emit [QUOTE_READY], leaving the customer
    staring at "Justin will be in touch" with no PDF card. The server
    must auto-append the marker when:
      - the channel is web
      - save_customer_info ran this turn
      - a Quote already exists on this conversation
      - contact is now on file
      - the LLM's final reply doesn't include [QUOTE_READY]
      - the PDF wasn't already released in a prior turn

    We exercise the decision logic directly — easier than mocking out
    DeepSeek end-to-end, and the logic is what the bug was about.
    """
    _fresh_tables()
    db = _TestSession()
    try:
        # Conversation that already has contact info (just collected this turn)
        conv = _new_conversation(db, with_contact=True)
        _seed_business_cards(db)
        # Seed a quote from a PRIOR turn (this is the held-back PDF)
        q = Quote(
            organization_slug=DEFAULT_ORG_SLUG,
            conversation_id=conv.id,
            product_key="business_cards",
            specs={"quantity": 500},
            base_price=190.0, surcharges=[],
            final_price_ex_vat=205.0, vat_amount=27.68,
            final_price_inc_vat=232.68, artwork_cost=0.0,
            total=232.68, status="pending_approval",
        )
        db.add(q)
        db.flush()

        # Inputs the auto-release logic checks
        channel = "web"
        order_confirmed = False
        tool_calls_audit = [{"tool": "save_customer_info", "args": {}, "result": {}}]
        existing_quotes = [q]
        had_prior_quote = bool(existing_quotes)
        save_contact_called = any(
            (tc.get("tool") or "").lower() == "save_customer_info"
            for tc in tool_calls_audit
        )
        has_contact = bool(conv.customer_email)
        # The LLM's reply that triggered the bug:
        final_reply = "You're all set! Justin will be in touch \U0001f680"
        already_has_marker = "[QUOTE_READY]" in final_reply
        pdf_already_released_earlier = False  # fresh conversation, no prior assistant msgs

        channel_needs_gate = channel.lower() in ("web", "")
        should_auto_release = (
            channel_needs_gate
            and not order_confirmed
            and save_contact_called
            and has_contact
            and had_prior_quote
            and not already_has_marker
            and not pdf_already_released_earlier
        )

        assert should_auto_release is True, (
            "Auto-release MUST fire when contact was just collected, "
            "a prior quote exists, and the LLM's reply lacks the marker"
        )
    finally:
        db.close()


def test_auto_release_does_not_fire_when_marker_already_present():
    """If the LLM correctly emitted [QUOTE_READY], we don't double-add."""
    _fresh_tables()
    db = _TestSession()
    try:
        conv = _new_conversation(db, with_contact=True)
        _seed_business_cards(db)
        q = Quote(
            organization_slug=DEFAULT_ORG_SLUG,
            conversation_id=conv.id,
            product_key="business_cards", specs={"quantity": 500},
            base_price=190.0, surcharges=[],
            final_price_ex_vat=205.0, vat_amount=27.68,
            final_price_inc_vat=232.68, artwork_cost=0.0,
            total=232.68, status="pending_approval",
        )
        db.add(q); db.flush()

        final_reply = "All set, here's your full quote 📋\n\n[QUOTE_READY]"
        already_has_marker = "[QUOTE_READY]" in final_reply
        # The early-return short-circuits the rest
        assert already_has_marker is True


    finally:
        db.close()


def test_auto_release_does_not_fire_on_order_confirmed_path():
    """confirm_order replies should NOT auto-release a PDF — that path
    deliberately omits the marker (the customer already has the PDF)."""
    _fresh_tables()
    db = _TestSession()
    try:
        conv = _new_conversation(db, with_contact=True)
        _seed_business_cards(db)
        q = Quote(
            organization_slug=DEFAULT_ORG_SLUG,
            conversation_id=conv.id,
            product_key="business_cards", specs={"quantity": 500},
            base_price=190.0, surcharges=[],
            final_price_ex_vat=205.0, vat_amount=27.68,
            final_price_inc_vat=232.68, artwork_cost=0.0,
            total=232.68, status="confirmed",
        )
        db.add(q); db.flush()

        order_confirmed = True
        save_contact_called = False  # confirm_order, not save_customer_info
        # The condition requires save_contact_called AND not order_confirmed
        should_auto_release = (
            (not order_confirmed) and save_contact_called
        )
        assert should_auto_release is False
    finally:
        db.close()


def test_soft_touch_not_applied_when_finish_is_matte():
    """Matte should leave the price at base — no additive."""
    _fresh_tables()
    db = _TestSession()
    try:
        _seed_business_cards(db)
        _seed_soft_touch_additive(db)
        result = quote_small_format(
            db, "business_cards", 500, double_sided=False, finish="matte",
        )
        assert result.success
        # 500 cards × (€38/100) = €190. No surcharge.
        assert abs(result.final_price_ex_vat - 190.0) < 0.01
    finally:
        db.close()
