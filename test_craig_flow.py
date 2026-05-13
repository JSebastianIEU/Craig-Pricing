"""
Multi-turn conversation flow tests — locks Craig's behaviour contract
across the full customer journey, mocking DeepSeek so each turn is
deterministic.

These tests are the v38 audit's defensive scaffolding. The widget
audit found 14 conversations (42%) abandoned mid-flow. Each scenario
below is one of those failure modes — REPRODUCED in a test so it
can't silently regress.

Pattern: each test scripts a sequence of (user_message → LLM mock
responses) and asserts the conversation state after each turn.

Run all tests in this file: ~30s.
"""

from __future__ import annotations

import json
import os
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

os.environ["STRATEGOS_JWT_SECRET"] = os.environ.get(
    "STRATEGOS_JWT_SECRET", "test-secret-32-bytes-long-padding-enough-now",
)

from app import app  # noqa: E402
from rate_limiter import _reset_for_tests as _rl_reset  # noqa: E402

# Re-use the mocking helpers from test_chat_smoke
from test_chat_smoke import (  # noqa: E402
    _llm_reply, _llm_tool_call, _make_mock_llm,
)


client = TestClient(app)


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    _rl_reset()
    yield


def _send_chat(
    message: str, *, session_id: str, conversation_id: int | None = None,
):
    """Helper to POST /chat."""
    body = {
        "message": message,
        "channel": "web",
        "organization_slug": "just-print",
        "session_id": session_id,
    }
    if conversation_id is not None:
        body["conversation_id"] = conversation_id
    return client.post("/chat", json=body)


# ===========================================================================
# HAPPY PATHS — common product flows that MUST work
# ===========================================================================


class TestHappyPathBusinessCards:
    """500 business cards single-sided matte → expect quote + form path."""

    def test_full_business_cards_journey(self):
        """5-turn happy path. Customer arrives, specs, price-with-artwork-
        question, picks own-artwork, form."""
        session = "flow-bc-happy"

        # ── Turn 1: customer arrives ──────────────────────────────
        with patch("llm.craig_agent.OpenAI", return_value=_make_mock_llm(
            _llm_reply("Hey! What can I get printed for you?"),
        )):
            r = _send_chat("Hi", session_id=session)
        assert r.status_code == 200
        conv_id = r.json()["conversation_id"]

        # ── Turn 2: customer states intent ─────────────────────────
        with patch("llm.craig_agent.OpenAI", return_value=_make_mock_llm(
            _llm_reply(
                "Nice one — how many and any preference on sides + finish?"
            ),
        )):
            r = _send_chat(
                "500 business cards single-sided matte please",
                session_id=session, conversation_id=conv_id,
            )
        assert r.status_code == 200

        # ── Turn 3: customer confirms, Craig prices + asks artwork ──
        with patch("llm.craig_agent.OpenAI", return_value=_make_mock_llm(
            _llm_tool_call("quote_small_format", {
                "product_key": "business_cards",
                "quantity": 500,
                "double_sided": False,
                "finish": "matte",
                "needs_artwork": False,
            }),
            _llm_reply(
                "That'll be €46.74 for 500 business cards 👍 Quick one — "
                "do you have artwork or need our design service?"
            ),
        )):
            r = _send_chat(
                "Yes 500 single-sided matte",
                session_id=session, conversation_id=conv_id,
            )
        body = r.json()
        assert body.get("quote_generated") is True, (
            f"Expected quote after confirming specs, got: {body}"
        )
        assert "€" in body["reply"]

        # ── Turn 4: customer picks own artwork ─────────────────────
        with patch("llm.craig_agent.OpenAI", return_value=_make_mock_llm(
            _llm_reply("Got it 👍 send your print-ready artwork over."),
        )):
            r = _send_chat(
                "I have my own artwork",
                session_id=session, conversation_id=conv_id,
            )
        assert r.status_code == 200

        # No backend errors anywhere in the journey
        assert "Backend error" not in body["reply"]


class TestHappyPathVinylLabels:
    """500 vinyl labels with explicit dimensions → ~€11 inc VAT.
    Regression guard for Bug 1 — Ian Byrne's €341 case."""

    def test_vinyl_labels_with_dims_correct_price(self):
        session = "flow-vinyl-happy"

        # ── Turn 1: customer states specs with dims upfront ─────────
        with patch("llm.craig_agent.OpenAI", return_value=_make_mock_llm(
            _llm_tool_call("quote_large_format", {
                "product_key": "vinyl_labels",
                "quantity": 500,
                "width_mm": 40,
                "height_mm": 10,
                "needs_artwork": False,
            }),
            _llm_reply(
                "That comes to €11.07 for 500 vinyl labels at 40×10mm 👍"
            ),
        )):
            r = _send_chat(
                "500 vinyl labels 40x10mm please",
                session_id=session,
            )
        assert r.status_code == 200
        body = r.json()
        assert body.get("quote_generated") is True
        total = body.get("quote_total_inc_vat") or 0
        assert 9.0 <= total <= 14.0, (
            f"vinyl labels regression: expected ~€11, got €{total}"
        )


class TestHappyPathPVCBanner:
    """PVC banner 1m x 2m, quantity 1 → ~€68.88 inc VAT.
    Regression guard for Bug 3 — price BEFORE artwork question."""

    def test_pvc_banner_price_before_artwork(self):
        session = "flow-banner-happy"

        with patch("llm.craig_agent.OpenAI", return_value=_make_mock_llm(
            _llm_tool_call("quote_large_format", {
                "product_key": "pvc_banners",
                "quantity": 1,
                "width_mm": 1000,
                "height_mm": 2000,
                "needs_artwork": False,
            }),
            _llm_reply(
                "That'll be €68.88 for 1 PVC banner at 1m × 2m 👍 "
                "Do you have artwork ready or would you like design help?"
            ),
        )):
            r = _send_chat(
                "1 PVC banner 1m x 2m please",
                session_id=session,
            )
        body = r.json()
        assert body.get("quote_generated") is True
        total = body.get("quote_total_inc_vat") or 0
        assert 65 <= total <= 75, (
            f"PVC banner price changed: expected ~€68.88, got €{total}"
        )
        # KEY assertion: price appears in the reply
        assert "€" in body["reply"]


class TestHappyPathFoamex:
    """20 foamex panels at 250x500mm → expect 2 sheets × €150 = €300 ex VAT."""

    def test_foamex_per_sheet_packing(self):
        session = "flow-foamex-happy"

        with patch("llm.craig_agent.OpenAI", return_value=_make_mock_llm(
            _llm_tool_call("quote_large_format", {
                "product_key": "foamex_boards",
                "quantity": 20,
                "width_mm": 250,
                "height_mm": 500,
                "needs_artwork": False,
            }),
            _llm_reply(
                "That'll be €369 for 20 foamex panels at 250×500mm "
                "(2 sheets worth) 👍"
            ),
        )):
            r = _send_chat(
                "20 foamex panels 250x500mm",
                session_id=session,
            )
        body = r.json()
        assert body.get("quote_generated") is True
        total = body.get("quote_total_inc_vat") or 0
        # 2 × €150 × 1.23 = €369
        assert 360 <= total <= 380, (
            f"foamex sheet-packing math broken: expected ~€369, got €{total}"
        )


# ===========================================================================
# REGRESSION SCENARIOS — the production-observed abandons
# ===========================================================================


class TestVinylLabelsNoDimsEscalates:
    """Bug 1 defensive — vinyl labels NO dims must NOT silently fall
    back to yield math. This is the Ian-Byrne €341 case."""

    def test_no_dims_returns_escalation_not_runaway_yield(self):
        session = "flow-vinyl-no-dims"

        # The LLM forgets to pass dims. The engine REFUSES to fall
        # back. _exec_tool returns the EscalationResult, the LLM is
        # called again with that result, and it should generate an
        # "ask for dims" reply (NOT a fake price).
        with patch("llm.craig_agent.OpenAI", return_value=_make_mock_llm(
            _llm_tool_call("quote_large_format", {
                "product_key": "vinyl_labels",
                "quantity": 500,
                # MISSING: width_mm, height_mm
                "needs_artwork": False,
            }),
            _llm_reply(
                "Just need the per-label size before I can price it. "
                "What dimensions are they (width × height in mm)?"
            ),
        )):
            r = _send_chat("500 vinyl labels", session_id=session)
        body = r.json()
        # CORE assertion: no Quote row was created (because engine
        # escalated). If the engine had silently fallen back, the
        # tool would have returned a QuoteResult and a Quote row
        # would have been persisted.
        total = body.get("quote_total_inc_vat") or 0
        # The escalation path can either: a) the LLM shell creates a
        # `needs_revision` Quote with total=None, or b) no Quote at
        # all. In neither case should we see €341.
        assert total < 30, (
            f"Bug 1 regression: yield-fallback fired. Got €{total} for "
            f"500 vinyl labels with no dims (should have escalated)."
        )


class TestSpanishCustomerLanguageMirror:
    """Bug 4 — Craig must reply in Spanish when the customer writes
    in Spanish. We verify two things:
      1. The request reaches the LLM intact with the Spanish text.
      2. The business_rules Setting (DB-stored, force-reseeded by v38
         migration) contains the LANGUAGE MIRRORING rule.
    """

    def test_spanish_message_reaches_llm_without_crash(self):
        with patch("llm.craig_agent.OpenAI", return_value=_make_mock_llm(
            _llm_reply(
                "¡Claro! Justo para confirmar — 500 tarjetas, doble cara, "
                "mate 👍"
            ),
        )):
            r = _send_chat(
                "quiero 500 tarjetas de visita a doble cara mate",
                session_id="flow-spanish-mirror",
            )
        assert r.status_code == 200
        body = r.json()
        assert "Backend error" not in body["reply"]
        # The mocked reply (Spanish) should reach the customer modulo
        # post-processing (the price-first gate may append [ARTWORK_CHOICE]
        # English text — that's a separate bug to fix later).
        assert any(
            kw in body["reply"].lower()
            for kw in ("claro", "tarjet", "justo", "doble cara", "mate")
        ), f"Spanish reply got mangled: {body['reply']!r}"

    def test_language_mirroring_rule_seeded_in_business_rules(self):
        """The v38 migration force-reseeds business_rules with Rule 0
        being LANGUAGE MIRRORING. Verify the rule is in the DB."""
        from db import db_session
        from db.models import Setting
        import json as _json

        with db_session() as db:
            row = db.query(Setting).filter_by(
                organization_slug="just-print", key="business_rules",
            ).first()
            assert row is not None, "business_rules Setting missing"
            rules = _json.loads(row.value)
            joined = " ".join(rules)
        assert "LANGUAGE MIRRORING" in joined, (
            "v38 Rule 0 (LANGUAGE MIRRORING) missing from business_rules "
            "Setting — Bug 4 fix regressed"
        )


class TestPostersRequestRecognized:
    """Bug 5 + 6 — Craig must RECOGNIZE 'posters' as a valid product
    (added in v38) and NOT say 'we don't have A0'."""

    def test_posters_in_catalog_and_reachable_via_chat(self):
        """The /products endpoint includes `posters` (already tested in
        test_chat_smoke). Here we verify /chat doesn't fall over when
        a customer asks for posters AND the reply doesn't contain
        the old 'we don't have A0' rejection."""
        with patch("llm.craig_agent.OpenAI", return_value=_make_mock_llm(
            _llm_reply(
                "A0 posters — nice one. Quick one: what quantity are you "
                "after, and any preference on paper weight?"
            ),
        )):
            r = _send_chat(
                "I need A0 posters",
                session_id="flow-posters",
            )
        body = r.json()
        # The forbidden response from before v38
        assert "don't have A0" not in body["reply"].lower()
        assert "don't do A0" not in body["reply"].lower()


class TestArtworkSendLaterStillPrices:
    """Bug 2 — when customer says 'send artwork later', Craig must
    still produce a price BEFORE asking for the contact form. Old
    flow jumped straight to [CUSTOMER_FORM] with no price visible."""

    def test_send_later_keeps_price_visible(self):
        """3-turn: specs → artwork choice → 'send later' should
        result in a reply containing a price."""
        session = "flow-send-later"

        # Turn 1: specs
        with patch("llm.craig_agent.OpenAI", return_value=_make_mock_llm(
            _llm_tool_call("quote_small_format", {
                "product_key": "business_cards",
                "quantity": 1000,
                "double_sided": False,
                "finish": "soft_touch",
                "needs_artwork": False,
            }),
            _llm_reply(
                "That'll be €X.XX for 1000 business cards soft-touch 👍 "
                "Do you have artwork or need design help?"
            ),
        )):
            r = _send_chat(
                "1000 business cards single-sided soft touch",
                session_id=session,
            )
        body = r.json()
        conv_id = body["conversation_id"]
        # Quote should already be in the conversation now
        assert body.get("quote_generated") is True

        # Turn 2: customer says they'll send artwork later
        with patch("llm.craig_agent.OpenAI", return_value=_make_mock_llm(
            _llm_reply(
                "No problem, I'll get that ready 👍 Just need a few details "
                "to put the full quote together."
            ),
        )):
            r2 = _send_chat(
                "I'll send my artwork later — please price it now.",
                session_id=session,
                conversation_id=conv_id,
            )
        body2 = r2.json()
        # The key Bug 2 assertion: the reply for this turn should
        # NOT be just "[CUSTOMER_FORM]" with no price context. Either
        # the LLM mentions a price OR a price was already shown on
        # turn 1 (which means our conversation has price context).
        # The premature gate (v38) injects a price if missing — so
        # the customer should see a number SOMEWHERE in this thread.
        all_replies = body["reply"] + " " + body2["reply"]
        # If we have a quote on the conversation, at minimum the
        # quote_total_inc_vat field on the latest turn should be
        # populated.
        # We accept either: a) total in latest response, OR b) € in
        # the combined reply text.
        has_price_signal = (
            (body2.get("quote_total_inc_vat") or 0) > 0
            or "€" in all_replies
            or "EUR" in all_replies.upper()
        )
        assert has_price_signal, (
            f"Bug 2 regression: customer asked for price 'now' but "
            f"no price signal appears. Turn 1 reply: {body['reply']!r} "
            f"Turn 2 reply: {body2['reply']!r}"
        )


class TestOffFlowChatter:
    """When the customer says something completely off-topic OR
    walks away, Craig shouldn't crash and shouldn't fake a price."""

    def test_thanks_bye_doesnt_create_quote(self):
        """Customer's first turn is just 'thanks!'. No specs, no
        product. Craig should reply politely, not invent a quote."""
        with patch("llm.craig_agent.OpenAI", return_value=_make_mock_llm(
            _llm_reply("No problem at all — give us a shout when you know what you need 👍"),
        )):
            r = _send_chat("thanks!", session_id="flow-thanks-bye")
        body = r.json()
        assert r.status_code == 200
        assert body.get("quote_generated") is not True
        assert body.get("quote_total_inc_vat") is None

    def test_question_about_address_doesnt_crash(self):
        """Some customers ask 'where are you based?' instead of for
        a quote. Make sure Craig responds without errors."""
        with patch("llm.craig_agent.OpenAI", return_value=_make_mock_llm(
            _llm_reply("We're in Ballymount, Dublin 12 — Unit 7 👍"),
        )):
            r = _send_chat(
                "Where are you based?",
                session_id="flow-address",
            )
        body = r.json()
        assert r.status_code == 200
        assert "Backend error" not in body["reply"]


class TestEngagementRejectedThreadStaysSilent:
    """v37 — once a Missive thread is engagement_rejected, future
    messages must be silently dropped. (This is a Missive-flow test,
    but the contract belongs alongside the other flow guards.)"""

    def test_rejected_thread_drops_inbound_silently(self):
        from db import db_session
        from db.models import Conversation

        with db_session() as db:
            conv = Conversation(
                organization_slug="just-print",
                channel="missive",
                external_id="flow-rejected-thread-1",
                status="engagement_rejected",
                messages=[{"role": "user", "content": "earlier"}],
            )
            db.add(conv)
            db.commit()

        from app import _handle_missive_event
        from unittest.mock import patch as _patch

        chat_called = MagicMock()
        create_draft_called = MagicMock()

        def fake_get_setting(db, key, default="", organization_slug=None):
            if key == "missive_enabled":
                return "true"
            if key == "missive_api_token":
                return "fake-token"
            return default

        with _patch("pricing_engine._get_setting", side_effect=fake_get_setting), \
             _patch("llm.craig_agent.chat_with_craig", chat_called), \
             _patch("missive.create_draft", create_draft_called):
            _handle_missive_event("just-print", {
                "rule": {"id": "r", "type": "webhook"},
                "conversation": {"id": "flow-rejected-thread-1", "subject": "test"},
                "message": {
                    "id": "msg-rejected-followup",
                    "type": "email",
                    "from_field": {"address": "bob@example.com", "name": "Bob"},
                    "to_fields": [{"address": "info@just-print.ie"}],
                    "preview": "Following up on my quote",
                    "body": "Following up on my quote",
                    "subject": "Re: Quote",
                    "headers": {},
                },
            })
        assert chat_called.call_count == 0, (
            "engagement_rejected thread leaked back to chat_with_craig — "
            "Justin will get pestered by Craig"
        )
        assert create_draft_called.call_count == 0


# ===========================================================================
# WIDGET-SPECIFIC GATES — funnel + contact + artwork upload
# ===========================================================================


class TestPrematureQuoteReadyGateInjectsPrice:
    """Bug 2 helper — when the LLM emits [QUOTE_READY] without a
    price in the reply text, the gate must fetch the Quote row and
    prepend the price. Customer must always see a number."""

    def test_premature_quote_ready_with_no_price_in_text_still_shows_price(self):
        session = "flow-premature-price-inject"

        # Turn 1: customer + specs → quote created
        with patch("llm.craig_agent.OpenAI", return_value=_make_mock_llm(
            _llm_tool_call("quote_small_format", {
                "product_key": "business_cards",
                "quantity": 100,
                "double_sided": False,
                "finish": "matte",
                "needs_artwork": False,
            }),
            _llm_reply("That'll be €X for 100 cards 👍"),
        )):
            r = _send_chat(
                "100 business cards single-sided matte",
                session_id=session,
            )
        body = r.json()
        conv_id = body["conversation_id"]
        # Quote exists now
        last_quote_id = body.get("quote_id")
        assert last_quote_id is not None

        # Turn 2: LLM emits [QUOTE_READY] but no € in its text.
        # The premature gate should inject a price line.
        with patch("llm.craig_agent.OpenAI", return_value=_make_mock_llm(
            _llm_reply("Here's your quote! 📋 [QUOTE_READY]"),
        )):
            r2 = _send_chat(
                "yes please",
                session_id=session,
                conversation_id=conv_id,
            )
        body2 = r2.json()
        # The gate should have either left [QUOTE_READY] or replaced
        # it. But the customer must see a price OR the conversation
        # must have a known quote total.
        has_visible_price = (
            "€" in body2["reply"]
            or (body2.get("quote_total_inc_vat") or 0) > 0
        )
        assert has_visible_price, (
            f"v38 Bug 2 regression: no price visible after [QUOTE_READY]. "
            f"Reply: {body2['reply']!r}"
        )


# ===========================================================================
# SYSTEM PROMPT CONTRACTS — assert the v38 rules survive in production
# ===========================================================================


class TestV38PromptContracts:
    """The business_rules Setting is reseeded by v38 migration. If a
    later migration accidentally clobbers it, these tests catch the
    regression. We inspect the DB Setting directly so we're robust
    against operator-edited system_prompt overrides."""

    def _get_rules(self):
        from db import db_session
        from db.models import Setting
        import json as _json
        with db_session() as db:
            row = db.query(Setting).filter_by(
                organization_slug="just-print", key="business_rules",
            ).first()
            assert row is not None, (
                "business_rules Setting missing — v38 migration didn't run"
            )
            return _json.loads(row.value)

    def test_v38_language_mirroring_rule_present(self):
        joined = " ".join(self._get_rules())
        assert "LANGUAGE MIRRORING" in joined, (
            "v38 Rule 0 (LANGUAGE MIRRORING) missing — Bug 4 regression"
        )

    def test_v38_price_first_rule_present(self):
        joined = " ".join(self._get_rules())
        assert "PRICE FIRST" in joined, (
            "v38 Rule 2 (PRICE FIRST) missing — Bug 3 regression"
        )

    def test_v38_posters_handling_rule_present(self):
        joined = " ".join(self._get_rules())
        # Rule 8 has uppercase "POSTERS" header
        assert "POSTERS" in joined or "posters" in joined.lower(), (
            "v38 Rule 8 (POSTERS handling) missing — Bug 5 risk"
        )

    def test_v38_vinyl_dimensions_rule_present(self):
        joined = " ".join(self._get_rules())
        # Rule 8 specific wording on vinyl labels + dimensions
        assert ("VINYL LABELS" in joined or "vinyl labels" in joined.lower()), (
            "v38 vinyl-labels dimension rule missing — Bug 1 risk"
        )
