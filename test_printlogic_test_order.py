"""
Tests for the dashboard test-order endpoints (create/cancel/clear).

We don't hit real PrintLogic here — we patch `printlogic.create_order`
and `printlogic.update_order_status` so the test exercises only our
glue (settings persistence, response shaping, status transitions).

The end-to-end real-API smoke test lives in
`scripts/probe_printlogic_ops_cycle.py` and is run manually against
production keys when needed.
"""

import os
import time
from unittest.mock import patch, AsyncMock

import jwt
from fastapi.testclient import TestClient

os.environ.setdefault(
    "STRATEGOS_JWT_SECRET",
    "test-secret-32-bytes-long-padding-enough-now",
)

from app import app  # noqa: E402
from db import db_session  # noqa: E402
from db.models import DEFAULT_ORG_SLUG, Setting  # noqa: E402

client = TestClient(app)


def _auth(role: str = "client_owner") -> dict[str, str]:
    token = jwt.encode(
        {
            "email": "test@example.com",
            "org_slug": DEFAULT_ORG_SLUG,
            "role": role,
            "iat": int(time.time()),
            "exp": int(time.time()) + 300,
            "iss": "strategos-dashboard",
            "sub": "test@example.com",
        },
        os.environ["STRATEGOS_JWT_SECRET"],
        algorithm="HS256",
    )
    return {"Authorization": f"Bearer {token}"}


def _set(db, key: str, value: str) -> None:
    row = (
        db.query(Setting)
        .filter_by(organization_slug=DEFAULT_ORG_SLUG, key=key)
        .first()
    )
    if row:
        row.value = value
    else:
        db.add(Setting(
            organization_slug=DEFAULT_ORG_SLUG,
            key=key, value=value, value_type="string",
        ))


def _reset_test_order_state(api_key: str = "test-api-key", dry_run: bool = True) -> None:
    keys = [
        "printlogic_api_key",
        "printlogic_dry_run",
        "printlogic_last_test_order_id",
        "printlogic_last_test_order_number",
        "printlogic_last_test_customer_id",
        "printlogic_last_test_marker",
        "printlogic_last_test_created_at",
        "printlogic_last_test_status",
        "printlogic_last_test_dry_run",
    ]
    with db_session() as db:
        _set(db, "printlogic_api_key", api_key)
        _set(db, "printlogic_dry_run", "true" if dry_run else "false")
        for k in keys[2:]:
            _set(db, k, "")


def test_create_test_order_dry_run_returns_synthetic_id():
    _reset_test_order_state(dry_run=True)
    r = client.post(
        f"/admin/api/orgs/{DEFAULT_ORG_SLUG}/integrations/printlogic/test-order",
        headers=_auth(),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["dry_run"] is True
    assert body["order_id"].startswith("DRY-"), body
    assert body["marker"].startswith("[CRAIG-PROBE-DELETE-ME-")
    assert body["status"] == "open"


def test_get_test_order_returns_persisted_after_create():
    _reset_test_order_state(dry_run=True)
    client.post(
        f"/admin/api/orgs/{DEFAULT_ORG_SLUG}/integrations/printlogic/test-order",
        headers=_auth(),
    )
    r = client.get(
        f"/admin/api/orgs/{DEFAULT_ORG_SLUG}/integrations/printlogic/test-order",
        headers=_auth(),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["present"] is True
    assert body["order_id"].startswith("DRY-")
    assert body["status"] == "open"
    assert body["dry_run"] is True


def test_cancel_dry_run_test_order_clears_local_only():
    _reset_test_order_state(dry_run=True)
    client.post(
        f"/admin/api/orgs/{DEFAULT_ORG_SLUG}/integrations/printlogic/test-order",
        headers=_auth(),
    )
    r = client.post(
        f"/admin/api/orgs/{DEFAULT_ORG_SLUG}/integrations/printlogic/test-order/cancel",
        headers=_auth(),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["dry_run"] is True
    # The persisted record should now be marked cancelled
    r2 = client.get(
        f"/admin/api/orgs/{DEFAULT_ORG_SLUG}/integrations/printlogic/test-order",
        headers=_auth(),
    )
    assert r2.json()["status"] == "cancelled"


def test_cancel_with_no_test_order_returns_404():
    _reset_test_order_state(dry_run=True)
    # No create — straight to cancel
    r = client.post(
        f"/admin/api/orgs/{DEFAULT_ORG_SLUG}/integrations/printlogic/test-order/cancel",
        headers=_auth(),
    )
    assert r.status_code == 404


def test_create_without_api_key_rejected():
    with db_session() as db:
        _set(db, "printlogic_api_key", "")
        _set(db, "printlogic_dry_run", "true")
    r = client.post(
        f"/admin/api/orgs/{DEFAULT_ORG_SLUG}/integrations/printlogic/test-order",
        headers=_auth(),
    )
    assert r.status_code == 400
    assert "api_key" in r.json()["detail"].lower()


def test_create_test_order_live_mode_calls_printlogic():
    """When dry_run=false, we call printlogic.create_order with the real
    payload. We mock it here to avoid hitting real PrintLogic."""
    _reset_test_order_state(api_key="real-key", dry_run=False)

    fake_create_response = {
        "ok": True,
        "order_id": "999999",
        "customer_id": "888888",
        "dry_run": False,
        "ambiguous": False,
        "raw": {"order_id": "999999", "order_number": "49999", "customer_id": "888888"},
        "error": None,
    }

    with patch(
        "printlogic.create_order",
        new=AsyncMock(return_value=fake_create_response),
    ) as mock_create:
        r = client.post(
            f"/admin/api/orgs/{DEFAULT_ORG_SLUG}/integrations/printlogic/test-order",
            headers=_auth(),
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is True
        assert body["dry_run"] is False
        assert body["order_id"] == "999999"
        assert body["order_number"] == "49999"
        assert body["customer_id"] == "888888"

        # Verify the call shape — payload must include the marker
        assert mock_create.await_count == 1
        call_args = mock_create.await_args
        payload = call_args.args[0]
        api_key_arg = call_args.args[1]
        assert api_key_arg == "real-key"
        assert payload["customer_name"].startswith("CRAIG-PROBE-DO-NOT-PROCESS-")
        assert "[CRAIG-PROBE-DELETE-ME-" in payload["order_description"]
        assert call_args.kwargs["dry_run"] is False


def test_cancel_test_order_live_mode_calls_update_status():
    """Cancel for a live test order must call update_order_status='Cancelled'."""
    _reset_test_order_state(api_key="real-key", dry_run=False)

    # First create (with the same mock path)
    fake_create = {
        "ok": True, "order_id": "777", "customer_id": "555",
        "dry_run": False, "ambiguous": False,
        "raw": {"order_id": "777", "order_number": "12345", "customer_id": "555"},
        "error": None,
    }
    fake_update = {"ok": True, "raw": {"result": "ok"}, "error": None}

    with patch("printlogic.create_order", new=AsyncMock(return_value=fake_create)):
        client.post(
            f"/admin/api/orgs/{DEFAULT_ORG_SLUG}/integrations/printlogic/test-order",
            headers=_auth(),
        )

    with patch(
        "printlogic.update_order_status",
        new=AsyncMock(return_value=fake_update),
    ) as mock_update:
        r = client.post(
            f"/admin/api/orgs/{DEFAULT_ORG_SLUG}/integrations/printlogic/test-order/cancel",
            headers=_auth(),
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is True
        assert body["dry_run"] is False
        assert body["order_number"] == "12345"

        assert mock_update.await_count == 1
        args = mock_update.await_args.args
        assert args[0] == "12345"
        assert args[1] == "Cancelled"
        assert args[2] == "real-key"


def test_clear_endpoint_resets_state_without_calling_printlogic():
    _reset_test_order_state(dry_run=True)
    client.post(
        f"/admin/api/orgs/{DEFAULT_ORG_SLUG}/integrations/printlogic/test-order",
        headers=_auth(),
    )
    r = client.post(
        f"/admin/api/orgs/{DEFAULT_ORG_SLUG}/integrations/printlogic/test-order/clear",
        headers=_auth(),
    )
    assert r.status_code == 200
    assert r.json()["ok"] is True

    r2 = client.get(
        f"/admin/api/orgs/{DEFAULT_ORG_SLUG}/integrations/printlogic/test-order",
        headers=_auth(),
    )
    assert r2.json() == {"present": False}


def test_member_role_rejected():
    """Test-order endpoints require client_owner — members can't create.
    A non-owner role should be rejected (401 for unknown role, 403 for
    a recognised-but-insufficient one — either is fine, both are gates)."""
    _reset_test_order_state(dry_run=True)
    r = client.post(
        f"/admin/api/orgs/{DEFAULT_ORG_SLUG}/integrations/printlogic/test-order",
        headers=_auth(role="member"),
    )
    assert r.status_code in (401, 403)
