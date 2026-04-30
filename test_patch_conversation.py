"""
Tests for PATCH /admin/api/orgs/{slug}/conversations/{cid} — Phase E
endpoint that lets Justin edit customer info from the dashboard if
Craig misread / failed to collect a field.
"""

from __future__ import annotations

import os
import time

import jwt
from fastapi.testclient import TestClient

os.environ["STRATEGOS_JWT_SECRET"] = os.environ.get(
    "STRATEGOS_JWT_SECRET", "test-secret-32-bytes-long-padding-enough-now",
)

from app import app  # noqa: E402
from db import db_session  # noqa: E402
from db.models import Conversation, DEFAULT_ORG_SLUG  # noqa: E402

client = TestClient(app)


def _auth(role: str = "client_owner") -> dict[str, str]:
    token = jwt.encode(
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
    return {"Authorization": f"Bearer {token}"}


def _make_conv(**extra) -> int:
    with db_session() as db:
        c = Conversation(
            organization_slug=DEFAULT_ORG_SLUG,
            external_id=f"patch-test-{time.time_ns()}",
            channel="web",
            customer_name="Old Name",
            customer_email="old@example.com",
            messages=[],
        )
        for k, v in extra.items():
            setattr(c, k, v)
        db.add(c); db.commit()
        return c.id


def test_patch_updates_basic_contact():
    cid = _make_conv()
    r = client.patch(
        f"/admin/api/orgs/{DEFAULT_ORG_SLUG}/conversations/{cid}",
        json={"customer_name": "New Name", "customer_email": "new@example.com"},
        headers=_auth(),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["conversation"]["customer_name"] == "New Name"
    assert body["conversation"]["customer_email"] == "new@example.com"


def test_patch_updates_funnel_fields():
    cid = _make_conv()
    r = client.patch(
        f"/admin/api/orgs/{DEFAULT_ORG_SLUG}/conversations/{cid}",
        json={
            "is_company": True,
            "is_returning_customer": True,
            "past_customer_email": "old-acct@example.com",
            "delivery_method": "delivery",
            "delivery_address": {
                "address1": "Unit 7",
                "address4": "Cork",
                "postcode": "T12 X1Y2",
            },
        },
        headers=_auth(),
    )
    assert r.status_code == 200, r.text
    conv = r.json()["conversation"]
    assert conv["is_company"] is True
    assert conv["is_returning_customer"] is True
    assert conv["past_customer_email"] == "old-acct@example.com"
    assert conv["delivery_method"] == "delivery"
    assert conv["delivery_address"] == {
        "address1": "Unit 7",
        "address4": "Cork",
        "postcode": "T12 X1Y2",
    }


def test_patch_partial_does_not_overwrite_other_fields():
    cid = _make_conv(
        is_company=True,
        delivery_method="collect",
    )
    r = client.patch(
        f"/admin/api/orgs/{DEFAULT_ORG_SLUG}/conversations/{cid}",
        json={"customer_phone": "+353 1 555 0000"},
        headers=_auth(),
    )
    assert r.status_code == 200
    conv = r.json()["conversation"]
    # Only phone changed
    assert conv["customer_phone"] == "+353 1 555 0000"
    assert conv["is_company"] is True
    assert conv["delivery_method"] == "collect"
    # Original fields preserved
    assert conv["customer_name"] == "Old Name"
    assert conv["customer_email"] == "old@example.com"


def test_patch_rejects_invalid_delivery_method():
    cid = _make_conv()
    r = client.patch(
        f"/admin/api/orgs/{DEFAULT_ORG_SLUG}/conversations/{cid}",
        json={"delivery_method": "magic-carpet"},
        headers=_auth(),
    )
    assert r.status_code == 422


def test_patch_404_when_conversation_missing():
    r = client.patch(
        f"/admin/api/orgs/{DEFAULT_ORG_SLUG}/conversations/999999",
        json={"customer_name": "X"},
        headers=_auth(),
    )
    assert r.status_code == 404


def test_patch_rejects_unknown_role():
    cid = _make_conv()
    r = client.patch(
        f"/admin/api/orgs/{DEFAULT_ORG_SLUG}/conversations/{cid}",
        json={"customer_name": "X"},
        headers=_auth(role="member"),
    )
    assert r.status_code in (401, 403)
