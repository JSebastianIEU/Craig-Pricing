"""
Unit tests for the PrintLogic thin client + push orchestrator.

ALL network traffic is mocked via `respx`. These tests are the safety
gate: they run before any `printlogic_dry_run=false` flag is flipped in
production. If anything here fails, we do NOT push to real PrintLogic.

Run:  pytest test_printlogic.py -q
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

import pytest
import respx
import httpx

sys.path.insert(0, os.path.dirname(__file__))

import printlogic


FAKE_KEY = "test-api-key-12345"
PL_URL = f"https://www.printlogicsystem.com/api.php?api_key={FAKE_KEY}"


# ---------------------------------------------------------------------------
# create_order — the ONE destructive call that needs bulletproof coverage
# ---------------------------------------------------------------------------


def test_create_order_dry_run_zero_network_calls():
    """Dry-run MUST NOT touch the network. Core safety invariant."""
    # assert_all_called=False because the whole point of this test is that
    # the registered mock route never fires. respx's default would fail
    # on the ELSE-path we want.
    with respx.mock(assert_all_called=False) as mock:
        route = mock.post(printlogic.PL_BASE).mock(
            return_value=httpx.Response(500, text="THIS SHOULD NEVER RUN")
        )
        result = asyncio.run(
            printlogic.create_order({"foo": "bar"}, FAKE_KEY, dry_run=True, quote_id_for_dry=42)
        )
    assert route.called is False, "Dry-run MUST NOT make any HTTP call"
    assert result["ok"] is True
    assert result["dry_run"] is True
    assert result["order_id"].startswith("DRY-"), f"Expected DRY- prefix, got {result['order_id']}"
    assert result["error"] is None


def test_create_order_sends_json_with_api_key_in_query():
    """Verify wire-format matches PrintLogic's spec (JSON body, api_key in URL)."""
    with respx.mock() as mock:
        route = mock.post(printlogic.PL_BASE).mock(
            return_value=httpx.Response(200, json={"status": "ok", "order_id": "154879", "customer_id": "95479"})
        )
        asyncio.run(
            printlogic.create_order(
                {"customer_name": "Test", "order_items": []}, FAKE_KEY, dry_run=False,
            )
        )

    assert route.called
    req = route.calls[0].request
    assert "api_key=" in str(req.url), f"api_key must be in query, got {req.url}"
    assert req.headers["content-type"].startswith("application/json")
    body = json.loads(req.content)
    assert body["action"] == "create_order"
    assert body["customer_name"] == "Test"


def test_create_order_returns_order_id_on_success():
    """Happy path — 200 with order_id → ok=True, correct id extracted."""
    with respx.mock() as mock:
        mock.post(printlogic.PL_BASE).mock(
            return_value=httpx.Response(200, json={
                "status": "ok", "order_id": "154879", "customer_id": "95479",
            })
        )
        result = asyncio.run(printlogic.create_order({}, FAKE_KEY, dry_run=False))

    assert result["ok"] is True
    assert result["order_id"] == "154879"
    assert result["customer_id"] == "95479"
    assert result["ambiguous"] is False
    assert result["error"] is None


def test_create_order_ambiguous_response_flagged():
    """
    The real probe returned {"result":"ok","request_length":1,
    "post_length":0,"raw_body_length":53} for a nonexistent order. We
    must NOT treat this as success.
    """
    with respx.mock() as mock:
        mock.post(printlogic.PL_BASE).mock(
            return_value=httpx.Response(200, json={
                "result": "ok", "request_length": 1,
                "post_length": 0, "raw_body_length": 53,
            })
        )
        result = asyncio.run(printlogic.create_order({}, FAKE_KEY, dry_run=False))

    assert result["ok"] is False
    assert result["ambiguous"] is True
    assert result["order_id"] is None
    assert result["error"] == "ambiguous_ok"


def test_create_order_401_sets_error_no_order_id():
    """401 unauthorized → ok=False, error captured, no id."""
    with respx.mock() as mock:
        mock.post(printlogic.PL_BASE).mock(
            return_value=httpx.Response(401, json={"error": "bad api key"})
        )
        result = asyncio.run(printlogic.create_order({}, FAKE_KEY, dry_run=False))

    assert result["ok"] is False
    assert result["order_id"] is None
    assert result["error"].startswith("http_401")
    assert result["ambiguous"] is False


def test_create_order_400_bad_payload_captures_body():
    """400 → caller gets the body preview so we can debug what went wrong."""
    with respx.mock() as mock:
        mock.post(printlogic.PL_BASE).mock(
            return_value=httpx.Response(400, text="missing customer_name")
        )
        result = asyncio.run(printlogic.create_order({}, FAKE_KEY, dry_run=False))

    assert result["ok"] is False
    assert "missing customer_name" in result["error"]


def test_create_order_timeout_returns_error():
    """Network timeout → error='timeout', no id, no ambiguous flag."""
    with respx.mock() as mock:
        mock.post(printlogic.PL_BASE).mock(
            side_effect=httpx.TimeoutException("timeout")
        )
        result = asyncio.run(printlogic.create_order({}, FAKE_KEY, dry_run=False))

    assert result["ok"] is False
    assert result["error"] == "timeout"
    assert result["order_id"] is None


# ---------------------------------------------------------------------------
# find_customer — read-only, essential for dedup
# ---------------------------------------------------------------------------


def test_find_customer_returns_customer_on_hit():
    with respx.mock() as mock:
        mock.post(printlogic.PL_BASE).mock(
            return_value=httpx.Response(200, json={
                "customer_id": "95479", "customer_name": "Jane Doe",
                "email": "jane@example.com",
            })
        )
        result = asyncio.run(printlogic.find_customer(FAKE_KEY, email="jane@example.com"))

    assert result["ok"] is True
    assert result["customer"]["customer_id"] == "95479"
    assert result["error"] is None


def test_find_customer_returns_none_on_miss():
    """A 200 with no customer_id is a miss, not an error."""
    with respx.mock() as mock:
        mock.post(printlogic.PL_BASE).mock(
            return_value=httpx.Response(200, json={"result": "not found"})
        )
        result = asyncio.run(printlogic.find_customer(FAKE_KEY, email="nobody@example.com"))

    assert result["ok"] is True
    assert result["customer"] is None


def test_find_customer_refuses_empty_args():
    """No identifier passed → refuse without touching the network."""
    # assert_all_called=False: same reason as the dry_run test — we are
    # verifying that the mock route is NOT called.
    with respx.mock(assert_all_called=False) as mock:
        route = mock.post(printlogic.PL_BASE)
        result = asyncio.run(printlogic.find_customer(FAKE_KEY))
    assert result["ok"] is False
    assert result["error"] == "no_identifier"
    assert route.called is False


# ---------------------------------------------------------------------------
# get_order_detail — the Stage 1 auth-validation probe
# ---------------------------------------------------------------------------


def test_get_order_detail_happy_path():
    with respx.mock() as mock:
        mock.post(printlogic.PL_BASE).mock(
            return_value=httpx.Response(200, json={
                "order_number": "1519487", "order_total": "164.58",
                "order_contact": "Justin",
            })
        )
        result = asyncio.run(printlogic.get_order_detail("1519487", FAKE_KEY))

    assert result["ok"] is True
    assert result["order"]["order_number"] == "1519487"
    assert result["ambiguous"] is False


def test_get_order_detail_ambiguous_shape_rejected():
    """The EXACT response we saw from the real probe — must be rejected."""
    with respx.mock() as mock:
        mock.post(printlogic.PL_BASE).mock(
            return_value=httpx.Response(200, json={
                "result": "ok", "request_length": 1,
                "post_length": 0, "raw_body_length": 53,
            })
        )
        result = asyncio.run(printlogic.get_order_detail("000000", FAKE_KEY))

    assert result["ok"] is False
    assert result["ambiguous"] is True
    assert result["error"] == "ambiguous_ok"


# ---------------------------------------------------------------------------
# update_order_status — rollback path
# ---------------------------------------------------------------------------


def test_update_order_status_cancel_happy_path():
    with respx.mock() as mock:
        route = mock.post(printlogic.PL_BASE).mock(
            return_value=httpx.Response(200, json={"status": "ok"})
        )
        result = asyncio.run(printlogic.update_order_status("154879", "Cancelled", FAKE_KEY))

    assert result["ok"] is True
    assert result["error"] is None
    body = json.loads(route.calls[0].request.content)
    assert body["action"] == "update_order_status"
    # PrintLogic's update_order_status expects `order_id`, not
    # `order_number` (verified live 2026-05-01 against order 2925490
    # — order_number returns "no such order").
    assert body["order_id"] == "154879"
    assert body["status"] == "Cancelled"


def test_update_order_status_rejection_captured():
    with respx.mock() as mock:
        mock.post(printlogic.PL_BASE).mock(
            return_value=httpx.Response(200, json={"error": "cannot cancel"})
        )
        result = asyncio.run(printlogic.update_order_status("154879", "Cancelled", FAKE_KEY))

    assert result["ok"] is False
    assert result["error"] == "unexpected_response"


# ---------------------------------------------------------------------------
# Never logs the api_key
# ---------------------------------------------------------------------------


def test_dry_run_log_does_not_leak_api_key(capsys):
    """Dry-run prints a log line — ensure the api_key is not in it."""
    with respx.mock(assert_all_called=False):
        asyncio.run(
            printlogic.create_order({"foo": FAKE_KEY}, FAKE_KEY, dry_run=True, quote_id_for_dry=1)
        )
    captured = capsys.readouterr()
    # The payload could reference FAKE_KEY if a caller accidentally put it in
    # the payload (not our case), but the log itself must not include the api_key.
    # We check only for the url-level `api_key=` marker to be absent.
    assert "api_key=" not in captured.out


# ---------------------------------------------------------------------------
# Orchestrator: printlogic_push.push_quote — idempotency + dry→real promotion
# + non-throwing error handling
# ---------------------------------------------------------------------------


@pytest.fixture
def _push_db():
    """Isolated in-memory SQLite with the bare-minimum rows for push_quote
    to have something to work with. Doesn't touch the real craig.db."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from db.models import Base, Conversation, Quote, Setting, DEFAULT_ORG_SLUG

    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    db = Session()

    # Tenant settings — dry_run ON by default, api_key populated.
    db.add(Setting(organization_slug=DEFAULT_ORG_SLUG, key="printlogic_api_key",
                   value=FAKE_KEY, value_type="string"))
    db.add(Setting(organization_slug=DEFAULT_ORG_SLUG, key="printlogic_dry_run",
                   value="true", value_type="string"))

    conv = Conversation(
        organization_slug=DEFAULT_ORG_SLUG, external_id="test", channel="web",
        customer_name="Jane Doe", customer_email="jane@example.com", messages=[],
    )
    db.add(conv); db.flush()

    quote = Quote(
        organization_slug=DEFAULT_ORG_SLUG, conversation_id=conv.id,
        product_key="flyers_a5",
        specs={"quantity": 500, "finish": "silk", "double_sided": True},
        base_price=145.0, surcharges=[], final_price_ex_vat=145.0,
        vat_amount=19.58, final_price_inc_vat=164.58, artwork_cost=0.0,
        total=164.58, status="pending_approval",
    )
    db.add(quote); db.flush()

    yield db, quote
    db.close()


def _set_dry_run(db, value: str) -> None:
    from db.models import Setting, DEFAULT_ORG_SLUG
    row = db.query(Setting).filter_by(
        organization_slug=DEFAULT_ORG_SLUG, key="printlogic_dry_run",
    ).first()
    row.value = value
    db.flush()


def test_push_quote_dry_run_sets_DRY_prefix_no_network(_push_db):
    import printlogic_push
    db, quote = _push_db
    with respx.mock(assert_all_called=False) as mock:
        route = mock.post(printlogic.PL_BASE).mock(
            return_value=httpx.Response(500, text="should not fire"),
        )
        from db.models import DEFAULT_ORG_SLUG
        result = printlogic_push.push_quote(db, quote, DEFAULT_ORG_SLUG)

    assert route.called is False, "dry_run must not hit the network"
    assert result["ok"] is True
    assert result["dry_run"] is True
    assert result["order_id"].startswith("DRY-")
    # Persisted on the quote row
    assert quote.printlogic_order_id == result["order_id"]
    assert quote.printlogic_push_attempts == 1


def test_push_quote_idempotent_when_real_order_id_set(_push_db):
    """Second call after a real push returns the existing id, no HTTP."""
    import printlogic_push
    from db.models import DEFAULT_ORG_SLUG
    db, quote = _push_db
    # Pre-seed as if we'd already pushed a real order
    quote.printlogic_order_id = "154879"
    quote.printlogic_customer_id = "95479"
    db.flush()

    with respx.mock(assert_all_called=False) as mock:
        route = mock.post(printlogic.PL_BASE)
        result = printlogic_push.push_quote(db, quote, DEFAULT_ORG_SLUG)

    assert route.called is False
    assert result["already_pushed"] is True
    assert result["order_id"] == "154879"
    # attempts counter should not have been bumped on idempotent hit
    assert quote.printlogic_push_attempts == 0


def test_push_quote_promotes_dry_run_to_real(_push_db):
    """
    If quote has a DRY-xxxx id and dry_run is flipped to false, the next
    push DOES hit the real API and overwrites the DRY id.
    """
    import printlogic_push
    from db.models import DEFAULT_ORG_SLUG
    db, quote = _push_db
    quote.printlogic_order_id = "DRY-ABC12345"
    db.flush()
    _set_dry_run(db, "false")

    with respx.mock() as mock:
        mock.post(printlogic.PL_BASE).mock(side_effect=[
            httpx.Response(200, json={"customer_id": "95479"}),  # find_customer
            httpx.Response(200, json={
                "status": "ok", "order_id": "201234", "customer_id": "95479",
            }),  # create_order
        ])
        result = printlogic_push.push_quote(db, quote, DEFAULT_ORG_SLUG)

    assert result["ok"] is True
    assert result["dry_run"] is False
    assert result["order_id"] == "201234"
    assert quote.printlogic_order_id == "201234"
    # No longer DRY-*


def test_push_quote_captures_http_error_without_raising(_push_db):
    """
    If PrintLogic returns an error, push_quote stores the error on the
    Quote and returns ok=False — it does NOT raise. This keeps the
    confirm_order tool reply path safe.
    """
    import printlogic_push
    from db.models import DEFAULT_ORG_SLUG
    db, quote = _push_db
    _set_dry_run(db, "false")

    with respx.mock() as mock:
        mock.post(printlogic.PL_BASE).mock(side_effect=[
            httpx.Response(200, json={"result": "not found"}),     # find_customer miss
            httpx.Response(400, text="missing delivery_address1"),  # create_order fails
        ])
        result = printlogic_push.push_quote(db, quote, DEFAULT_ORG_SLUG)

    assert result["ok"] is False
    assert result["order_id"] is None
    assert "missing delivery_address1" in (result["error"] or "")
    # Persisted
    assert quote.printlogic_order_id is None
    assert "missing delivery_address1" in (quote.printlogic_last_error or "")
    assert quote.printlogic_push_attempts == 1


def test_push_quote_handles_ambiguous_response(_push_db):
    """The real-probe `{"result":"ok", raw_body_length:...}` shape is flagged."""
    import printlogic_push
    from db.models import DEFAULT_ORG_SLUG
    db, quote = _push_db
    _set_dry_run(db, "false")

    with respx.mock() as mock:
        mock.post(printlogic.PL_BASE).mock(side_effect=[
            httpx.Response(200, json={"result": "not found"}),
            httpx.Response(200, json={
                "result": "ok", "request_length": 1,
                "post_length": 0, "raw_body_length": 53,
            }),
        ])
        result = printlogic_push.push_quote(db, quote, DEFAULT_ORG_SLUG)

    assert result["ok"] is False
    assert result["ambiguous"] is True
    assert quote.printlogic_last_error == "ambiguous_ok"
    assert quote.printlogic_order_id is None


def test_push_quote_refuses_real_push_without_api_key(_push_db):
    """When dry_run=false but api_key is empty, refuse cleanly (no network)."""
    import printlogic_push
    from db.models import DEFAULT_ORG_SLUG, Setting
    db, quote = _push_db
    # Clear the api_key
    row = db.query(Setting).filter_by(
        organization_slug=DEFAULT_ORG_SLUG, key="printlogic_api_key",
    ).first()
    row.value = ""
    _set_dry_run(db, "false")
    db.flush()

    with respx.mock(assert_all_called=False) as mock:
        route = mock.post(printlogic.PL_BASE)
        result = printlogic_push.push_quote(db, quote, DEFAULT_ORG_SLUG)

    assert route.called is False
    assert result["ok"] is False
    assert result["error"] == "no_api_key"
