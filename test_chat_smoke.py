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
        """v40.8.3 → v40.8.7: prompt must give Craig the safe-reply
        wording when customer asks about flyer finishes. The v40.8.7
        cleanup collapsed the verbose ❌WRONG/✓RIGHT block; what
        survived is the exact-reply phrase that DeepSeek pattern-
        matches against."""
        from llm.craig_agent import CRAIG_SYSTEM_PROMPT
        # Look for the safe-reply phrase OR the WRONG/RIGHT marker
        # (whichever wording is current).
        signals = [
            "no finish options needed",
            "no separate finish options",
            "no finish options",
            "no separate matte/gloss",
            "❌ WRONG",
            "✓ RIGHT",
        ]
        assert any(s in CRAIG_SYSTEM_PROMPT for s in signals), (
            "Prompt should give the safe-reply pattern for 'what "
            f"finishes do you have?' on flyers. Looked for: {signals}."
        )

    def test_prompt_explicitly_says_no_laminate_is_supported_no_escalation(self):
        """v40.8.4 → v40.8.7: prompt must frame 'no laminate' as a valid
        supported choice (not requiring escalation). v40.8.7 collapsed
        the verbose examples; the shorter TOP FACT + Finishes section
        carries the same instruction."""
        from llm.craig_agent import CRAIG_SYSTEM_PROMPT
        positive_signals = [
            # v40.8.4 wording
            "valid, common, supported choice",
            "JUST PRICE IT",
            "Do NOT escalate to Justin",
            "do NOT escalate to Justin",
            # v40.8.7 wording (short TOP FACT + Finishes section)
            "that IS the default product",
            "Don't push laminate",
            "don't escalate",
            "no finish surcharge",
            "Do NOT escalate. Do NOT push laminate",
        ]
        assert any(s in CRAIG_SYSTEM_PROMPT for s in positive_signals), (
            "Prompt must frame 'no laminate' as supported (not "
            f"escalation). Looked for any of: {positive_signals}."
        )

    def test_prompt_has_no_laminate_what_to_say_examples(self):
        """v40.8.4 → v40.8.7: in v40.8.4 we baked verbatim ❌ WRONG
        examples; v40.8.7 collapsed them into the shorter Finishes
        section. What survives is the positive instruction (pass
        finish='uncoated', don't escalate) which is what DeepSeek
        actually needs."""
        from llm.craig_agent import CRAIG_SYSTEM_PROMPT
        # Either the explicit forbidden examples (v40.8.4 wording)
        # OR the positive imperative (v40.8.7 wording) is acceptable.
        wrong_examples = [
            "I'll need to get Justin to check that for you",
            "Would you like me to go with one of those finishes anyway",
            "Plain unlaminated business cards aren't a standard option",
            "Most people go with soft-touch",
        ]
        positive_imperative = [
            "pass `finish=\"uncoated\"`",
            "pass `finish=\\\"uncoated\\\"`",
            "finish=\"uncoated\"",
            "Pass `finish=\"uncoated\"`",
            "call quote_small_format(finish=\"uncoated\")",
        ]
        wrong_present = sum(1 for w in wrong_examples if w in CRAIG_SYSTEM_PROMPT)
        positive_present = any(p in CRAIG_SYSTEM_PROMPT for p in positive_imperative)
        assert wrong_present >= 2 or positive_present, (
            f"Prompt must either list ≥2 ❌ WRONG examples (v40.8.4 wording, "
            f"found {wrong_present}) or contain the positive imperative "
            f"finish='uncoated' (v40.8.7 wording)."
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
        """v40.8.5 → v40.8.7: TOP FACT must remain scoped to
        business_cards only. v40.8.7 shortened the wording but the
        scoping is preserved."""
        from llm.craig_agent import CRAIG_SYSTEM_PROMPT
        scope_signals = [
            # v40.8.5 wording
            "does NOT extend",
            "ONLY about business_cards",
            "STRICTLY to product_key",
            # v40.8.7 wording (short FACT)
            "applies ONLY to business_cards",
            "ONLY to business_cards",
            "other products keep their own rules below",
            "applies ONLY to business cards",
        ]
        assert any(s in CRAIG_SYSTEM_PROMPT for s in scope_signals), (
            "TOP FACT must explicitly scope to business_cards "
            f"(looked for any of: {scope_signals})."
        )

    def test_prompt_has_positive_imperative_tool_call_template(self):
        """v40.8.6 → v40.8.7: the v40.8.6 verbose "IMMEDIATE NEXT
        ACTION" block was collapsed into the graded confirm rule in
        v40.8.7. The positive direction "specs clear → CALL THE TOOL
        DIRECTLY" is preserved (now applies to all products, not just
        business_cards)."""
        from llm.craig_agent import CRAIG_SYSTEM_PROMPT
        positive_signals = [
            # v40.8.6 verbose wording
            "YOUR IMMEDIATE NEXT ACTION",
            "your NEXT action is a tool call",
            "Tool to call: quote_small_format",
            # v40.8.7 graded rule wording
            "CALL THE TOOL DIRECTLY",
            "CALL quote_small_format directly",
            "CALL quote_large_format directly",
            "CALL quote_booklet directly",
            "DIRECT TOOL CALL",
        ]
        assert any(s in CRAIG_SYSTEM_PROMPT for s in positive_signals), (
            "Prompt must give a positive tool-call imperative "
            f"(looked for: {positive_signals})."
        )

    def test_prompt_specifies_required_order_of_operations(self):
        """v40.8.6 → v40.8.7: the bug is workflow inversion (collect
        contact BEFORE tool call). v40.8.7 collapsed the explicit
        ORDER OF OPERATIONS block; what survives is the negative
        instruction "Do NOT collect contact info before calling the
        tool" inside the short TOP FACT."""
        from llm.craig_agent import CRAIG_SYSTEM_PROMPT
        sequence_signals = [
            # v40.8.6 verbose wording
            "REQUIRED ORDER OF OPERATIONS",
            "CALL the tool (above). Get the price",
            "ONLY AFTER",
            # v40.8.7 wording (in the short TOP FACT)
            "Do NOT collect contact info before calling the tool",
            "Do not collect contact info before calling the tool",
            "before calling the tool",
            "graded confirm rule applies normally",
        ]
        assert any(s in CRAIG_SYSTEM_PROMPT for s in sequence_signals), (
            "Prompt must enforce the order (no contact-collect before "
            f"tool call). Looked for: {sequence_signals}."
        )

    def test_prompt_lists_forbidden_workflow_inversion_patterns(self):
        """v40.8.6 → v40.8.7: the v40.8.6 verbatim ❌ FORBIDDEN block
        was collapsed in v40.8.7. The new structure delegates to the
        graded confirm rule + the short TOP FACT, which together say
        'specs clear → tool call, don't collect contact first'. Accept
        either the v40.8.6 verbatim phrases OR the v40.8.7 condensed
        instruction."""
        from llm.craig_agent import CRAIG_SYSTEM_PROMPT
        # v40.8.6 verbatim
        forbidden_verbatim = [
            "Before I get Justin to confirm the price",
            "So Justin can get back to you with the price",
            "I need to grab your details first",
            "Justin will check that and come back to you",
            "BEFORE calling the tool",
            "you HAVE the price",
        ]
        # v40.8.7 condensed
        condensed_signals = [
            "Do NOT collect contact info before calling the tool",
            "Do not collect contact info before calling the tool",
            "Do NOT escalate. Do NOT push laminate",
            "before calling the tool",
        ]
        verbatim_present = sum(1 for f in forbidden_verbatim if f in CRAIG_SYSTEM_PROMPT)
        condensed_present = any(s in CRAIG_SYSTEM_PROMPT for s in condensed_signals)
        assert verbatim_present >= 2 or condensed_present, (
            f"Prompt must inhibit contact-first workflow either by ≥2 "
            f"verbatim v40.8.6 patterns (found {verbatim_present}) or "
            f"by the v40.8.7 condensed instruction."
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

    def test_v40_8_7_graded_confirm_rule_present(self):
        """v40.8.7 — the prompt must include the graded confirm rule
        that resolves the v40.8.6 contradiction (always-confirm vs
        tool-first). DeepSeek needs both the GENUINE AMBIGUITY framing
        AND the DIRECT TOOL CALL counter-examples to pattern-match
        correctly."""
        from llm.craig_agent import CRAIG_SYSTEM_PROMPT
        # 1. The graded framing exists
        graded_signals = [
            "GENUINE\nAMBIGUITY",
            "GENUINE AMBIGUITY",
            "Genuine ambiguity",
            "When to confirm specs vs call the tool directly",
            "Confirm specs back to the customer BEFORE the tool call ONLY when",
        ]
        assert any(s in CRAIG_SYSTEM_PROMPT for s in graded_signals), (
            f"Prompt must include the graded confirm rule "
            f"(looked for: {graded_signals})."
        )

    def test_v40_8_7_direct_tool_call_examples_present(self):
        """v40.8.7 — the prompt must include explicit DIRECT TOOL CALL
        examples for unambiguous specs across all 3 quote tools so
        DeepSeek pattern-matches and skips the confirmation step when
        appropriate."""
        from llm.craig_agent import CRAIG_SYSTEM_PROMPT
        # At least 3 of the 4 verbatim direct-call examples should be
        # present (one per quote tool: small_format, large_format,
        # booklet).
        direct_examples = [
            "CALL quote_small_format directly",
            "CALL quote_large_format directly",
            "CALL quote_booklet directly",
            "DIRECT TOOL CALL",
        ]
        present = sum(1 for ex in direct_examples if ex in CRAIG_SYSTEM_PROMPT)
        assert present >= 3, (
            f"Prompt must contain ≥3 of {direct_examples} (the per-tool "
            f"DIRECT TOOL CALL examples). Found {present}."
        )

    def test_v40_8_7_old_always_confirm_rule_gone(self):
        """v40.8.7 — verify the unconditional 'ALWAYS confirm the
        specs back to the customer BEFORE calling the pricing tool'
        rule has been removed (it was the contradiction source vs
        TOP FACT). Replaced by the graded rule."""
        from llm.craig_agent import CRAIG_SYSTEM_PROMPT
        old_rule = "ALWAYS confirm the specs back to the customer BEFORE calling the pricing tool"
        assert old_rule not in CRAIG_SYSTEM_PROMPT, (
            f"The old unconditional rule must be removed (was at line ~189 "
            f"pre-v40.8.7). It conflicted with the TOP FACT tool-first "
            f"imperative and caused DeepSeek inconsistency."
        )

    def test_v40_8_8_deepseek_temperature_setting_seeded(self):
        """v40.8.8 — the per-tenant `deepseek_temperature` setting must
        be seeded on the just-print org so the runtime can read it via
        _get_setting. Default value 0.3 preserves pre-v40.8.8 behavior."""
        from db import db_session
        from db.models import Setting
        with db_session() as db:
            row = (
                db.query(Setting)
                .filter_by(organization_slug="just-print",
                           key="deepseek_temperature")
                .first()
            )
            # Extract values BEFORE session close to avoid
            # DetachedInstanceError on lazy attribute load.
            row_present = row is not None
            row_value_type = row.value_type if row else None
            row_value = row.value if row else None

        assert row_present, (
            "deepseek_temperature setting missing for just-print — "
            "v44 seed didn't run."
        )
        assert row_value_type == "float", (
            f"deepseek_temperature value_type must be float (got {row_value_type!r}); "
            "_get_setting type-casts based on this."
        )
        # Default seed = 0.3 (pre-v40.8.8 behavior). Test only asserts
        # the row exists with float type; the actual numeric value can
        # be tuned per-tenant via PATCH /settings/deepseek_temperature.
        assert 0.0 <= float(row_value) <= 2.0, (
            f"deepseek_temperature must be in OpenAI's [0.0, 2.0] range "
            f"(got {row_value!r})."
        )

    def test_v40_8_8_chat_loop_reads_temperature_from_setting(self):
        """v40.8.8 — chat_with_craig must read the temperature from the
        Setting via _get_setting (not hardcode 0.3). Verify by checking
        the function source uses _get_setting('deepseek_temperature', ...)
        OR references the variable name."""
        import inspect
        from llm.craig_agent import chat_with_craig
        src = inspect.getsource(chat_with_craig)
        assert "deepseek_temperature" in src, (
            "chat_with_craig must read 'deepseek_temperature' from settings, "
            "not hardcode 0.3."
        )
        # No raw `temperature=0.3` literal should remain — must come from
        # the variable. (The clamping branch may still mention 0.0/2.0.)
        assert "temperature=0.3" not in src, (
            "Hardcoded temperature=0.3 found in chat_with_craig — should "
            "use deepseek_temperature variable read from Setting."
        )

    def test_v40_8_9_boards_no_size_escalation_is_craig_instructional(self):
        """v40.8.9 — Justin reported many board orders escalating to
        manual pricing because Craig was repeating the engine's
        old escalation message ('what custom dimensions in mm?')
        verbatim to customers who had already named a standard A-series
        size. New message must be CRAIG-INSTRUCTIONAL (tells the LLM
        to retry with `size`), not customer-facing."""
        from pricing_engine import quote_large_format, EscalationResult
        from db import db_session
        with db_session() as db:
            from db.models import Product
            p = db.query(Product).filter_by(
                organization_slug="just-print", key="corri_boards",
            ).first()
            if p is None:
                import pytest
                pytest.skip("corri_boards missing from seed.")
            orig_strat = p.pricing_strategy
            p.pricing_strategy = "tiered"
            db.commit()
            try:
                # Caller forgot to pass size or width/height.
                result = quote_large_format(
                    db, product_key="corri_boards", quantity=5,
                    organization_slug="just-print",
                )
                msg = result.message if isinstance(result, EscalationResult) else ""
            finally:
                p.pricing_strategy = orig_strat
                db.commit()

        assert isinstance(result, EscalationResult)
        # The new message must be addressed to Craig, not the customer.
        instructional_signals = [
            "INSTRUCTION FOR CRAIG",
            "do NOT repeat to the customer",
            "RETRY this tool call",
            "RETRY the tool call",
            "retry the tool call",
        ]
        assert any(s in msg for s in instructional_signals), (
            f"Escalation message must be Craig-instructional, not "
            f"customer-facing. Looked for: {instructional_signals}. "
            f"Got: {msg!r}"
        )
        # And must NOT contain the old customer-facing wording.
        assert "Customer should be asked" not in msg, (
            "Old customer-facing wording leaked through — the v40.8.9 "
            "fix is not in place."
        )

    def test_v40_8_9_size_tool_description_emphasizes_required_for_a_series(self):
        """v40.8.9 — the `size` parameter description in
        quote_large_format must explicitly tell DeepSeek that `size`
        is REQUIRED when the customer mentions an A-series size, not
        just optional."""
        from llm.craig_agent import TOOLS
        large_format = next(t for t in TOOLS
                            if t.get("function", {}).get("name") == "quote_large_format")
        size_desc = large_format["function"]["parameters"]["properties"]["size"]["description"]
        required_signals = [
            "REQUIRED for board products",
            "REQUIRED whenever",
            "EVEN IF the customer only said",
            "Do NOT ask the customer for",
            "ONLY OMIT `size`",
        ]
        present = sum(1 for s in required_signals if s in size_desc)
        assert present >= 3, (
            f"Tool size description must emphasize that `size` is "
            f"REQUIRED for boards with A-series mention. Found "
            f"{present}/{len(required_signals)} markers."
        )

    def test_v40_8_9_prompt_has_board_size_examples(self):
        """v40.8.9 — the prompt's DIRECT TOOL CALL section must include
        explicit examples for the 7 board sizes so DeepSeek
        pattern-matches against them."""
        from llm.craig_agent import CRAIG_SYSTEM_PROMPT
        # Each canonical board phrasing should appear verbatim
        board_examples = [
            "5 corri boards A3",
            "10 foamex boards A1",
            "2 A0 dibond",
            "1 full sheet corri",
            "20 corri boards at 800mm by 600mm",
        ]
        present = sum(1 for ex in board_examples if ex in CRAIG_SYSTEM_PROMPT)
        assert present >= 4, (
            f"Prompt must contain ≥4 verbatim board examples. "
            f"Found {present}/{len(board_examples)}: "
            f"{[ex for ex in board_examples if ex in CRAIG_SYSTEM_PROMPT]}."
        )
        # And the anti-pattern warning
        assert "NEVER ask" in CRAIG_SYSTEM_PROMPT and "mm" in CRAIG_SYSTEM_PROMPT, (
            "Prompt must explicitly forbid asking for mm when A-series "
            "is named."
        )

    def test_v40_8_7_prompt_size_reduced(self):
        """v40.8.7 — sanity check that the prompt didn't bloat further.
        Pre-v40.8.7 was 16,208 chars; v40.8.7 shrunk to 13,454.
        v40.8.9 added ~1,400 chars of board examples + anti-pattern
        warning (necessary to fix Justin's board escalation bug).
        Guardrail raised to 15,500 to accommodate."""
        from llm.craig_agent import CRAIG_SYSTEM_PROMPT
        assert len(CRAIG_SYSTEM_PROMPT) < 15500, (
            f"Prompt is {len(CRAIG_SYSTEM_PROMPT)} chars — should stay "
            f"under 15,500. Check if you accidentally re-added a "
            f"verbose block."
        )
