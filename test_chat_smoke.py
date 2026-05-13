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
