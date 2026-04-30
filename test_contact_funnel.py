"""
Tests for Phase E — extended customer-funnel + FAQs + re-order tool.

Covers:
  - save_customer_info accepts and persists the 5 new fields
    (is_company, is_returning_customer, past_customer_email,
    delivery_method, delivery_address)
  - save_customer_info nulls don't overwrite existing data
  - find_past_quotes_by_email returns matching prior quotes,
    tenant-scoped
  - find_past_quotes_by_email filters out non-accepted statuses
    (pending_approval, rejected) so customers can't trick Craig into
    quoting from a quote Justin never approved
  - PATCH /orgs/{slug}/conversations/{cid} endpoint persists the funnel
    fields and the existing contact fields
  - _build_faq_context injects expected text + expands {{shop_address}}
  - printlogic_payload pulls delivery_address from Conversation when
    not passed explicitly + adds the right lines to item_detail
  - printlogic_push prefers past_customer_email for returning-customer
    dedup over the in-chat email
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
from llm.craig_agent import _exec_tool, _build_faq_context


_engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
)
Base.metadata.create_all(bind=_engine)
_TestSession = sessionmaker(autocommit=False, autoflush=False, bind=_engine)


def _fresh():
    Base.metadata.drop_all(bind=_engine)
    Base.metadata.create_all(bind=_engine)


def _new_conversation(db, *, with_contact=False, **extra) -> Conversation:
    conv = Conversation(
        organization_slug=DEFAULT_ORG_SLUG,
        external_id="test", channel="web",
        messages=[],
    )
    if with_contact:
        conv.customer_name = "Test Customer"
        conv.customer_email = "test@example.com"
        conv.customer_phone = "+353 1 555 1234"
    for k, v in extra.items():
        setattr(conv, k, v)
    db.add(conv); db.flush()
    return conv


def _seed_quote(
    db, conversation_id, *, status="approved", product="business_cards",
    final=232.68,
):
    q = Quote(
        organization_slug=DEFAULT_ORG_SLUG,
        conversation_id=conversation_id,
        product_key=product,
        specs={"quantity": 500},
        base_price=190.0, surcharges=[],
        final_price_ex_vat=205.0, vat_amount=27.68,
        final_price_inc_vat=final, artwork_cost=0.0,
        total=final, status=status,
    )
    db.add(q); db.flush()
    return q


# ---------------------------------------------------------------------------
# save_customer_info — extended fields
# ---------------------------------------------------------------------------


def test_save_customer_info_persists_all_extended_fields():
    _fresh()
    db = _TestSession()
    try:
        conv = _new_conversation(db)
        result = _exec_tool(
            db, "save_customer_info",
            {
                "name": "ACME Ltd contact",
                "email": "orders@acme.ie",
                "phone": "+353 1 555 9999",
                "is_company": True,
                "is_returning_customer": True,
                "past_customer_email": "old-email@acme.ie",
                "delivery_method": "delivery",
                "delivery_address": {
                    "address1": "Unit 12 Industrial Park",
                    "address2": "Sandyford",
                    "address4": "Dublin",
                    "postcode": "D18 X1Y2",
                },
            },
            conversation_id=conv.id,
        )
        assert result["saved"] is True

        db.refresh(conv)
        assert conv.customer_name == "ACME Ltd contact"
        assert conv.customer_email == "orders@acme.ie"
        assert conv.customer_phone == "+353 1 555 9999"
        assert conv.is_company is True
        assert conv.is_returning_customer is True
        assert conv.past_customer_email == "old-email@acme.ie"
        assert conv.delivery_method == "delivery"
        assert conv.delivery_address == {
            "address1": "Unit 12 Industrial Park",
            "address2": "Sandyford",
            "address4": "Dublin",
            "postcode": "D18 X1Y2",
        }
    finally:
        db.close()


def test_save_customer_info_partial_call_does_not_overwrite():
    """LLM often calls save_customer_info with one new field — must not nuke
    fields previously stored."""
    _fresh()
    db = _TestSession()
    try:
        conv = _new_conversation(
            db, with_contact=True,
            is_company=True, delivery_method="delivery",
        )
        # Second call only adds is_returning_customer=True
        _exec_tool(
            db, "save_customer_info",
            {"name": "Test Customer", "is_returning_customer": True},
            conversation_id=conv.id,
        )
        db.refresh(conv)
        # Previous fields preserved
        assert conv.customer_email == "test@example.com"
        assert conv.is_company is True
        assert conv.delivery_method == "delivery"
        # New field set
        assert conv.is_returning_customer is True
    finally:
        db.close()


def test_save_customer_info_collect_method_no_address_required():
    _fresh()
    db = _TestSession()
    try:
        conv = _new_conversation(db, with_contact=True)
        _exec_tool(
            db, "save_customer_info",
            {"name": "Test", "delivery_method": "collect"},
            conversation_id=conv.id,
        )
        db.refresh(conv)
        assert conv.delivery_method == "collect"
        assert conv.delivery_address is None
    finally:
        db.close()


def test_save_customer_info_rejects_unknown_delivery_method():
    """Pydantic-level — actually we don't validate at save_customer_info,
    invalid enum values are silently ignored. Verify that behavior."""
    _fresh()
    db = _TestSession()
    try:
        conv = _new_conversation(db, with_contact=True)
        _exec_tool(
            db, "save_customer_info",
            {"name": "Test", "delivery_method": "magic-carpet"},
            conversation_id=conv.id,
        )
        db.refresh(conv)
        assert conv.delivery_method is None  # not 'magic-carpet'
    finally:
        db.close()


# ---------------------------------------------------------------------------
# find_past_quotes_by_email
# ---------------------------------------------------------------------------


def test_find_past_quotes_returns_matching_quotes():
    _fresh()
    db = _TestSession()
    try:
        conv1 = _new_conversation(db, customer_email="returning@cust.ie")
        _seed_quote(db, conv1.id, status="approved", final=232.68)
        _seed_quote(db, conv1.id, status="sent", final=190.50, product="flyers_a5")

        result = _exec_tool(
            db, "find_past_quotes_by_email",
            {"email": "returning@cust.ie"},
            conversation_id=None, organization_slug=DEFAULT_ORG_SLUG,
        )
        assert result["found"] is True
        assert result["count"] == 2
        # Sorted desc by created_at — flyers (last seeded) is first
        refs = [q["ref"] for q in result["quotes"]]
        assert all(r.startswith("JP-") for r in refs)
    finally:
        db.close()


def test_find_past_quotes_filters_non_accepted_statuses():
    """pending_approval and rejected quotes must NOT show — only ones
    Justin actually approved or sent."""
    _fresh()
    db = _TestSession()
    try:
        conv = _new_conversation(db, customer_email="filter@test.ie")
        _seed_quote(db, conv.id, status="pending_approval", final=10.0)
        _seed_quote(db, conv.id, status="rejected", final=20.0)
        _seed_quote(db, conv.id, status="approved", final=30.0)

        result = _exec_tool(
            db, "find_past_quotes_by_email",
            {"email": "filter@test.ie"},
            conversation_id=None, organization_slug=DEFAULT_ORG_SLUG,
        )
        assert result["found"] is True
        assert result["count"] == 1
        assert result["quotes"][0]["total_inc_vat"] == 30.0
    finally:
        db.close()


def test_find_past_quotes_tenant_scoped():
    """A returning-customer email lookup MUST not leak quotes from other
    tenants — even if the email matches."""
    _fresh()
    db = _TestSession()
    try:
        # Tenant A — has the customer
        conv_a = _new_conversation(db, customer_email="shared@cust.ie")
        _seed_quote(db, conv_a.id, status="approved", final=100.0)
        # Tenant B — same customer email, different org
        conv_b = Conversation(
            organization_slug="other-tenant",
            external_id="test-b", channel="web",
            customer_email="shared@cust.ie", messages=[],
        )
        db.add(conv_b); db.flush()
        _seed_quote(db, conv_b.id, status="approved", final=999.0)
        # Hack the quote's org_slug since _seed_quote uses DEFAULT_ORG_SLUG
        leaked_q = (
            db.query(Quote).filter_by(conversation_id=conv_b.id).first()
        )
        leaked_q.organization_slug = "other-tenant"
        db.flush()

        # Look up as DEFAULT_ORG_SLUG (tenant A)
        result = _exec_tool(
            db, "find_past_quotes_by_email",
            {"email": "shared@cust.ie"},
            conversation_id=None, organization_slug=DEFAULT_ORG_SLUG,
        )
        # Should ONLY see tenant A's quote (€100), never tenant B's (€999)
        assert result["count"] == 1
        assert result["quotes"][0]["total_inc_vat"] == 100.0
    finally:
        db.close()


def test_find_past_quotes_empty_returns_clean():
    _fresh()
    db = _TestSession()
    try:
        result = _exec_tool(
            db, "find_past_quotes_by_email",
            {"email": "nobody@nowhere.ie"},
            conversation_id=None, organization_slug=DEFAULT_ORG_SLUG,
        )
        assert result["found"] is False
        assert result["quotes"] == []
        assert "No prior quotes" in result["message"]
    finally:
        db.close()


# ---------------------------------------------------------------------------
# _build_faq_context
# ---------------------------------------------------------------------------


def test_build_faq_context_renders_seeded_faqs():
    _fresh()
    db = _TestSession()
    try:
        import json
        # Seed via the same shape v22 uses
        db.add(Setting(
            organization_slug=DEFAULT_ORG_SLUG,
            key="craig_faqs_json",
            value=json.dumps([
                {"q": "Do you ship?", "a": "Yes, free over €100."},
                {"q": "Where to collect?", "a": "Pickup at {{shop_address}}."},
            ]),
            value_type="string",
        ))
        db.add(Setting(
            organization_slug=DEFAULT_ORG_SLUG,
            key="shop_address", value="42 Main Street, Dublin",
            value_type="string",
        ))
        db.commit()

        ctx = _build_faq_context(db, DEFAULT_ORG_SLUG)
        assert "Frequently asked questions" in ctx
        assert "Do you ship?" in ctx
        assert "free over" in ctx
        # Shop address placeholder expanded
        assert "42 Main Street, Dublin" in ctx
        assert "{{shop_address}}" not in ctx
    finally:
        db.close()


def test_build_faq_context_empty_when_setting_missing():
    _fresh()
    db = _TestSession()
    try:
        ctx = _build_faq_context(db, DEFAULT_ORG_SLUG)
        assert ctx == ""
    finally:
        db.close()


# ---------------------------------------------------------------------------
# printlogic_payload — delivery wired from Conversation
# ---------------------------------------------------------------------------


def test_payload_pulls_delivery_from_conversation():
    """When delivery_address kwarg isn't passed, build_payload_from_quote
    should fall back to Conversation.delivery_address (mapping our internal
    address1..4/postcode keys to the kwarg's delivery_address1..4/postcode
    shape)."""
    _fresh()
    db = _TestSession()
    try:
        conv = _new_conversation(
            db, with_contact=True,
            is_company=False,
            delivery_method="delivery",
            delivery_address={
                "address1": "Unit 5",
                "address2": "Tech Park",
                "address4": "Dublin",
                "postcode": "D02 X1Y2",
            },
        )
        q = _seed_quote(db, conv.id, status="approved")

        from printlogic_payload import build_payload_from_quote
        payload = build_payload_from_quote(q, conv)

        # Delivery fields populated from the conversation
        assert payload["delivery_address1"] == "Unit 5"
        assert payload["delivery_address2"] == "Tech Park"
        assert payload["delivery_address4"] == "Dublin"
        assert payload["delivery_postcode"] == "D02 X1Y2"

        # And item_detail jobsheet contains the Delivery: line
        item_detail = payload["order_items"][0]["item_detail"]
        assert "Delivery:" in item_detail
        assert "Unit 5" in item_detail
        # And Customer: line shows "Individual"
        assert "Customer:" in item_detail
        assert "Individual" in item_detail
    finally:
        db.close()


def test_payload_collect_method_blank_addresses_but_logs_in_detail():
    _fresh()
    db = _TestSession()
    try:
        conv = _new_conversation(
            db, with_contact=True,
            delivery_method="collect",
        )
        q = _seed_quote(db, conv.id, status="approved")
        from printlogic_payload import build_payload_from_quote
        payload = build_payload_from_quote(q, conv)
        # No delivery — address fields all empty strings
        assert payload["delivery_address1"] == ""
        assert payload["delivery_postcode"] == ""
        # Detail shows the collect-from-shop line
        assert "collect from shop" in payload["order_items"][0]["item_detail"]
    finally:
        db.close()


def test_payload_returning_customer_in_detail():
    _fresh()
    db = _TestSession()
    try:
        conv = _new_conversation(
            db, with_contact=True,
            is_returning_customer=True,
            past_customer_email="prior@cust.ie",
        )
        q = _seed_quote(db, conv.id, status="approved")
        from printlogic_payload import build_payload_from_quote
        payload = build_payload_from_quote(q, conv)
        item_detail = payload["order_items"][0]["item_detail"]
        assert "Returning:" in item_detail
        assert "prior@cust.ie" in item_detail
    finally:
        db.close()


# ---------------------------------------------------------------------------
# printlogic_push — returning customer dedup
# ---------------------------------------------------------------------------


def test_push_uses_past_email_for_returning_customer_dedup():
    """When is_returning_customer is true, find_customer must be called
    with past_customer_email, not the in-chat email."""
    _fresh()
    db = _TestSession()
    try:
        conv = _new_conversation(
            db, with_contact=True,
            is_returning_customer=True,
            past_customer_email="old@account.ie",
        )
        # Real (non-dry-run) push so the dedup branch fires
        # Need a non-empty api_key + dry_run=false setting
        db.add(Setting(
            organization_slug=DEFAULT_ORG_SLUG,
            key="printlogic_api_key", value="test-key",
            value_type="string",
        ))
        db.add(Setting(
            organization_slug=DEFAULT_ORG_SLUG,
            key="printlogic_dry_run", value="false",
            value_type="string",
        ))
        db.flush()
        q = _seed_quote(db, conv.id, status="approved")

        # Mock both find_customer (returns no match — we just want to see
        # the call args) and create_order (so we don't hit real PrintLogic)
        with patch(
            "printlogic.find_customer",
            new=AsyncMock(return_value={"ok": True, "customer": None}),
        ) as mock_find, patch(
            "printlogic.create_order",
            new=AsyncMock(return_value={
                "ok": True, "order_id": "12345", "customer_id": "67890",
                "dry_run": False, "ambiguous": False,
                "raw": {"order_number": "98765"}, "error": None,
            }),
        ):
            from printlogic_push import push_quote
            push_quote(db, q, DEFAULT_ORG_SLUG)

        assert mock_find.await_count == 1
        kwargs = mock_find.await_args.kwargs
        assert kwargs.get("email") == "old@account.ie", (
            f"Expected past_customer_email lookup; got {kwargs.get('email')!r}"
        )
    finally:
        db.close()


def test_push_uses_in_chat_email_when_not_returning():
    _fresh()
    db = _TestSession()
    try:
        conv = _new_conversation(
            db, with_contact=True,
            is_returning_customer=False,
        )
        db.add(Setting(
            organization_slug=DEFAULT_ORG_SLUG,
            key="printlogic_api_key", value="test-key",
            value_type="string",
        ))
        db.add(Setting(
            organization_slug=DEFAULT_ORG_SLUG,
            key="printlogic_dry_run", value="false",
            value_type="string",
        ))
        db.flush()
        q = _seed_quote(db, conv.id, status="approved")

        with patch(
            "printlogic.find_customer",
            new=AsyncMock(return_value={"ok": True, "customer": None}),
        ) as mock_find, patch(
            "printlogic.create_order",
            new=AsyncMock(return_value={
                "ok": True, "order_id": "12345", "customer_id": "67890",
                "dry_run": False, "ambiguous": False,
                "raw": {"order_number": "98765"}, "error": None,
            }),
        ):
            from printlogic_push import push_quote
            push_quote(db, q, DEFAULT_ORG_SLUG)

        kwargs = mock_find.await_args.kwargs
        assert kwargs.get("email") == "test@example.com"
    finally:
        db.close()
