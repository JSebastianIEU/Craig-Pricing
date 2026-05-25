"""
v40 — marketing attribution tests.

Covers the merge rules (first-touch write-once, last-touch always),
the unknown-key scrub, the identity backfill, and that the API models
(/chat ChatRequest, customer-info CustomerInfoForm) now accept the
`attribution` field despite being extra="forbid".
"""

from __future__ import annotations

import os
import uuid

import pytest

os.environ.setdefault(
    "STRATEGOS_JWT_SECRET",
    "test-secret-32-bytes-long-padding-enough-now",
)

from db import db_session  # noqa: E402
from db.models import Conversation, DEFAULT_ORG_SLUG  # noqa: E402
from attribution import (  # noqa: E402
    merge_attribution, backfill_attribution_by_identity,
)


ORG = DEFAULT_ORG_SLUG


# ===========================================================================
# merge_attribution
# ===========================================================================


class TestMergeAttribution:
    def test_first_touch_is_write_once(self):
        conv = Conversation(organization_slug=ORG, channel="web")
        changed = merge_attribution(conv, {
            "first_touch": {"utm_source": "google", "gclid": "abc123"},
            "last_touch": {"utm_source": "google", "gclid": "abc123"},
        })
        assert changed is True
        assert conv.attribution["first_touch"]["utm_source"] == "google"
        assert conv.attribution["first_touch"]["gclid"] == "abc123"

        # A later visit from a different source must NOT overwrite first_touch.
        merge_attribution(conv, {
            "first_touch": {"utm_source": "newsletter"},
            "last_touch": {"utm_source": "newsletter"},
        })
        assert conv.attribution["first_touch"]["utm_source"] == "google"
        assert conv.attribution["last_touch"]["utm_source"] == "newsletter"

    def test_last_touch_always_updates(self):
        conv = Conversation(organization_slug=ORG, channel="web")
        merge_attribution(conv, {"last_touch": {"utm_source": "a"}})
        merge_attribution(conv, {"last_touch": {"utm_source": "b"}})
        assert conv.attribution["last_touch"]["utm_source"] == "b"

    def test_empty_payload_is_noop(self):
        conv = Conversation(organization_slug=ORG, channel="web")
        assert merge_attribution(conv, {}) is False
        assert merge_attribution(conv, None) is False
        assert conv.attribution in (None, {})

    def test_flat_payload_treated_as_single_touch(self):
        conv = Conversation(organization_slug=ORG, channel="web")
        merge_attribution(conv, {"utm_source": "google", "fbclid": "z"})
        assert conv.attribution["first_touch"]["utm_source"] == "google"
        assert conv.attribution["last_touch"]["fbclid"] == "z"

    def test_unknown_keys_are_dropped(self):
        conv = Conversation(organization_slug=ORG, channel="web")
        merge_attribution(conv, {
            "first_touch": {"utm_source": "ok", "evil": "DROP", "x": 1},
        })
        first = conv.attribution["first_touch"]
        assert first == {"utm_source": "ok"}, f"unexpected keys kept: {first}"

    def test_all_click_ids_pass_through(self):
        conv = Conversation(organization_slug=ORG, channel="web")
        touch = {
            "utm_source": "s", "utm_medium": "m", "utm_campaign": "c",
            "utm_term": "t", "utm_content": "co",
            "gclid": "g", "gbraid": "gb", "wbraid": "wb",
            "fbclid": "fb", "fbc": "fbc1", "fbp": "fbp1",
            "ttclid": "tt", "msclkid": "ms", "li_fat_id": "li",
        }
        merge_attribution(conv, {"first_touch": touch})
        for k, v in touch.items():
            assert conv.attribution["first_touch"][k] == v


# ===========================================================================
# backfill_attribution_by_identity
# ===========================================================================


class TestBackfillByIdentity:
    def test_backfill_copies_first_touch_from_prior_web_session(self):
        email = f"backfill-{uuid.uuid4().hex[:8]}@example.com"
        prior_ext = f"web-prior-{uuid.uuid4().hex[:6]}"

        with db_session() as db:
            prior = Conversation(
                organization_slug=ORG, channel="web",
                external_id=prior_ext,
                customer_email=email,
                attribution={
                    "first_touch": {"utm_source": "google", "gclid": "abc"},
                    "last_touch": {"utm_source": "google", "gclid": "abc"},
                },
            )
            db.add(prior)
            db.commit()

            # A later conversation by the same person, no attribution yet.
            later = Conversation(
                organization_slug=ORG, channel="email",
                external_id=f"email-{uuid.uuid4().hex[:6]}",
                customer_email=email,
            )
            db.add(later)
            db.flush()

            did = backfill_attribution_by_identity(db, later)
            assert did is True
            assert later.attribution["first_touch"]["utm_source"] == "google"
            assert prior_ext in later.attribution["merged_from"]

    def test_backfill_noop_when_already_has_first_touch(self):
        with db_session() as db:
            conv = Conversation(
                organization_slug=ORG, channel="web",
                customer_email=f"x-{uuid.uuid4().hex[:8]}@example.com",
                attribution={"first_touch": {"utm_source": "direct"}},
            )
            db.add(conv)
            db.flush()
            assert backfill_attribution_by_identity(db, conv) is False

    def test_backfill_noop_when_no_identity(self):
        with db_session() as db:
            conv = Conversation(organization_slug=ORG, channel="web")
            db.add(conv)
            db.flush()
            assert backfill_attribution_by_identity(db, conv) is False


# ===========================================================================
# API models accept the new field (both are extra="forbid")
# ===========================================================================


class TestApiModelsAcceptAttribution:
    def test_chat_request_accepts_attribution(self):
        from app import ChatRequest
        req = ChatRequest(
            message="hi",
            attribution={"last_touch": {"utm_source": "google"}},
        )
        assert req.attribution["last_touch"]["utm_source"] == "google"

    def test_customer_info_form_accepts_attribution(self):
        from widget_api import CustomerInfoForm
        form = CustomerInfoForm(
            external_id="web-abc",
            name="Jane Doe",
            email="jane@example.com",
            delivery_method="collect",
            attribution={"first_touch": {"gclid": "abc123"}},
        )
        assert form.attribution["first_touch"]["gclid"] == "abc123"

    def test_customer_info_form_still_rejects_unknown_field(self):
        """Sanity — extra='forbid' still blocks junk fields, we only
        opened up `attribution`."""
        from widget_api import CustomerInfoForm
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            CustomerInfoForm(
                external_id="web-abc",
                name="Jane Doe",
                email="jane@example.com",
                delivery_method="collect",
                some_random_field="nope",
            )


class TestAttributionReportEndpoint:
    """v40 — the source→quote→revenue report. Owner-only; buckets leads
    by attribution source and ties paid-quote revenue back to it."""

    def _token(self, role="client_owner", org="just-print"):
        import time
        import jwt
        return jwt.encode(
            {
                "email": "t@example.com", "org_slug": org, "role": role,
                "iat": int(time.time()), "exp": int(time.time()) + 300,
                "iss": "strategos-dashboard", "sub": "t@example.com",
            },
            os.environ["STRATEGOS_JWT_SECRET"], algorithm="HS256",
        )

    def test_revenue_attributed_to_source(self):
        from fastapi.testclient import TestClient
        from app import app
        from db.models import Quote

        src_paid = f"google-{uuid.uuid4().hex[:6]}"
        src_dry = f"facebook-{uuid.uuid4().hex[:6]}"

        with db_session() as db:
            # Lead 1 — from src_paid, with a PAID quote (€100) + a pending one.
            c1 = Conversation(
                organization_slug=ORG, channel="web",
                external_id=f"web-{uuid.uuid4().hex[:6]}",
                attribution={"last_touch": {"utm_source": src_paid}},
            )
            db.add(c1)
            db.flush()
            db.add(Quote(
                organization_slug=ORG, conversation_id=c1.id,
                product_key="business_cards", total=100.0,
                final_price_inc_vat=100.0, status="approved",
                stripe_payment_status="paid",
            ))
            db.add(Quote(
                organization_slug=ORG, conversation_id=c1.id,
                product_key="flyers_a5", total=50.0,
                final_price_inc_vat=50.0, status="pending_approval",
            ))
            # Lead 2 — from src_dry, quote but NO paid.
            c2 = Conversation(
                organization_slug=ORG, channel="web",
                external_id=f"web-{uuid.uuid4().hex[:6]}",
                attribution={"last_touch": {"utm_source": src_dry}},
            )
            db.add(c2)
            db.flush()
            db.add(Quote(
                organization_slug=ORG, conversation_id=c2.id,
                product_key="business_cards", total=80.0,
                final_price_inc_vat=80.0, status="pending_approval",
            ))
            db.commit()

        client = TestClient(app)
        r = client.get(
            f"/admin/api/orgs/{ORG}/attribution-report",
            params={"group_by": "utm_source", "touch": "last"},
            headers={"Authorization": f"Bearer {self._token()}"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        rows = {row["source"]: row for row in body["rows"]}

        assert src_paid in rows, f"missing {src_paid}: {list(rows)}"
        assert rows[src_paid]["won"] == 1
        assert rows[src_paid]["revenue"] == 100.0
        # quotes_value counts ALL quotes on the lead (100 + 50).
        assert rows[src_paid]["quotes_value"] == 150.0

        assert src_dry in rows
        assert rows[src_dry]["won"] == 0
        assert rows[src_dry]["revenue"] == 0.0
        assert rows[src_dry]["quotes_value"] == 80.0

    def test_unattributed_bucket_collects_no_source_leads(self):
        from fastapi.testclient import TestClient
        from app import app

        with db_session() as db:
            c = Conversation(
                organization_slug=ORG, channel="web",
                external_id=f"web-noattr-{uuid.uuid4().hex[:6]}",
                attribution=None,
            )
            db.add(c)
            db.commit()

        client = TestClient(app)
        r = client.get(
            f"/admin/api/orgs/{ORG}/attribution-report",
            headers={"Authorization": f"Bearer {self._token()}"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["unattributed"]["leads"] >= 1

    def test_report_requires_owner_role(self):
        from fastapi.testclient import TestClient
        from app import app
        client = TestClient(app)
        r = client.get(
            f"/admin/api/orgs/{ORG}/attribution-report",
            headers={"Authorization": f"Bearer {self._token(role='client_viewer')}"},
        )
        assert r.status_code in (401, 403), (
            f"viewer should be denied, got {r.status_code}"
        )


class TestChatPersistsAttributionEndToEnd:
    """Integration — posting /chat with attribution must land it on the
    Conversation row (proves the app.py → chat_with_craig → merge wiring)."""

    def test_chat_persists_attribution(self):
        from unittest.mock import patch
        from fastapi.testclient import TestClient
        from app import app
        from test_chat_smoke import _make_mock_llm, _llm_reply
        from rate_limiter import _reset_for_tests as _rl_reset

        _rl_reset()
        client = TestClient(app)
        session = f"web-attr-{uuid.uuid4().hex[:8]}"

        with patch("llm.craig_agent.OpenAI", return_value=_make_mock_llm(
            _llm_reply("Hey — what are you printing?"),
        )):
            r = client.post("/chat", json={
                "message": "hi",
                "channel": "web",
                "organization_slug": ORG,
                "session_id": session,
                "attribution": {
                    "first_touch": {"utm_source": "google", "gclid": "abc123"},
                    "last_touch": {"utm_source": "google", "gclid": "abc123"},
                },
            })
        assert r.status_code == 200
        conv_id = r.json()["conversation_id"]

        with db_session() as db:
            conv = db.query(Conversation).filter_by(id=conv_id).first()
            assert conv is not None
            assert conv.attribution is not None, "attribution not persisted"
            assert conv.attribution["first_touch"]["gclid"] == "abc123"
