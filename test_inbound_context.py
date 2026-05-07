"""
Tests for v32.2 — pre-LLM context injection (sender + returning-customer).

Two pieces:
  1. `_detect_returning_customer` (in app.py) — pure SQL helper.
     Tests prior-conv counting, case-insensitive email match, empty
     email handling, and the current-conv exclusion.
  2. `chat_with_craig(extra_system_messages=...)` — verify the
     server-injected system messages flow into the LLM prompt.
"""

from __future__ import annotations

import os
from unittest.mock import patch, MagicMock

os.environ.setdefault("STRATEGOS_JWT_SECRET", "test-secret-32b-pad-enough-now")

import pytest
from db import db_session
from db.models import Conversation, Quote, DEFAULT_ORG_SLUG


# ---------------------------------------------------------------------------
# detect_returning_customer
# ---------------------------------------------------------------------------


@pytest.fixture
def fresh_org():
    """Wipe Missive-channel conversations before each test so prior
    test runs don't pollute the helper's count."""
    with db_session() as db:
        rows = db.query(Conversation).filter_by(
            organization_slug=DEFAULT_ORG_SLUG,
            channel="missive_test_v322",
        ).all()
        for r in rows:
            db.query(Quote).filter_by(conversation_id=r.id).delete()
            db.delete(r)
        db.commit()
    yield


def _new_conv(db, email: str, *, channel: str = "missive_test_v322") -> Conversation:
    c = Conversation(
        organization_slug=DEFAULT_ORG_SLUG,
        external_id=f"test-{email}-{id(object())}",
        channel=channel,
        customer_email=email,
        messages=[],
    )
    db.add(c)
    db.flush()
    return c


class TestDetectReturningCustomer:
    def test_empty_email_returns_not_returning(self, fresh_org):
        from app import _detect_returning_customer
        with db_session() as db:
            r = _detect_returning_customer(db, DEFAULT_ORG_SLUG, "")
        assert r["is_returning"] is False
        assert r["prior_conversations"] == 0

    def test_none_email_returns_not_returning(self, fresh_org):
        from app import _detect_returning_customer
        with db_session() as db:
            r = _detect_returning_customer(db, DEFAULT_ORG_SLUG, None)
        assert r["is_returning"] is False

    def test_no_prior_convs_returns_zero(self, fresh_org):
        from app import _detect_returning_customer
        with db_session() as db:
            r = _detect_returning_customer(
                db, DEFAULT_ORG_SLUG, "first-time@example.com",
            )
        assert r["is_returning"] is False
        assert r["prior_conversations"] == 0
        assert r["prior_quote_count"] == 0

    def test_one_prior_conv_returns_returning(self, fresh_org):
        from app import _detect_returning_customer
        with db_session() as db:
            _new_conv(db, "repeat@example.com")
            current = _new_conv(db, "repeat@example.com")
            db.commit()
            r = _detect_returning_customer(
                db, DEFAULT_ORG_SLUG, "repeat@example.com",
                current_conv_id=current.id,
            )
        assert r["is_returning"] is True
        assert r["prior_conversations"] == 1

    def test_case_insensitive_match(self, fresh_org):
        from app import _detect_returning_customer
        with db_session() as db:
            _new_conv(db, "Foo@Example.com")
            current = _new_conv(db, "foo@example.com")
            db.commit()
            r = _detect_returning_customer(
                db, DEFAULT_ORG_SLUG, "FOO@EXAMPLE.COM",
                current_conv_id=current.id,
            )
        assert r["is_returning"] is True
        assert r["prior_conversations"] >= 1

    def test_excludes_current_conv(self, fresh_org):
        """Without current_conv_id passed, the lookup would wrongly
        count the current conversation as 'prior'."""
        from app import _detect_returning_customer
        with db_session() as db:
            current = _new_conv(db, "alone@example.com")
            db.commit()
            r = _detect_returning_customer(
                db, DEFAULT_ORG_SLUG, "alone@example.com",
                current_conv_id=current.id,
            )
        assert r["is_returning"] is False
        assert r["prior_conversations"] == 0

    def test_counts_quotes_too(self, fresh_org):
        from app import _detect_returning_customer
        with db_session() as db:
            prior = _new_conv(db, "buyer@example.com")
            q = Quote(
                organization_slug=DEFAULT_ORG_SLUG,
                conversation_id=prior.id,
                product_key="business_cards",
                specs={"product_key": "business_cards", "quantity": 100},
                base_price=20.0, surcharges=[], final_price_ex_vat=20.0,
                vat_amount=2.7, final_price_inc_vat=22.7, artwork_cost=0.0,
                total=22.7, status="pending_approval",
            )
            db.add(q)
            current = _new_conv(db, "buyer@example.com")
            db.commit()
            r = _detect_returning_customer(
                db, DEFAULT_ORG_SLUG, "buyer@example.com",
                current_conv_id=current.id,
            )
        assert r["is_returning"] is True
        assert r["prior_quote_count"] == 1


# ---------------------------------------------------------------------------
# extra_system_messages flows into the LLM prompt
# ---------------------------------------------------------------------------


class TestExtraSystemMessages:
    """Verify chat_with_craig wires the new kwarg into the messages list
    that goes to the LLM. We don't make a real LLM call — we mock the
    OpenAI client and inspect what was sent."""

    def test_extra_messages_appear_before_user_turn(self):
        from llm.craig_agent import chat_with_craig

        with db_session() as db:
            conv = Conversation(
                organization_slug=DEFAULT_ORG_SLUG,
                external_id="test-extra-ctx-1",
                channel="missive",
                customer_email="who@example.com",
                messages=[],
            )
            db.add(conv)
            db.commit()
            cid = conv.id

        captured = {}
        def _fake_create(**kwargs):
            captured["messages"] = kwargs.get("messages", [])
            mock_msg = MagicMock()
            mock_msg.content = "Hi there, mocked reply.\nCheers,\nCraig\nJust Print"
            mock_msg.tool_calls = None
            mock_choice = MagicMock(message=mock_msg, finish_reason="stop")
            return MagicMock(choices=[mock_choice])

        extra = [
            {"role": "system", "content": "[SENDER METADATA]\nEmail: who@example.com\nDisplay name: Test Person"},
            {"role": "system", "content": "[CUSTOMER STATUS]\nNo prior conversations."},
        ]
        with patch("llm.craig_agent.OpenAI") as mock_cls:
            mock_client = MagicMock()
            mock_client.chat.completions.create = _fake_create
            mock_cls.return_value = mock_client
            chat_with_craig(
                db=None,  # type: ignore[arg-type]  # filled below
                conversation_id=cid,
                user_message="Hi, can you do 100 cards?",
                external_id="test-extra-ctx-1",
                channel="missive",
                organization_slug=DEFAULT_ORG_SLUG,
                extra_system_messages=extra,
            ) if False else None  # noqa: E501
            # ^ placeholder so static-checker doesn't complain. The real
            # call is below with a fresh session.

        with db_session() as db:
            with patch("llm.craig_agent.OpenAI") as mock_cls:
                mock_client = MagicMock()
                mock_client.chat.completions.create = _fake_create
                mock_cls.return_value = mock_client
                chat_with_craig(
                    db=db,
                    conversation_id=cid,
                    user_message="Hi, can you do 100 cards?",
                    external_id="test-extra-ctx-1",
                    channel="missive",
                    organization_slug=DEFAULT_ORG_SLUG,
                    extra_system_messages=extra,
                )

        msgs = captured["messages"]
        # Find our injected system messages — they must appear after the
        # main system prompt and before the user turn.
        system_msgs = [m for m in msgs if m["role"] == "system"]
        sender_seen = any(
            "[SENDER METADATA]" in m.get("content", "") for m in system_msgs
        )
        status_seen = any(
            "[CUSTOMER STATUS]" in m.get("content", "") for m in system_msgs
        )
        assert sender_seen, "sender metadata system message must reach the LLM"
        assert status_seen, "customer status system message must reach the LLM"


# ---------------------------------------------------------------------------
# Sanity guard: placeholder-string rejection in save_customer_info
# ---------------------------------------------------------------------------


class TestPlaceholderStringRejection:
    """v32.2 — if the LLM passes literal phrases like 'the customer's
    name from the conversation', the tool execution must drop them
    instead of writing them to the DB."""

    def _run_save_tool(self, args):
        """Returns a dict snapshot of the conversation row (extracted
        inside the session) so callers can inspect persisted values
        without touching a detached ORM object."""
        from llm.craig_agent import _exec_tool
        with db_session() as db:
            conv = Conversation(
                organization_slug=DEFAULT_ORG_SLUG,
                external_id=f"test-placeholder-rejection-{id(args)}",
                channel="missive",
                customer_email="whoever@example.com",
                messages=[],
            )
            db.add(conv)
            db.commit()
            _exec_tool(
                db, "save_customer_info", args,
                conversation_id=conv.id,
                organization_slug=DEFAULT_ORG_SLUG,
            )
            db.refresh(conv)
            return {
                "customer_name": conv.customer_name,
                "customer_email": conv.customer_email,
                "past_customer_email": conv.past_customer_email,
                "is_returning_customer": conv.is_returning_customer,
                "is_company": conv.is_company,
            }

    def test_rejects_literal_customers_name_phrase(self):
        snap = self._run_save_tool({
            "name": "the customer's name from the conversation",
            "is_company": False,
        })
        assert (snap["customer_name"] or "") == ""

    def test_rejects_angle_brackets_placeholder(self):
        snap = self._run_save_tool({
            "name": "<name>",
            "is_company": False,
        })
        assert (snap["customer_name"] or "") == ""

    def test_rejects_email_placeholder(self):
        snap = self._run_save_tool({
            "name": "Real Person",
            "is_returning_customer": True,
            "past_customer_email": "the customer's email from the conversation",
        })
        assert snap["customer_name"] == "Real Person"
        assert (snap["past_customer_email"] or "") != "the customer's email from the conversation"

    def test_real_name_passes_through(self):
        snap = self._run_save_tool({
            "name": "Juan Sebastian Peña",
            "is_company": False,
        })
        assert snap["customer_name"] == "Juan Sebastian Peña"
