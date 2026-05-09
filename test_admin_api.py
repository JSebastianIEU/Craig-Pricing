"""
Smoke tests for the admin API. Verifies the major endpoints work end-to-end
with a JWT against the existing seeded Just Print data.
"""

import os
import time

import jwt
import pytest
from fastapi.testclient import TestClient

# Ensure secret is set BEFORE importing app
os.environ["STRATEGOS_JWT_SECRET"] = os.environ.get("STRATEGOS_JWT_SECRET", "test-secret-32-bytes-long-padding-enough-now")

from app import app  # noqa: E402

client = TestClient(app)


def _token(role: str = "client_owner", org: str = "just-print", email: str = "test@example.com") -> str:
    return jwt.encode(
        {
            "email": email,
            "org_slug": org,
            "role": role,
            "iat": int(time.time()),
            "exp": int(time.time()) + 300,
            "iss": "strategos-dashboard",
            "sub": email,
        },
        os.environ["STRATEGOS_JWT_SECRET"],
        algorithm="HS256",
    )


def _auth(role: str = "client_owner") -> dict[str, str]:
    return {"Authorization": f"Bearer {_token(role)}"}


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def test_no_token_rejected():
    r = client.get("/admin/api/me")
    assert r.status_code == 401


def test_me_returns_claims():
    r = client.get("/admin/api/me", headers=_auth())
    assert r.status_code == 200
    body = r.json()
    assert body["org_slug"] == "just-print"
    assert body["role"] == "client_owner"


def test_wrong_org_rejected_for_non_admin():
    r = client.get("/admin/api/orgs/other-client/quotes", headers=_auth("client_owner"))
    assert r.status_code == 403


def test_strategos_admin_can_access_any_org():
    r = client.get("/admin/api/orgs/just-print/quotes", headers=_auth("strategos_admin"))
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# Read endpoints (existing seeded data)
# ---------------------------------------------------------------------------


def test_categories_listed():
    r = client.get("/admin/api/orgs/just-print/categories", headers=_auth())
    assert r.status_code == 200
    cats = r.json()["categories"]
    cat_slugs = {c["slug"] for c in cats}
    assert "small_format" in cat_slugs
    assert "large_format" in cat_slugs
    assert "booklet" in cat_slugs


def test_products_listed():
    r = client.get("/admin/api/orgs/just-print/products", headers=_auth())
    assert r.status_code == 200
    products = r.json()["products"]
    assert len(products) >= 26


def test_tax_rates_listed():
    r = client.get("/admin/api/orgs/just-print/tax-rates", headers=_auth())
    assert r.status_code == 200
    rates = r.json()["tax_rates"]
    names = {t["name"] for t in rates}
    assert "standard" in names
    assert "reduced" in names


def test_metrics_endpoint_returns_structure():
    r = client.get("/admin/api/orgs/just-print/metrics", headers=_auth())
    assert r.status_code == 200
    body = r.json()
    assert "totals" in body
    assert "by_channel" in body
    assert "by_status" in body
    assert "top_products" in body
    assert "by_day" in body


# ---------------------------------------------------------------------------
# Write endpoints (CRUD)
# ---------------------------------------------------------------------------


def test_product_create_update_delete_cycle():
    # Create
    r = client.post(
        "/admin/api/orgs/just-print/products",
        headers=_auth(),
        json={
            "name": "Test Sticker Pack",
            "category": "small_format",
            "pricing_strategy": "tiered",
            "description": "Smoke test product",
        },
    )
    assert r.status_code == 201, r.text
    pid = r.json()["product"]["id"]

    # Update
    r = client.patch(
        f"/admin/api/orgs/just-print/products/{pid}",
        headers=_auth(),
        json={"description": "updated"},
    )
    assert r.status_code == 200
    assert r.json()["product"]["description"] == "updated"

    # Add tier
    r = client.post(
        f"/admin/api/orgs/just-print/products/{pid}/tiers",
        headers=_auth(),
        json={"quantity": 100, "price": 25.0},
    )
    assert r.status_code == 201
    tier_id = r.json()["product"]["tiers"][0]["id"]

    # Update tier
    r = client.patch(
        f"/admin/api/orgs/just-print/products/{pid}/tiers/{tier_id}",
        headers=_auth(),
        json={"price": 30.0},
    )
    assert r.status_code == 200
    assert r.json()["product"]["tiers"][0]["price"] == 30.0

    # Delete tier
    r = client.delete(
        f"/admin/api/orgs/just-print/products/{pid}/tiers/{tier_id}",
        headers=_auth(),
    )
    assert r.status_code == 204

    # Delete product
    r = client.delete(f"/admin/api/orgs/just-print/products/{pid}", headers=_auth())
    assert r.status_code == 204


def test_tax_rate_lifecycle():
    # Create
    r = client.post(
        "/admin/api/orgs/just-print/tax-rates",
        headers=_auth(),
        json={
            "name": f"test_rate_{int(time.time())}",
            "rate": 0.08,
            "description": "smoke test",
        },
    )
    assert r.status_code == 201, r.text
    rid = r.json()["tax_rate"]["id"]

    # Update
    r = client.patch(
        f"/admin/api/orgs/just-print/tax-rates/{rid}",
        headers=_auth(),
        json={"rate": 0.10},
    )
    assert r.status_code == 200
    assert r.json()["tax_rate"]["rate"] == 0.10

    # Delete
    r = client.delete(f"/admin/api/orgs/just-print/tax-rates/{rid}", headers=_auth())
    assert r.status_code == 204


def test_default_tax_cannot_be_deleted():
    # Get the default rate
    r = client.get("/admin/api/orgs/just-print/tax-rates", headers=_auth())
    rates = r.json()["tax_rates"]
    default = next(t for t in rates if t["is_default"])
    r = client.delete(
        f"/admin/api/orgs/just-print/tax-rates/{default['id']}",
        headers=_auth(),
    )
    assert r.status_code == 400


def test_role_enforcement():
    # client_viewer cannot create a product
    viewer_auth = {"Authorization": f"Bearer {_token('client_viewer')}"}
    r = client.post(
        "/admin/api/orgs/just-print/products",
        headers=viewer_auth,
        json={"name": "Should fail", "category": "small_format"},
    )
    assert r.status_code == 403


def test_strict_pydantic_rejects_extra_fields():
    r = client.post(
        "/admin/api/orgs/just-print/products",
        headers=_auth(),
        json={
            "name": "x",
            "category": "small_format",
            "unknown_field": "should fail",
        },
    )
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# Integrations status endpoint
# ---------------------------------------------------------------------------


def test_integrations_status_returns_three_blocks():
    """Smoke: endpoint returns the three integrations + computed_at timestamp."""
    r = client.get(
        "/admin/api/orgs/just-print/integrations/status",
        headers=_auth("client_member"),
    )
    assert r.status_code == 200
    body = r.json()
    assert set(body.keys()) >= {"missive", "printlogic", "stripe", "computed_at"}
    for k in ("missive", "printlogic", "stripe"):
        block = body[k]
        # Every block has the same surface contract
        assert "configured" in block
        assert "enabled" in block
        assert "health" in block
        assert block["health"] in ("green", "yellow", "red", "unknown")
        assert "stats_30d" in block


def test_integrations_status_printlogic_yellow_in_dry_run():
    """V13 seeds printlogic_dry_run=true → printlogic should be yellow."""
    r = client.get(
        "/admin/api/orgs/just-print/integrations/status",
        headers=_auth("client_member"),
    )
    assert r.status_code == 200
    pl = r.json()["printlogic"]
    assert pl["dry_run"] is True
    # In dry-run we expect yellow (or unknown if api_key empty, which is the
    # local-dev case). Either way NOT green and NOT red.
    assert pl["health"] in ("yellow", "unknown")


def test_integrations_status_stripe_unknown_when_disabled():
    """V16 seeds stripe_enabled=false by default → unknown."""
    r = client.get(
        "/admin/api/orgs/just-print/integrations/status",
        headers=_auth("client_member"),
    )
    body = r.json()["stripe"]
    assert body["enabled"] is False
    assert body["health"] == "unknown"


def test_integrations_status_requires_auth():
    r = client.get("/admin/api/orgs/just-print/integrations/status")
    assert r.status_code == 401


def test_integrations_status_blocks_other_org_for_client_member():
    r = client.get(
        "/admin/api/orgs/some-other-tenant/integrations/status",
        headers=_auth("client_member"),
    )
    # client_member can only see their own org
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# Rate limiter integration smoke (just confirms 429 fires on /chat)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# v37 — engagement-approval endpoints
# ---------------------------------------------------------------------------


def _make_pending_engagement_conversation(channel: str = "missive"):
    """Helper — create a Conversation in `pending_engagement_approval`
    state with a stashed inbound body so the approve/reject endpoints
    have something to act on."""
    from db import db_session
    from db.models import Conversation
    import uuid as _uuid

    with db_session() as db:
        ext_id = f"test-thread-{_uuid.uuid4().hex[:10]}"
        conv = Conversation(
            organization_slug="just-print",
            channel=channel,
            external_id=ext_id,
            customer_email="bob@example.com",
            customer_name="Bob",
            status="pending_engagement_approval",
            messages=[{"role": "user", "content": "Hi, are you guys around?"}],
            engagement_classification={
                "from": "bob@example.com",
                "subject": "Hi",
                "body_preview": "Hi, are you guys around?",
                "verdict": True,
                "confidence": 0.55,
                "reason": "vague greeting",
                "classified_at": "2026-05-09T10:00:00",
                "missive_message_id": "msg-xxx",
                "missive_subject": "Hi",
            },
        )
        db.add(conv)
        db.commit()
        return conv.id, ext_id


def test_reject_engagement_flips_status():
    cid, _ext = _make_pending_engagement_conversation()
    r = client.post(
        f"/admin/api/orgs/just-print/conversations/{cid}/reject-engagement",
        headers=_auth("client_owner"),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["conversation"]["status"] == "engagement_rejected"
    ec = body["conversation"]["engagement_classification"]
    assert ec.get("rejected_at")
    assert ec.get("rejected_by") == "test@example.com"


def test_reject_engagement_idempotent():
    """A second reject on an already-rejected conversation succeeds
    (does not change rejected_at / rejected_by)."""
    cid, _ext = _make_pending_engagement_conversation()
    r1 = client.post(
        f"/admin/api/orgs/just-print/conversations/{cid}/reject-engagement",
        headers=_auth("client_owner"),
    )
    assert r1.status_code == 200
    first_at = r1.json()["conversation"]["engagement_classification"]["rejected_at"]
    r2 = client.post(
        f"/admin/api/orgs/just-print/conversations/{cid}/reject-engagement",
        headers=_auth("client_owner"),
    )
    assert r2.status_code == 200
    second_at = r2.json()["conversation"]["engagement_classification"]["rejected_at"]
    assert first_at == second_at


def test_reject_engagement_rejects_active_conversation():
    """The endpoint refuses to demote a normal conversation to
    engagement_rejected — only paused or already-rejected ones."""
    from db import db_session
    from db.models import Conversation

    with db_session() as db:
        conv = Conversation(
            organization_slug="just-print",
            channel="web",
            status="active",
            messages=[],
        )
        db.add(conv)
        db.commit()
        cid = conv.id

    r = client.post(
        f"/admin/api/orgs/just-print/conversations/{cid}/reject-engagement",
        headers=_auth("client_owner"),
    )
    assert r.status_code == 400


def test_approve_engagement_runs_craig_and_drafts(monkeypatch):
    """v37 — approve-engagement flips status, calls chat_with_craig with
    the stashed inbound body, and posts a Missive draft. We mock both
    side-effects to avoid LLM + network calls."""
    cid, ext_id = _make_pending_engagement_conversation()

    # Mock chat_with_craig to return a canned reply without burning tokens.
    captured = {"calls": []}

    def fake_chat(*args, **kwargs):
        captured["calls"].append({
            "user_message": kwargs.get("user_message"),
            "external_id": kwargs.get("external_id"),
            "channel": kwargs.get("channel"),
        })
        return {
            "reply": "Hey, sure — what would you like printed?",
            "quote_generated": False,
            "quote_id": None,
            "escalated": False,
            "order_confirmed": False,
            "conversation_id": kwargs.get("conversation_id"),
        }

    monkeypatch.setattr("llm.craig_agent.chat_with_craig", fake_chat)

    # Mock missive.create_draft so we don't try to talk to Missive.
    drafted = {"called": False, "args": None}

    async def fake_create_draft(**kwargs):
        drafted["called"] = True
        drafted["args"] = kwargs
        return {"id": "draft-fake"}

    monkeypatch.setattr("missive.create_draft", fake_create_draft)

    # Force the org to look enabled so the endpoint actually drafts.
    from db import db_session
    from db.models import Setting
    with db_session() as db:
        for k, v in (("missive_enabled", "true"), ("missive_api_token", "fake-token")):
            row = db.query(Setting).filter_by(
                organization_slug="just-print", key=k,
            ).first()
            if row:
                row.value = v
            else:
                db.add(Setting(
                    organization_slug="just-print", key=k, value=v,
                    value_type="string",
                ))
        db.commit()

    r = client.post(
        f"/admin/api/orgs/just-print/conversations/{cid}/approve-engagement",
        headers=_auth("client_owner"),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["conversation"]["status"] == "engagement_approved"
    # The fake chat_with_craig saw the stashed inbound body
    assert any(
        "are you guys around" in (c.get("user_message") or "")
        for c in captured["calls"]
    )
    # And a Missive draft was posted to the original thread
    assert drafted["called"] is True
    assert drafted["args"]["conversation_id"] == ext_id
    assert drafted["args"]["send"] is True


def test_approve_engagement_404_on_missing():
    r = client.post(
        "/admin/api/orgs/just-print/conversations/9999999/approve-engagement",
        headers=_auth("client_owner"),
    )
    assert r.status_code == 404


def test_approve_engagement_fast_path_uses_cached_reply(monkeypatch):
    """v37.1 — when the Tier-2 webhook stashed a pre-rendered reply on
    `engagement_classification.proposed_reply`, the approve endpoint
    posts that exact text to Missive and DOES NOT re-invoke
    chat_with_craig. The customer sees what Justin saw."""
    cid, ext_id = _make_pending_engagement_conversation()

    # Patch the cached reply onto the conversation row to simulate
    # what the webhook would have stashed.
    from db import db_session
    from db.models import Conversation, Setting

    cached_reply = "Hey Bob — sure, what would you like printed?"
    cached_html = "<p>Hey Bob — sure, what would you like printed?</p>"
    cached_subject = "Re: Hi"
    with db_session() as db:
        conv = db.query(Conversation).filter_by(id=cid).first()
        c = dict(conv.engagement_classification or {})
        c.update({
            "proposed_reply": cached_reply,
            "proposed_html": cached_html,
            "proposed_subject": cached_subject,
            "proposed_quote_id": None,
            "proposed_should_send": True,
        })
        conv.engagement_classification = c
        # Make sure Missive looks enabled
        for k, v in (("missive_enabled", "true"), ("missive_api_token", "fake-token")):
            row = db.query(Setting).filter_by(
                organization_slug="just-print", key=k,
            ).first()
            if row:
                row.value = v
            else:
                db.add(Setting(
                    organization_slug="just-print", key=k, value=v,
                    value_type="string",
                ))
        db.commit()

    # If the fast path works, chat_with_craig must NOT be called.
    def boom(*a, **kw):
        raise AssertionError("chat_with_craig should not be called on the v37.1 fast path")

    monkeypatch.setattr("llm.craig_agent.chat_with_craig", boom)

    drafted = {"args": None}

    async def fake_create_draft(**kwargs):
        drafted["args"] = kwargs
        return {"id": "draft-fast"}

    monkeypatch.setattr("missive.create_draft", fake_create_draft)

    r = client.post(
        f"/admin/api/orgs/just-print/conversations/{cid}/approve-engagement",
        headers=_auth("client_owner"),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["drafted"] is True
    assert body["conversation"]["status"] == "engagement_approved"
    # The exact cached HTML + subject got shipped to Missive
    assert drafted["args"] is not None
    assert drafted["args"]["html_body"] == cached_html
    assert drafted["args"]["subject"] == cached_subject
    assert drafted["args"]["conversation_id"] == ext_id


def test_reject_engagement_cascades_pending_quote():
    """v37.1 — rejecting engagement marks any pending Quote produced by
    the Tier-2 preview as `rejected` so it disappears from the active
    operator queue."""
    cid, _ext = _make_pending_engagement_conversation()

    from db import db_session
    from db.models import Conversation, Quote
    with db_session() as db:
        # Add a pending quote linked to this conversation, mimicking
        # the webhook's Tier-2 chat_with_craig run.
        q = Quote(
            conversation_id=cid,
            organization_slug="just-print",
            product_key="business_cards",
            specs={"quantity": 500},
            base_price=120.00,
            final_price_ex_vat=145.00,
            vat_amount=33.35,
            final_price_inc_vat=178.35,
            status="pending_approval",
        )
        db.add(q)
        db.commit()
        qid = q.id
        # Stash the proposed_quote_id on the conversation
        conv = db.query(Conversation).filter_by(id=cid).first()
        c = dict(conv.engagement_classification or {})
        c["proposed_quote_id"] = qid
        conv.engagement_classification = c
        db.commit()

    r = client.post(
        f"/admin/api/orgs/just-print/conversations/{cid}/reject-engagement",
        headers=_auth("client_owner"),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert qid in body["quotes_rejected"]

    # Verify in the DB
    from db import db_session
    from db.models import Quote
    with db_session() as db:
        q = db.query(Quote).filter_by(id=qid).first()
        assert q.status == "rejected"


def test_approve_engagement_blocks_wrong_status():
    """approve-engagement on an already-approved conversation succeeds
    (idempotent), but on an unrelated active conversation it 400s."""
    from db import db_session
    from db.models import Conversation

    with db_session() as db:
        conv = Conversation(
            organization_slug="just-print",
            channel="web",
            status="active",
            messages=[],
        )
        db.add(conv)
        db.commit()
        cid = conv.id

    r = client.post(
        f"/admin/api/orgs/just-print/conversations/{cid}/approve-engagement",
        headers=_auth("client_owner"),
    )
    assert r.status_code == 400


def test_chat_endpoint_rate_limit_fires_at_threshold():
    """30 req/min on /chat (rate_limit('chat', 30)). 31st should 429.

    Uses unique X-Forwarded-For per test so other tests don't bleed into this
    bucket and vice versa.
    """
    import rate_limiter
    rate_limiter._reset_for_tests()
    headers = {"X-Forwarded-For": "203.0.113.99"}  # TEST-NET-3, can't be real
    # 30 should pass (well, rate-limit-wise — they may fail on body validation,
    # but that's after the dependency runs).
    seen_429 = False
    for _ in range(35):
        r = client.post("/chat", json={"message": "test"}, headers=headers)
        if r.status_code == 429:
            seen_429 = True
            break
    assert seen_429, "Expected at least one 429 within 35 requests"
    rate_limiter._reset_for_tests()
