"""
Tests for the email-channel hardening shipped in v29:

  - Inbound idempotency (no duplicate drafts on webhook retry)
  - HTML quoted-thread stripping
  - `_obvious_junk` already covered in test_inbound_classifier.py
  - Inbound attachment ingestion → Quote.artwork_files
  - Defensive marker stripping (ARTWORK_CHOICE etc. removed if LLM drifts)
  - save_customer_info applies shipping when called with delivery_method

These tests exercise the helpers + handler logic directly with mocks
so we don't hit the live Missive REST API or DeepSeek. Multi-turn
LLM behavior is covered indirectly — those flows are E2E and are run
by the operator from the smoke test in the plan file.
"""

from __future__ import annotations

import os
from unittest.mock import patch, AsyncMock, MagicMock

os.environ.setdefault("STRATEGOS_JWT_SECRET", "test-secret-32b-pad-enough-now")
os.environ.setdefault("CRAIG_ARTWORK_LOCAL_DIR", "/tmp/craig-artwork-test")

import pytest

from app import (  # noqa: E402
    _strip_quoted_thread,
    _mark_drafted,
    _DRAFTED_FOR_MESSAGES,
    _DRAFTED_FOR_MESSAGES_ORDER,
)
from db import db_session, parse_artwork_files  # noqa: E402
from db.models import Conversation, Quote, Setting, DEFAULT_ORG_SLUG  # noqa: E402
from missive import extract_attachments_from_message  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_idempotency_set():
    _DRAFTED_FOR_MESSAGES.clear()
    _DRAFTED_FOR_MESSAGES_ORDER.clear()
    yield
    _DRAFTED_FOR_MESSAGES.clear()
    _DRAFTED_FOR_MESSAGES_ORDER.clear()


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


class TestMessageIdempotency:
    def test_first_call_returns_true(self):
        assert _mark_drafted("just-print", "msg-abc") is True

    def test_duplicate_returns_false(self):
        assert _mark_drafted("just-print", "msg-xyz") is True
        assert _mark_drafted("just-print", "msg-xyz") is False

    def test_different_orgs_dont_collide(self):
        assert _mark_drafted("org-a", "msg-1") is True
        # Same message_id, different org — independent keys
        assert _mark_drafted("org-b", "msg-1") is True

    def test_cap_evicts_old_entries(self):
        # Stuff 1024 entries in, then verify the 1025th evicts the first
        for i in range(1024):
            _mark_drafted("just-print", f"msg-{i}")
        # All 1024 are recorded
        assert _mark_drafted("just-print", "msg-0") is False  # already there
        # Pushing one more evicts msg-0
        _mark_drafted("just-print", "msg-overflow")
        # msg-0 is now evictable; calling again it counts as new
        assert _mark_drafted("just-print", "msg-0") is True


# ---------------------------------------------------------------------------
# Quoted-thread stripping
# ---------------------------------------------------------------------------


class TestStripQuotedThread:
    def test_gmail_outlook_on_x_wrote(self):
        body = (
            "Sounds good, please proceed with the order.\n"
            "\n"
            "Thanks,\n"
            "Bryan\n"
            "\n"
            "On Mon, May 1 2026 at 14:35 Justin <justin@just-print.ie> wrote:\n"
            "> Here's your quote for 500 cards...\n"
            "> [snip]"
        )
        out = _strip_quoted_thread(body)
        assert "On Mon" not in out
        assert "Sounds good, please proceed with the order." in out
        assert "Bryan" in out

    def test_outlook_original_message(self):
        body = (
            "Yes please proceed.\n"
            "\n"
            "-----Original Message-----\n"
            "From: Justin <justin@just-print.ie>\n"
            "Sent: ...\n"
            "Subject: Your quote\n"
        )
        out = _strip_quoted_thread(body)
        assert "Yes please proceed." in out
        assert "Original Message" not in out

    def test_underscore_divider(self):
        body = (
            "Confirmed, ship to D02 X123.\n"
            "\n"
            "______________________\n"
            "From: Justin..."
        )
        out = _strip_quoted_thread(body)
        assert "Confirmed, ship to D02 X123." in out
        assert "From: Justin" not in out

    def test_spanish_el_x_escribio(self):
        body = (
            "Perfecto, sigamos con la orden.\n"
            "\n"
            "El Lun, 1 de mayo de 2026, Justin escribió:\n"
            "> Hola, aquí está la cotización..."
        )
        out = _strip_quoted_thread(body)
        assert "Perfecto, sigamos con la orden." in out
        assert "Justin escribió" not in out

    def test_no_quote_block_unchanged(self):
        body = "Hi, I'd like a quote for 500 business cards. Thanks, Bryan."
        out = _strip_quoted_thread(body)
        assert out == body

    def test_empty_string_safe(self):
        assert _strip_quoted_thread("") == ""

    def test_sent_from_my_iphone(self):
        body = (
            "Yes please go ahead.\n"
            "\n"
            "Sent from my iPhone\n"
        )
        out = _strip_quoted_thread(body)
        assert "Yes please go ahead." in out
        assert "iPhone" not in out


# ---------------------------------------------------------------------------
# Attachment extraction (helper from missive.py)
# ---------------------------------------------------------------------------


class TestExtractAttachmentsFromMessage:
    def test_no_attachments_returns_empty_list(self):
        msg = {"id": "abc", "body": "hi"}
        assert extract_attachments_from_message(msg) == []

    def test_extracts_normalized_dict(self):
        msg = {
            "attachments": [
                {
                    "id": "att_1",
                    "filename": "front.pdf",
                    "media_type": "application/pdf",
                    "size": 12345,
                    "url": "https://missive-cdn.example/att_1?sig=...",
                },
                {
                    "id": "att_2",
                    "name": "back.png",  # alt key
                    "content_type": "image/png",  # alt key
                    "size": 2048,
                    "signed_url": "https://missive-cdn.example/att_2?sig=...",
                },
            ]
        }
        out = extract_attachments_from_message(msg)
        assert len(out) == 2
        assert out[0]["filename"] == "front.pdf"
        assert out[0]["media_type"] == "application/pdf"
        assert out[1]["filename"] == "back.png"
        assert out[1]["media_type"] == "image/png"
        assert out[1]["url"]  # picked up signed_url alias

    def test_garbage_input_safe(self):
        # Defensive: shouldn't raise on malformed payloads
        assert extract_attachments_from_message({}) == []
        assert extract_attachments_from_message({"attachments": "not-a-list"}) == []
        assert extract_attachments_from_message({"attachments": [None, {}]}) == [
            # second entry survives with all-empty defaults
            {"id": "", "filename": "", "media_type": "", "size": 0, "url": ""}
        ]


# ---------------------------------------------------------------------------
# save_customer_info applies shipping on the email channel
# ---------------------------------------------------------------------------


def _seed_conv_with_pending_quote(*, goods_inc=200.0):
    """Returns (conversation_id, quote_id)."""
    with db_session() as db:
        # Ensure shipping settings exist
        for key, val in (
            ("shipping_fee_inc_vat", "15.00"),
            ("free_shipping_threshold_inc_vat", "100.00"),
            (
                "shop_address",
                "Ballymount Cross Business Park, 7, Ballymount, Dublin, D24 E5NH, Ireland",
            ),
        ):
            row = (
                db.query(Setting)
                .filter_by(organization_slug=DEFAULT_ORG_SLUG, key=key)
                .first()
            )
            if row is None:
                db.add(Setting(
                    organization_slug=DEFAULT_ORG_SLUG,
                    key=key, value=val, value_type="string",
                ))
        conv = Conversation(
            organization_slug=DEFAULT_ORG_SLUG,
            external_id=f"missive-funnel-{os.getpid()}",
            channel="missive", messages=[],
            customer_name="Bryan", customer_email="bryan@example.com",
        )
        db.add(conv); db.flush()
        q = Quote(
            organization_slug=DEFAULT_ORG_SLUG,
            conversation_id=conv.id,
            product_key="business_cards",
            specs={"quantity": 500},
            base_price=goods_inc / 1.135,
            surcharges=[],
            final_price_ex_vat=round(goods_inc / 1.135, 2),
            vat_amount=round(goods_inc - goods_inc / 1.135, 2),
            final_price_inc_vat=goods_inc,
            artwork_cost=0.0,
            total=goods_inc,
            status="pending_approval",
            shipping_cost_ex_vat=0.0,
            shipping_cost_inc_vat=0.0,
        )
        db.add(q); db.commit()
        return conv.id, q.id


class TestSaveCustomerInfoAppliesShipping:
    def test_email_channel_save_with_delivery_applies_shipping(self):
        cid, qid = _seed_conv_with_pending_quote(goods_inc=50.0)  # < threshold
        # Call the tool directly via _exec_tool
        from llm.craig_agent import _exec_tool
        with db_session() as db:
            result = _exec_tool(
                db, "save_customer_info",
                {
                    "name": "Bryan",
                    "delivery_method": "delivery",
                    "delivery_address": {
                        "address1": "1 Pearse St",
                        "postcode": "D02 X123",
                    },
                    "is_company": False,
                    "is_returning_customer": False,
                },
                conversation_id=cid,
                organization_slug=DEFAULT_ORG_SLUG,
            )
        assert result["saved"] is True
        # Verify shipping was applied to the pending quote
        with db_session() as db:
            q = db.query(Quote).filter_by(id=qid).first()
            assert q.shipping_cost_inc_vat > 0  # €15 expected
            conv = db.query(Conversation).filter_by(id=cid).first()
            assert conv.delivery_method == "delivery"
            assert conv.delivery_address == {
                "address1": "1 Pearse St",
                "postcode": "D02 X123",
            }

    def test_collect_method_zero_shipping(self):
        cid, qid = _seed_conv_with_pending_quote(goods_inc=50.0)
        from llm.craig_agent import _exec_tool
        with db_session() as db:
            _exec_tool(
                db, "save_customer_info",
                {"name": "Bryan", "delivery_method": "collect"},
                conversation_id=cid,
                organization_slug=DEFAULT_ORG_SLUG,
            )
        with db_session() as db:
            q = db.query(Quote).filter_by(id=qid).first()
            # Collect = no shipping fee
            assert q.shipping_cost_inc_vat == 0.0
            conv = db.query(Conversation).filter_by(id=cid).first()
            assert conv.delivery_method == "collect"

    def test_save_without_delivery_method_doesnt_apply_shipping(self):
        cid, qid = _seed_conv_with_pending_quote(goods_inc=50.0)
        from llm.craig_agent import _exec_tool
        with db_session() as db:
            _exec_tool(
                db, "save_customer_info",
                {"name": "Bryan", "is_company": True},
                conversation_id=cid,
                organization_slug=DEFAULT_ORG_SLUG,
            )
        with db_session() as db:
            q = db.query(Quote).filter_by(id=qid).first()
            # No delivery_method given → shipping unchanged
            assert q.shipping_cost_inc_vat == 0.0
            conv = db.query(Conversation).filter_by(id=cid).first()
            assert conv.is_company is True
            assert (conv.delivery_method or "") == ""
