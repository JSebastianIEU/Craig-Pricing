"""
Tests for the Phase D dashboard "Approve" flow.

When Justin clicks Approve on a Quote in the dashboard
(PATCH /admin/api/orgs/{slug}/quotes/{id} with status=approved),
the endpoint must:

  1. Flip Quote.status to 'approved' and stamp approved_by
  2. Auto-create a Stripe Payment Link (best-effort)
  3. Auto-create a Missive outbound draft with the PDF + URL (best-effort)
  4. NOT push to PrintLogic (that's a separate manual button)
  5. Be idempotent — re-approving must not double-fire

We mock both stripe_push.create_link_for_quote and
missive_outbound.send_quote_draft so the test exercises ONLY the
endpoint's orchestration, not the integrations themselves (those have
their own dedicated tests).
"""

from __future__ import annotations

import os
import time
from unittest.mock import patch

import jwt
from fastapi.testclient import TestClient

os.environ["STRATEGOS_JWT_SECRET"] = os.environ.get(
    "STRATEGOS_JWT_SECRET", "test-secret-32-bytes-long-padding-enough-now",
)

from app import app  # noqa: E402
from db import db_session  # noqa: E402
from db.models import (  # noqa: E402
    Conversation, Quote, Setting, DEFAULT_ORG_SLUG,
)


client = TestClient(app)


def _token(role: str = "client_owner") -> str:
    return jwt.encode(
        {
            "email": "justin@just-print.ie",
            "org_slug": DEFAULT_ORG_SLUG,
            "role": role,
            "iat": int(time.time()),
            "exp": int(time.time()) + 300,
            "iss": "strategos-dashboard",
            "sub": "justin@just-print.ie",
        },
        os.environ["STRATEGOS_JWT_SECRET"],
        algorithm="HS256",
    )


def _auth(role: str = "client_owner") -> dict[str, str]:
    return {"Authorization": f"Bearer {_token(role)}"}


def _make_quote_in_pending() -> int:
    """Seed a fresh Conversation + Quote in pending_approval, return the quote_id."""
    with db_session() as db:
        conv = Conversation(
            organization_slug=DEFAULT_ORG_SLUG,
            external_id=f"approve-test-{time.time_ns()}",
            channel="web",
            customer_name="Sebastian Test",
            customer_email="seb@example.com",
            messages=[],
            status="open",
        )
        db.add(conv); db.flush()
        q = Quote(
            organization_slug=DEFAULT_ORG_SLUG,
            conversation_id=conv.id,
            product_key="business_cards",
            specs={"quantity": 500},
            base_price=219.0, surcharges=[],
            final_price_ex_vat=219.0, vat_amount=50.37,
            final_price_inc_vat=269.37, artwork_cost=0.0,
            total=269.37, status="pending_approval",
        )
        db.add(q); db.commit()
        return q.id


def test_approve_fires_stripe_and_missive_integrations():
    """The single most important test for this feature."""
    qid = _make_quote_in_pending()

    fake_stripe = {
        "ok": True, "url": "https://buy.stripe.com/test_xyz",
        "disabled": False, "error": None,
    }
    fake_missive = {
        "ok": True, "skipped": False, "skip_reason": None,
        "draft_id": "draft_abc123", "error": None,
    }

    with patch(
        "stripe_push.create_link_for_quote",
        return_value=fake_stripe,
    ) as mock_stripe, patch(
        "missive_outbound.send_quote_draft",
        return_value=fake_missive,
    ) as mock_missive:
        r = client.patch(
            f"/admin/api/orgs/{DEFAULT_ORG_SLUG}/quotes/{qid}",
            json={"status": "approved"},
            headers=_auth(),
        )

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["quote"]["status"] == "approved"
    assert body["quote"]["approved_by"] == "justin@just-print.ie"

    # Both integrations were invoked
    assert mock_stripe.call_count == 1
    assert mock_missive.call_count == 1

    # Endpoint surfaced the result for the dashboard to display
    assert body["integrations"]["stripe"]["ok"] is True
    assert body["integrations"]["stripe"]["url"] == "https://buy.stripe.com/test_xyz"
    assert body["integrations"]["missive"]["ok"] is True
    assert body["integrations"]["missive"]["draft_id"] == "draft_abc123"


def test_approve_does_NOT_push_to_printlogic():
    """PrintLogic push is a separate manual button — approve must not fire it."""
    qid = _make_quote_in_pending()

    with patch("stripe_push.create_link_for_quote", return_value={"ok": False, "url": None, "disabled": True, "error": None}), \
         patch("missive_outbound.send_quote_draft", return_value={"ok": False, "skipped": True, "skip_reason": "disabled", "draft_id": None, "error": None}), \
         patch("printlogic_push.push_quote") as mock_pl:
        r = client.patch(
            f"/admin/api/orgs/{DEFAULT_ORG_SLUG}/quotes/{qid}",
            json={"status": "approved"},
            headers=_auth(),
        )
    assert r.status_code == 200
    assert mock_pl.call_count == 0, "Approve must NOT push to PrintLogic"


def test_approve_is_idempotent_does_not_double_fire():
    """Re-PATCHing approved (e.g. updating notes) must not re-fire integrations."""
    qid = _make_quote_in_pending()

    fake_stripe = {"ok": True, "url": "https://buy.stripe.com/x", "disabled": False, "error": None}
    fake_missive = {"ok": True, "skipped": False, "skip_reason": None, "draft_id": "d1", "error": None}

    # First approve — fires
    with patch("stripe_push.create_link_for_quote", return_value=fake_stripe) as ms1, \
         patch("missive_outbound.send_quote_draft", return_value=fake_missive) as mm1:
        r1 = client.patch(
            f"/admin/api/orgs/{DEFAULT_ORG_SLUG}/quotes/{qid}",
            json={"status": "approved"},
            headers=_auth(),
        )
    assert r1.status_code == 200
    assert ms1.call_count == 1
    assert mm1.call_count == 1

    # Second PATCH approved (same status, e.g. notes change) — must NOT fire
    with patch("stripe_push.create_link_for_quote") as ms2, \
         patch("missive_outbound.send_quote_draft") as mm2:
        r2 = client.patch(
            f"/admin/api/orgs/{DEFAULT_ORG_SLUG}/quotes/{qid}",
            json={"status": "approved", "notes": "second pass"},
            headers=_auth(),
        )
    assert r2.status_code == 200
    assert ms2.call_count == 0, "Stripe must not re-fire when already approved"
    assert mm2.call_count == 0, "Missive must not re-fire when already approved"


def test_pending_to_rejected_does_not_fire_integrations():
    """Only approved triggers integrations — rejected/sent/etc. don't."""
    qid = _make_quote_in_pending()

    with patch("stripe_push.create_link_for_quote") as mock_stripe, \
         patch("missive_outbound.send_quote_draft") as mock_missive:
        r = client.patch(
            f"/admin/api/orgs/{DEFAULT_ORG_SLUG}/quotes/{qid}",
            json={"status": "rejected", "notes": "Customer changed mind"},
            headers=_auth(),
        )
    assert r.status_code == 200
    assert mock_stripe.call_count == 0
    assert mock_missive.call_count == 0


def test_approve_returns_clean_dict_when_integrations_fail():
    """If Stripe / Missive fail, the status change still commits + returns 200,
    with the failure surfaced in the integrations dict."""
    qid = _make_quote_in_pending()

    with patch(
        "stripe_push.create_link_for_quote",
        side_effect=RuntimeError("Stripe down"),
    ), patch(
        "missive_outbound.send_quote_draft",
        side_effect=RuntimeError("Missive 401"),
    ):
        r = client.patch(
            f"/admin/api/orgs/{DEFAULT_ORG_SLUG}/quotes/{qid}",
            json={"status": "approved"},
            headers=_auth(),
        )
    assert r.status_code == 200
    body = r.json()
    # Status STILL flipped — integrations are best-effort
    assert body["quote"]["status"] == "approved"
    # Failures surfaced to the dashboard
    assert body["integrations"]["stripe"]["ok"] is False
    assert "stripe_crashed" in (body["integrations"]["stripe"]["error"] or "")
    assert body["integrations"]["missive"]["ok"] is False
    assert "missive_crashed" in (body["integrations"]["missive"]["error"] or "")
