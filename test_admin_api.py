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
