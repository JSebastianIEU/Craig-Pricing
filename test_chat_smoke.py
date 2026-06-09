"""
Smoke tests for POST /chat — catches deploy-breaking regressions like:
  * NameError / forward references in craig_agent.py (v38.3 caught one)
  * Missing imports
  * Migration tripping up the request path
  * Server returning 500 for any reason

These tests do NOT exercise LLM intelligence — they mock the LLM
client and assert that:
  1. /chat returns 200 (no backend crash).
  2. The response body has the expected shape (reply, conversation_id, ...).
  3. Tool-call → tool-result → final-reply sequence works end-to-end.
  4. The full code path through the response gates fires without error.

Runs fast (<10s). Should be the FIRST suite blocking a deploy: a
deploy that fails any of these is provably broken.

How LLM mocking works:
  - We patch `llm.craig_agent.OpenAI` to return MagicMock clients.
  - Each test defines a sequence of "canned" LLM responses (tool_call
    on turn 1, final text on turn 2 after the tool runs, etc.).
  - The mock_create function pops the next response per LLM call.
"""

from __future__ import annotations

import json
import os
import time
from unittest.mock import MagicMock, patch

import pytest
import jwt
from fastapi.testclient import TestClient

os.environ["STRATEGOS_JWT_SECRET"] = os.environ.get(
    "STRATEGOS_JWT_SECRET", "test-secret-32-bytes-long-padding-enough-now",
)

from app import app  # noqa: E402
from rate_limiter import _reset_for_tests as _rl_reset  # noqa: E402


client = TestClient(app)


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    _rl_reset()
    yield


# ---------------------------------------------------------------------------
# LLM mocking helpers — re-usable across this file and test_craig_flow.py
# ---------------------------------------------------------------------------


def _llm_reply(text: str):
    """Build a canned LLM response with a TEXT reply (no tool calls)."""
    msg = MagicMock()
    msg.content = text
    msg.tool_calls = None
    return MagicMock(choices=[MagicMock(message=msg, finish_reason="stop")])


def _llm_tool_call(name: str, args: dict, call_id: str = "call_1"):
    """Build a canned LLM response that requests a tool call.

    The LLM's contract: when it wants to invoke a tool, the response
    has `choices[0].message.tool_calls = [<ToolCall>]` and content
    is usually None or empty. craig_agent's loop will dispatch each
    tool call via _exec_tool, then call the LLM again with the tool
    results appended to the messages.
    """
    tc = MagicMock()
    tc.id = call_id
    tc.type = "function"
    tc.function = MagicMock()
    tc.function.name = name
    tc.function.arguments = json.dumps(args)
    msg = MagicMock()
    msg.content = None
    msg.tool_calls = [tc]
    return MagicMock(choices=[MagicMock(message=msg, finish_reason="tool_calls")])


def _make_mock_llm(*responses):
    """Build a MagicMock OpenAI client that returns the given responses
    in order, one per `chat.completions.create` call.

    Use like:
        with patch("llm.craig_agent.OpenAI", return_value=_make_mock_llm(
            _llm_tool_call("quote_large_format", {...}),
            _llm_reply("That'll be €X..."),
        )):
            r = client.post("/chat", json={...})
    """
    call_idx = [0]
    responses_list = list(responses)

    def _fake_create(**kwargs):
        i = call_idx[0]
        call_idx[0] += 1
        if i >= len(responses_list):
            # Reached the end — return a benign empty reply so the
            # function doesn't crash. Test should ideally not hit
            # this; if it does, the test was under-provisioned.
            return _llm_reply("")
        return responses_list[i]

    mock_client = MagicMock()
    mock_client.chat.completions.create = _fake_create
    return mock_client


# ---------------------------------------------------------------------------
# Smoke contract — does /chat work AT ALL?
# ---------------------------------------------------------------------------


class TestChatEndpointBasics:
    """Bare-minimum contract: /chat with a valid body returns 200 and
    a response with the documented shape. Catches the NameError /
    import / migration class of deploy-breakers."""

    def test_chat_responds_200_on_first_turn(self):
        """The simplest possible smoke test: a brand-new chat returns
        a 200. Catches the v38.3 NameError that broke every /chat
        request silently for ~5 minutes in production."""
        with patch(
            "llm.craig_agent.OpenAI",
            return_value=_make_mock_llm(
                _llm_reply("Hey! Craig here. What can I get printed for you?"),
            ),
        ):
            r = client.post("/chat", json={
                "message": "Hello",
                "channel": "web",
                "organization_slug": "just-print",
                "session_id": "smoke-basic-1",
            })
        assert r.status_code == 200, (
            f"Expected 200, got {r.status_code}: {r.text[:400]}"
        )
        body = r.json()
        # Response shape contract
        assert "reply" in body
        assert "conversation_id" in body
        assert isinstance(body["reply"], str)
        assert isinstance(body["conversation_id"], int)
        # The mocked LLM reply should make it through (modulo the
        # auto-emit / response-gate post-processing) — at minimum the
        # opening word should survive.
        assert "Craig" in body["reply"] or "Hey" in body["reply"], (
            f"Mocked LLM reply got lost in post-processing: {body['reply']!r}"
        )

    def test_chat_returns_no_backend_error_string(self):
        """If craig_agent crashes mid-request the handler catches the
        exception and returns a `Backend error: ...` string. We must
        never see that in a healthy deploy."""
        with patch(
            "llm.craig_agent.OpenAI",
            return_value=_make_mock_llm(
                _llm_reply("Sure thing — what are you after?"),
            ),
        ):
            r = client.post("/chat", json={
                "message": "I need business cards",
                "channel": "web",
                "organization_slug": "just-print",
                "session_id": "smoke-no-error-1",
            })
        assert r.status_code == 200
        body = r.json()
        assert "Backend error" not in body.get("reply", ""), (
            f"craig_agent crashed: {body['reply']!r}"
        )

    def test_chat_with_tool_call_round_trip(self):
        """Full LLM → tool → LLM round trip. Catches breakage in the
        _exec_tool dispatcher (e.g., a removed tool name, broken
        signature, etc.)."""
        with patch(
            "llm.craig_agent.OpenAI",
            return_value=_make_mock_llm(
                # Turn 1: LLM requests pricing tool
                _llm_tool_call("quote_small_format", {
                    "product_key": "business_cards",
                    "quantity": 100,
                    "double_sided": False,
                    "finish": "matte",
                    "needs_artwork": False,
                }),
                # Turn 2: after tool result, LLM gives final reply
                _llm_reply("That'll be €34.05 for 100 business cards 👍"),
            ),
        ):
            r = client.post("/chat", json={
                "message": "100 business cards single-sided matte please",
                "channel": "web",
                "organization_slug": "just-print",
                "session_id": "smoke-toolcall-1",
            })
        assert r.status_code == 200, f"got {r.status_code}: {r.text[:400]}"
        body = r.json()
        # Quote should have been recorded
        assert body.get("quote_generated") is True
        assert body.get("quote_id") is not None
        # And the price should be in the reply (€ symbol)
        assert "€" in body["reply"]

    def test_chat_returns_widget_response_shape(self):
        """The widget at just-print.ie depends on this shape. Lock it
        so we don't accidentally change a field name."""
        with patch(
            "llm.craig_agent.OpenAI",
            return_value=_make_mock_llm(_llm_reply("Hey")),
        ):
            r = client.post("/chat", json={
                "message": "Hi",
                "channel": "web",
                "organization_slug": "just-print",
                "session_id": "smoke-shape-1",
            })
        body = r.json()
        # Required fields:
        for key in (
            "reply", "conversation_id", "quote_generated",
            "quote_id", "quote_total_inc_vat",
            "escalated", "order_confirmed",
            "tool_calls",
        ):
            assert key in body, f"missing key {key!r} from response: {body}"


# ---------------------------------------------------------------------------
# The v38 bug-fix regressions — each test guards against a specific bug
# we already fixed coming back
# ---------------------------------------------------------------------------


class TestV38BugRegressionGuards:
    """Each test here re-creates a production bug we fixed in v38.
    If any of these fail, the fix has regressed."""

    def test_vinyl_labels_no_dims_returns_escalation_not_yield_price(self):
        """Bug 1 — when LLM forgets to pass width_mm/height_mm,
        vinyl_labels must escalate, not return €341. The engine
        guard in _quote_per_sqm enforces requires_dimensions."""
        with patch(
            "llm.craig_agent.OpenAI",
            return_value=_make_mock_llm(
                _llm_tool_call("quote_large_format", {
                    "product_key": "vinyl_labels",
                    "quantity": 500,
                    # MISSING: width_mm, height_mm
                }),
                _llm_reply("Let me check that with Justin — vinyl labels need a size to price."),
            ),
        ):
            r = client.post("/chat", json={
                "message": "500 vinyl labels",
                "channel": "web",
                "organization_slug": "just-print",
                "session_id": "v38-vinyl-no-dims",
            })
        assert r.status_code == 200
        body = r.json()
        # quote_id should be set IF the LLM shell creates an escalation quote,
        # OR it could be None if the engine returned EscalationResult without
        # a quote row. The KEY thing: total must be missing / zero.
        total = body.get("quote_total_inc_vat") or 0
        assert total == 0 or total < 30, (
            f"vinyl labels no-dims should escalate, not return €{total}"
        )

    def test_vinyl_labels_with_dims_prices_correctly(self):
        """Bug 1 happy path — with explicit dims, engine returns
        the correct ~€11 inc VAT for 500 × 40x10mm labels."""
        with patch(
            "llm.craig_agent.OpenAI",
            return_value=_make_mock_llm(
                _llm_tool_call("quote_large_format", {
                    "product_key": "vinyl_labels",
                    "quantity": 500,
                    "width_mm": 40,
                    "height_mm": 10,
                    "needs_artwork": False,
                }),
                _llm_reply("That'll be €11.07 for 500 vinyl labels 👍"),
            ),
        ):
            r = client.post("/chat", json={
                "message": "500 vinyl labels 40x10mm",
                "channel": "web",
                "organization_slug": "just-print",
                "session_id": "v38-vinyl-with-dims",
            })
        body = r.json()
        assert body.get("quote_generated") is True
        total = body.get("quote_total_inc_vat") or 0
        assert 9.0 <= total <= 14.0, (
            f"Expected ~€11 inc VAT, got €{total} — "
            f"yield-fallback bug regression?"
        )

    def test_chat_endpoint_no_nameerror_on_first_turn(self):
        """Bug v38.3 — `_had_prior_quote` was referenced before
        definition. Every /chat hit returned 'Backend error: cannot
        access local variable'. This test re-runs the exact scenario
        and asserts no error string appears in the reply."""
        with patch(
            "llm.craig_agent.OpenAI",
            return_value=_make_mock_llm(
                _llm_reply("Just to confirm — 1m × 2m PVC banner?"),
            ),
        ):
            r = client.post("/chat", json={
                "message": "PVC banner 1m x 2m",
                "channel": "web",
                "organization_slug": "just-print",
                "session_id": "v38-3-nameerror-regression",
            })
        assert r.status_code == 200
        body = r.json()
        assert "cannot access local variable" not in body.get("reply", ""), (
            f"NameError leaked back to customer: {body['reply']!r}"
        )
        assert "Backend error" not in body.get("reply", "")

    def test_artwork_question_required_guard_was_removed(self):
        """Bug v38.4 — _exec_tool had a guard that refused pricing
        until artwork was answered. v38's price-first flow needs the
        tool to actually run with needs_artwork=False. Confirm the
        guard is gone by inspecting tool_calls in the response: the
        guard would have produced a tool result containing
        'ARTWORK_QUESTION_REQUIRED'."""
        with patch(
            "llm.craig_agent.OpenAI",
            return_value=_make_mock_llm(
                _llm_tool_call("quote_small_format", {
                    "product_key": "business_cards",
                    "quantity": 250,
                    "double_sided": True,
                    "finish": "matte",
                    "needs_artwork": False,  # CRITICAL — flow under test
                }),
                _llm_reply("That'll be €X for 250 business cards 👍"),
            ),
        ):
            r = client.post("/chat", json={
                "message": "250 business cards double-sided matte finish",
                "channel": "web",
                "organization_slug": "just-print",
                "session_id": "v38-4-no-artwork-guard",
            })
        body = r.json()
        # Walk every tool_call result and verify NONE contain the
        # old guard error.
        tool_calls = body.get("tool_calls") or []
        for tc in tool_calls:
            result_str = json.dumps(tc.get("result") or {})
            assert "ARTWORK_QUESTION_REQUIRED" not in result_str, (
                f"v38.4 regression: _exec_tool guard fired and returned "
                f"the old ARTWORK_QUESTION_REQUIRED error. Tool result: "
                f"{tc.get('result')}"
            )

    def test_posters_product_recognized_not_unknown(self):
        """Bug 5 — `posters` is now in the catalog. The LLM should
        be able to call escalate_to_justin on it (manual_review).
        We just verify the smoke path: /chat doesn't crash when the
        message mentions posters."""
        with patch(
            "llm.craig_agent.OpenAI",
            return_value=_make_mock_llm(
                _llm_reply(
                    "A0 posters — nice. Just need to grab your name + email "
                    "so Justin can come back with a quote."
                ),
            ),
        ):
            r = client.post("/chat", json={
                "message": "I need 10 A0 posters",
                "channel": "web",
                "organization_slug": "just-print",
                "session_id": "v38-posters",
            })
        assert r.status_code == 200
        body = r.json()
        # No engine "we don't have A0" / catalog-miss error in the reply
        assert "don't have A0" not in body.get("reply", "").lower()
        assert "Backend error" not in body.get("reply", "")


# ---------------------------------------------------------------------------
# Catalog smoke — list endpoint must include all current products
# ---------------------------------------------------------------------------


class TestCatalogSmoke:
    def test_catalog_includes_posters_after_v38(self):
        """v38 added `posters`. Make sure it's in the public catalog
        endpoint (so the LLM context includes it)."""
        r = client.get("/products")
        assert r.status_code == 200
        keys = {p["key"] for p in r.json()}
        assert "posters" in keys, (
            f"`posters` missing from /products — v38 migration didn't run "
            f"or the seed got rolled back. Keys: {sorted(keys)}"
        )

    def test_catalog_vinyl_labels_is_per_sqm_priced(self):
        """vinyl_labels must be priced per sq/m (v36). The public
        /products endpoint doesn't expose `pricing_strategy`, but it
        does expose `pricing_unit` — verify that signals per_sqm."""
        r = client.get("/products")
        products = r.json()
        vinyl = next((p for p in products if p["key"] == "vinyl_labels"), None)
        assert vinyl is not None, "vinyl_labels missing from /products"
        assert vinyl.get("pricing_unit") == "per sq/m", (
            f"vinyl_labels.pricing_unit changed: {vinyl}"
        )

    def test_catalog_minimum_product_count(self):
        """Hard floor — catalog should have at least 25 products.
        Tripping this means a migration ran a DELETE on products."""
        r = client.get("/products")
        n = len(r.json())
        assert n >= 25, f"Catalog shrank to {n} products — migration data loss?"


# ===========================================================================
# v40.8 — System prompt + catalog-context wording tests
#
# These verify that Justin's meeting feedback is baked into the prompt
# and the runtime catalog context the LLM sees:
#   1. Price wording is "+ VAT" (Irish B2B), not "inc VAT", in chat.
#   2. Finishes (gloss/matte/soft-touch) are scoped to BUSINESS CARDS only.
#   3. Board products surface the 7 standard sizes + custom-mm option.
# ===========================================================================


class TestV408PromptWording:
    """v40.8 — verify the system prompt + catalog hints carry the
    meeting fixes BEFORE they reach the LLM."""

    def test_prompt_uses_plus_vat_phrasing(self):
        """The base system prompt should tell Craig to say '+ VAT'
        (Irish B2B convention) rather than 'inc VAT' in chat."""
        from llm.craig_agent import CRAIG_SYSTEM_PROMPT
        assert "+ VAT" in CRAIG_SYSTEM_PROMPT, (
            "Base prompt should instruct '+ VAT' wording (v40.8)."
        )

    def test_prompt_forbids_finish_question_on_flyers(self):
        """The prompt should explicitly tell Craig NOT to ask
        gloss/matte/soft-touch on flyers / leaflets / brochures /
        NCR books / letterheads / compliment slips — they're 170gsm
        silk full-stop."""
        from llm.craig_agent import CRAIG_SYSTEM_PROMPT
        # Look for any of the explicit-forbid phrases that the v40.8
        # rewrite introduced.
        forbid_signals = [
            "NO finish question",
            "DO NOT ask for finish on flyers",
            "DO NOT offer gloss",
            "no finish option",
            "no finish options",
            "never get a finish question",
        ]
        assert any(s in CRAIG_SYSTEM_PROMPT for s in forbid_signals), (
            "Base prompt should explicitly forbid finishes on non-cards "
            f"(looked for any of: {forbid_signals})."
        )

    def test_prompt_treats_finish_as_laminate_type(self):
        """v40.8.1 — finishes (gloss/matte/soft-touch) ARE the laminate
        type on business cards, not an independent option. Default
        cards are unlaminated; ask finish ONLY when customer mentions
        laminate. The prompt must encode this."""
        from llm.craig_agent import CRAIG_SYSTEM_PROMPT
        # "finish IS the laminate" framing — look for the conceptual
        # tie-in and the unlaminated-default rule.
        laminate_signals = [
            "type of LAMINATE",
            "type of laminate",
            "IS the type of LAMINATE",
            "= the laminate",
            "= which laminate",
            "LAMINATE TYPES",
        ]
        default_unlaminated_signals = [
            "default is UNLAMINATED",
            "default is unlaminated",
            "Default cards are unlaminated",
            "default cards are unlaminated",
            "Default: no laminate",
            "no finish surcharge",
        ]
        assert any(s in CRAIG_SYSTEM_PROMPT for s in laminate_signals), (
            "Prompt must frame finish as the laminate type "
            f"(looked for any of: {laminate_signals})."
        )
        assert any(s in CRAIG_SYSTEM_PROMPT for s in default_unlaminated_signals), (
            "Prompt must say default cards are unlaminated "
            f"(looked for any of: {default_unlaminated_signals})."
        )

    def test_prompt_forbids_asking_customer_to_round_off_tier_qty(self):
        """v40.8.2 — Justin's conv #188 bug: customer asked for 80 booklets,
        Craig replied "our quantities go by 25, 50, 100, 250, 500 — would
        100 work?". That's wrong: the engine has _stack_tiers (v34) which
        handles ANY qty by stack-billing to the nearest tier combination.
        Craig must always pass the customer's exact qty to the tool."""
        from llm.craig_agent import CRAIG_SYSTEM_PROMPT
        # The rule should explicitly forbid asking the customer to round.
        no_round_signals = [
            "DO NOT ask the customer to round",
            "Do not ask the customer to round",
            "do not ask the customer to round",
            "tier breakpoints, not restrictive options",
            "stack-combining tiers",
            "stack-bill",
            "stack-billed",
            "automatically handles ANY quantity",
        ]
        assert any(s in CRAIG_SYSTEM_PROMPT for s in no_round_signals), (
            "Prompt should forbid asking the customer to round off-tier "
            f"qtys (looked for any of: {no_round_signals})."
        )

    def test_catalog_context_calls_qtys_tier_breakpoints(self):
        """v40.8.2 — _build_catalog_context should label the qty list as
        'tier breakpoints' (with auto-stacking hint), not 'quantities'
        (which the LLM was reading as a restrictive enum)."""
        from llm.craig_agent import _build_catalog_context
        from db import db_session
        with db_session() as db:
            ctx = _build_catalog_context(db, "just-print")
        assert "tier breakpoints" in ctx, (
            "Catalog context should say 'tier breakpoints', not just 'quantities'."
        )
        assert "stack" in ctx.lower(), (
            "Catalog context should mention auto-stacking so the LLM knows "
            "off-tier qtys are OK."
        )

    def test_prompt_disambiguates_silk_paper_vs_finish(self):
        """v40.8.3 — DeepSeek hallucinates 'silk' as a finish option on
        flyers because the catalog description says '170gsm silk paper'.
        The prompt must explicitly disambiguate."""
        from llm.craig_agent import CRAIG_SYSTEM_PROMPT
        signals = [
            "silk\" is the PAPER TYPE",
            "silk' is the PAPER TYPE",
            '"silk" is the PAPER TYPE',
            "Do NOT offer \"silk\"",
            "Do not offer 'silk'",
            "do NOT offer 'silk'",
            "silk-coated 170gsm",
            "NOT a finish option",
        ]
        assert any(s in CRAIG_SYSTEM_PROMPT for s in signals), (
            "Prompt must disambiguate 'silk paper' (the paper type) "
            f"from 'silk finish' (looked for any of: {signals})."
        )

    def test_prompt_has_explicit_what_to_say_examples_for_flyer_finishes(self):
        """v40.8.3 — the rule alone wasn't enough to inhibit DeepSeek.
        The prompt now includes explicit ❌ WRONG / ✓ RIGHT example
        replies for when customers ask about flyer finishes."""
        from llm.craig_agent import CRAIG_SYSTEM_PROMPT
        # Look for the wrong/right example pattern in the finishes section.
        assert "❌ WRONG" in CRAIG_SYSTEM_PROMPT, "Prompt should have ❌ WRONG examples"
        assert "✓ RIGHT" in CRAIG_SYSTEM_PROMPT, "Prompt should have ✓ RIGHT examples"
        # The flyer-specific WRONG/RIGHT block should mention silk.
        assert "no finish options needed" in CRAIG_SYSTEM_PROMPT, (
            "Prompt should give Craig the exact safe-reply pattern for "
            "'what finishes do you have?' on flyers."
        )

    def test_prompt_explicitly_says_no_laminate_is_supported_no_escalation(self):
        """v40.8.4 — Justin reported (post-D3 smoke): customer says
        '530 cards, no laminate' and Craig replies "I'll need to get
        Justin to check that for you" instead of just calling the tool
        with finish=uncoated. The prompt rule from v40.8.1 was there but
        DeepSeek kept being cautious. PR 4.4 adds explicit WHAT-TO-SAY
        examples to make it airtight."""
        from llm.craig_agent import CRAIG_SYSTEM_PROMPT
        # The explicit "valid, common, supported choice" framing
        positive_signals = [
            "valid, common, supported choice",
            "valid common supported choice",
            "JUST PRICE IT",
            "Do NOT escalate to Justin",
            "Do not escalate to Justin",
            "do NOT escalate to Justin",
        ]
        assert any(s in CRAIG_SYSTEM_PROMPT for s in positive_signals), (
            "Prompt must frame 'no laminate' as supported (not requiring "
            f"escalation). Looked for any of: {positive_signals}."
        )

    def test_prompt_has_no_laminate_what_to_say_examples(self):
        """v40.8.4 — explicit ❌ WRONG / ✓ RIGHT examples for the
        'no laminate' case. Without these the rule keeps being ignored."""
        from llm.craig_agent import CRAIG_SYSTEM_PROMPT
        # Specific WRONG-example phrases that v40.8.4 forbids
        wrong_examples = [
            "I'll need to get Justin to check that for you",
            "Would you like me to go with one of those finishes anyway",
            "Plain unlaminated business cards aren't a standard option",
            "Most people go with soft-touch",
        ]
        # At least 2 of the forbidden examples should be present in the
        # ❌ WRONG list so DeepSeek pattern-matches when generating.
        present = sum(1 for w in wrong_examples if w in CRAIG_SYSTEM_PROMPT)
        assert present >= 2, (
            f"Prompt should contain at least 2 explicit ❌ WRONG examples "
            f"for 'no laminate' escalation patterns (found {present}/4)."
        )

    def test_top_fact_is_at_the_very_start_of_prompt(self):
        """v40.8.5 — the FACT statement about unlaminated business cards
        must live in the FIRST ~1,000 chars of the prompt so DeepSeek
        attends to it strongly enough to override training priors."""
        from llm.craig_agent import CRAIG_SYSTEM_PROMPT
        head = CRAIG_SYSTEM_PROMPT[:1500]
        assert "TOP FACT" in head, (
            f"'TOP FACT' header should be in the first 1,500 chars "
            f"(found at position {CRAIG_SYSTEM_PROMPT.find('TOP FACT')})."
        )
        assert "BUSINESS CARDS ONLY" in head, (
            "Top FACT must be visibly scoped to business cards only."
        )

    def test_top_fact_explicitly_scoped_not_universal(self):
        """v40.8.5 — Sebastian was explicit: the FACT must NOT extend
        to other products. The prompt must say so EXPLICITLY so the
        LLM doesn't generalize the unlaminated-default rule beyond
        business cards."""
        from llm.craig_agent import CRAIG_SYSTEM_PROMPT
        scope_signals = [
            "does NOT extend",
            "does not extend",
            "ONLY about business_cards",
            "ONLY about business cards",
            "only to product_key=\"business_cards\"",
            "STRICTLY to product_key",
        ]
        assert any(s in CRAIG_SYSTEM_PROMPT for s in scope_signals), (
            "TOP FACT must explicitly say it does NOT apply to other "
            f"products (looked for any of: {scope_signals})."
        )
        # And explicitly mention some of the other products NOT covered
        not_covered = ["flyers", "leaflets", "brochures", "NCR books",
                       "letterheads", "compliment slips", "boards"]
        non_covered_count = sum(1 for p in not_covered
                                if p in CRAIG_SYSTEM_PROMPT[:2000])
        assert non_covered_count >= 4, (
            f"TOP FACT should name at least 4 of {not_covered} as "
            f"NOT covered (found {non_covered_count})."
        )

    def test_prompt_forbids_pushing_laminate_unprompted(self):
        """v40.8.1 — Craig should NOT push laminate unprompted on
        business cards. Wait for the customer to mention it."""
        from llm.craig_agent import CRAIG_SYSTEM_PROMPT
        no_push_signals = [
            "Do NOT push laminate",
            "Don't push laminate",
            "do not push laminate",
            "don't push laminate",
            "Do NOT push laminate unprompted",
            "no push",
        ]
        assert any(s in CRAIG_SYSTEM_PROMPT for s in no_push_signals), (
            "Prompt must instruct Craig not to push laminate "
            f"(looked for any of: {no_push_signals})."
        )

    def test_email_channel_drops_generic_finish_offer(self):
        """The missive channel context should NOT instruct Craig
        to offer 'gloss/matte/soft-touch' generically — that's a
        business-cards-only line per v40.8, and the v40.8.1 rewrite
        also clarifies finish = laminate type."""
        from llm.craig_agent import _CHANNEL_CONTEXT
        missive = _CHANNEL_CONTEXT.get("missive", "")
        scope_signals = [
            # v40.8 original wording
            "Finishes apply ONLY to business cards",
            "no finish option",
            "DO NOT ask for finish",
            # v40.8.1 rewritten wording (laminate-type framing)
            "ONLY apply to business cards",
            "LAMINATE TYPES",
            "are LAMINATE TYPES",
            "do not push laminate",
            "do NOT push laminate",
            "Do NOT push laminate",
        ]
        assert any(s in missive for s in scope_signals), (
            "Missive context should restrict finishes to business cards "
            f"(looked for any of: {scope_signals})."
        )

    def test_catalog_context_injects_board_sizes_hint_for_tiered_large_format(self):
        """When a large_format product is configured tiered (v40.7),
        _build_catalog_context should inject the hint that lists the
        7 standard sizes + custom-mm option so the LLM knows what
        to ask the customer."""
        from llm.craig_agent import _build_catalog_context
        from db import db_session
        from db.models import Product

        # Snapshot + mutate corri_boards to tiered for this assertion,
        # then revert in finally.
        with db_session() as db:
            p = db.query(Product).filter_by(
                organization_slug="just-print", key="corri_boards",
            ).first()
            if p is None:
                import pytest
                pytest.skip("corri_boards not in seed.")
            orig_strat = p.pricing_strategy
            p.pricing_strategy = "tiered"
            db.commit()
            try:
                ctx = _build_catalog_context(db, "just-print")
            finally:
                p.pricing_strategy = orig_strat
                db.commit()

        assert "2440x1220" in ctx, (
            "Catalog context should mention full sheet size for boards."
        )
        # At least one of the standard sizes should appear next to the
        # tiered-board hint.
        assert "A3" in ctx and "A1" in ctx, (
            "Catalog context should mention standard board sizes (A3, A1, etc.)."
        )
        assert "laydown" in ctx.lower() or "custom" in ctx.lower(), (
            "Catalog context should mention the laydown / custom-mm path."
        )

    def test_catalog_context_does_not_break_for_per_sheet_boards(self):
        """Sanity: with corri_boards still on per_sheet, the catalog
        context still renders (no exception, no regression)."""
        from llm.craig_agent import _build_catalog_context
        from db import db_session
        with db_session() as db:
            ctx = _build_catalog_context(db, "just-print")
        assert "corri_boards" in ctx or "Corri" in ctx, (
            "Catalog context should always include corri_boards."
        )
