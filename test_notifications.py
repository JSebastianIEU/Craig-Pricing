"""
Tests for v33 — operator notifications (notifications.py).

We don't hit the real Resend API. Instead we mock `resend.Emails.send`
and assert the payload shape + idempotency behaviour. Three slices:

  1. send_quote_ready_for_approval — happy path, error path, settings
     short-circuits.
  2. trigger_approval_notification — idempotency on
     `quote.notification_sent_at`, error capture on the row.
  3. Body composition — subject + dashboard URL + customer fields all
     end up in the email.
"""

from __future__ import annotations

import os
from unittest.mock import patch, MagicMock

os.environ.setdefault("STRATEGOS_JWT_SECRET", "test-secret-32b-pad-enough-now")

import pytest
from db import db_session
from db.models import Conversation, Quote, Setting, DEFAULT_ORG_SLUG


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fresh_settings():
    """Wipe + seed the v33 settings before each test so we get a known
    config baseline. The v33 migration ran on the test DB during boot,
    but tests in other files might have mutated values."""
    with db_session() as db:
        for key, val in [
            ("notifications_enabled", "true"),
            ("notification_sender_address", "craig@notifications.strategos-ai.com"),
            ("notification_sender_name", "Craig (Just Print)"),
            ("notification_to_address", "info@just-print.ie"),
            ("dashboard_base_url", "https://strategos-dashboard.vercel.app"),
        ]:
            row = (
                db.query(Setting)
                .filter_by(organization_slug=DEFAULT_ORG_SLUG, key=key)
                .first()
            )
            if row:
                row.value = val
            else:
                db.add(Setting(
                    organization_slug=DEFAULT_ORG_SLUG,
                    key=key, value=val, value_type="string",
                ))
        db.commit()
    yield


def _new_conv_and_quote(channel: str = "missive") -> tuple[int, int]:
    """Returns (conv_id, quote_id) of a freshly-seeded pair. Inside its
    own session — caller can re-open another session to inspect."""
    with db_session() as db:
        conv = Conversation(
            organization_slug=DEFAULT_ORG_SLUG,
            external_id=f"test-notif-{id(object())}",
            channel=channel,
            customer_email="customer@example.com",
            customer_name="Test Customer",
            messages=[
                {"role": "user", "content": "Hi, can you do 100 cards?"},
                {"role": "assistant", "content": "Sure! Single or double sided?"},
                {"role": "user", "content": "single, matte"},
            ],
        )
        db.add(conv); db.flush()
        q = Quote(
            organization_slug=DEFAULT_ORG_SLUG,
            conversation_id=conv.id,
            product_key="business_cards",
            specs={"product_key": "business_cards", "quantity": 100,
                   "double_sided": False, "finish": "matte"},
            base_price=20.0, surcharges=[],
            final_price_ex_vat=20.0, vat_amount=2.7,
            final_price_inc_vat=22.7, artwork_cost=0.0,
            total=22.7, status="pending_approval",
        )
        db.add(q); db.commit()
        return conv.id, q.id


# ---------------------------------------------------------------------------
# send_quote_ready_for_approval — happy path
# ---------------------------------------------------------------------------


class TestSendQuoteReadyHappy:
    def test_sends_with_expected_payload(self, fresh_settings):
        from notifications import send_quote_ready_for_approval

        _, quote_id = _new_conv_and_quote()
        captured = {}
        def _fake_send(params):
            captured["params"] = params
            return {"id": "msg_abc123"}

        with patch.dict(os.environ, {"RESEND_API_KEY": "re_test_key"}):
            with patch("resend.Emails") as mock_emails:
                mock_emails.send = MagicMock(side_effect=_fake_send)
                with db_session() as db:
                    quote = db.query(Quote).filter_by(id=quote_id).first()
                    result = send_quote_ready_for_approval(db, quote, DEFAULT_ORG_SLUG)

        assert result["ok"] is True
        assert result["message_id"] == "msg_abc123"
        params = captured["params"]
        # Sender format must be "Name <addr>"
        assert "craig@notifications.strategos-ai.com" in params["from"]
        assert "Craig (Just Print)" in params["from"]
        # Recipient is the configured operator address
        assert params["to"] == ["info@just-print.ie"]
        # Subject contains the JP-XXXX ref + the price
        assert "JP-" in params["subject"]
        assert "22.70" in params["subject"]
        # HTML body contains the dashboard deep link and the customer
        assert "Test Customer" in params["html"]
        assert "customer@example.com" in params["html"]
        assert "focus_quote=" in params["html"]


class TestSendQuoteReadyShortCircuits:
    def test_disabled_via_settings(self, fresh_settings):
        from notifications import send_quote_ready_for_approval
        with db_session() as db:
            row = db.query(Setting).filter_by(
                organization_slug=DEFAULT_ORG_SLUG, key="notifications_enabled",
            ).first()
            row.value = "false"
            db.commit()
        _, quote_id = _new_conv_and_quote()
        with patch.dict(os.environ, {"RESEND_API_KEY": "re_test_key"}):
            with db_session() as db:
                quote = db.query(Quote).filter_by(id=quote_id).first()
                result = send_quote_ready_for_approval(db, quote, DEFAULT_ORG_SLUG)
        assert result["ok"] is False
        assert result["error"] == "notifications_disabled"

    def test_missing_api_key(self, fresh_settings):
        from notifications import send_quote_ready_for_approval
        _, quote_id = _new_conv_and_quote()
        # Force RESEND_API_KEY to empty (unset)
        with patch.dict(os.environ, {"RESEND_API_KEY": ""}):
            with db_session() as db:
                quote = db.query(Quote).filter_by(id=quote_id).first()
                result = send_quote_ready_for_approval(db, quote, DEFAULT_ORG_SLUG)
        assert result["ok"] is False
        assert "RESEND_API_KEY" in result["error"]

    def test_missing_to_address(self, fresh_settings):
        from notifications import send_quote_ready_for_approval
        with db_session() as db:
            row = db.query(Setting).filter_by(
                organization_slug=DEFAULT_ORG_SLUG, key="notification_to_address",
            ).first()
            row.value = ""
            db.commit()
        _, quote_id = _new_conv_and_quote()
        with patch.dict(os.environ, {"RESEND_API_KEY": "re_test_key"}):
            with db_session() as db:
                quote = db.query(Quote).filter_by(id=quote_id).first()
                result = send_quote_ready_for_approval(db, quote, DEFAULT_ORG_SLUG)
        assert result["ok"] is False
        assert result["error"] == "missing_notification_to_address"

    def test_resend_raises_returns_error_dict_not_exception(self, fresh_settings):
        from notifications import send_quote_ready_for_approval
        _, quote_id = _new_conv_and_quote()
        with patch.dict(os.environ, {"RESEND_API_KEY": "re_test_key"}):
            with patch("resend.Emails") as mock_emails:
                mock_emails.send = MagicMock(side_effect=RuntimeError("Resend 500"))
                with db_session() as db:
                    quote = db.query(Quote).filter_by(id=quote_id).first()
                    result = send_quote_ready_for_approval(db, quote, DEFAULT_ORG_SLUG)
        assert result["ok"] is False
        assert "Resend 500" in (result["error"] or "")


# ---------------------------------------------------------------------------
# trigger_approval_notification — idempotency + error persistence
# ---------------------------------------------------------------------------


class TestTriggerIdempotency:
    def test_first_call_sends_and_persists_message_id(self, fresh_settings):
        from notifications import trigger_approval_notification
        _, quote_id = _new_conv_and_quote()
        with patch.dict(os.environ, {"RESEND_API_KEY": "re_test_key"}):
            with patch("resend.Emails") as mock_emails:
                mock_emails.send = MagicMock(return_value={"id": "msg_001"})
                with db_session() as db:
                    r = trigger_approval_notification(db, DEFAULT_ORG_SLUG, quote_id)
        assert r["ok"] is True
        assert r["skipped"] is False
        with db_session() as db:
            q = db.query(Quote).filter_by(id=quote_id).first()
            assert q.notification_sent_at is not None
            assert q.notification_message_id == "msg_001"
            assert q.notification_last_error is None

    def test_second_call_skips(self, fresh_settings):
        """Customer says 'yes' twice — only one notification fires."""
        from notifications import trigger_approval_notification
        _, quote_id = _new_conv_and_quote()
        send_count = {"n": 0}
        def _send(params):
            send_count["n"] += 1
            return {"id": f"msg_{send_count['n']}"}
        with patch.dict(os.environ, {"RESEND_API_KEY": "re_test_key"}):
            with patch("resend.Emails") as mock_emails:
                mock_emails.send = MagicMock(side_effect=_send)
                with db_session() as db:
                    r1 = trigger_approval_notification(db, DEFAULT_ORG_SLUG, quote_id)
                with db_session() as db:
                    r2 = trigger_approval_notification(db, DEFAULT_ORG_SLUG, quote_id)
        assert r1["ok"] is True and r1["skipped"] is False
        assert r2["ok"] is True and r2["skipped"] is True
        assert send_count["n"] == 1

    def test_failed_send_persists_error(self, fresh_settings):
        from notifications import trigger_approval_notification
        _, quote_id = _new_conv_and_quote()
        with patch.dict(os.environ, {"RESEND_API_KEY": "re_test_key"}):
            with patch("resend.Emails") as mock_emails:
                mock_emails.send = MagicMock(side_effect=RuntimeError("Resend down"))
                with db_session() as db:
                    r = trigger_approval_notification(db, DEFAULT_ORG_SLUG, quote_id)
        assert r["ok"] is False
        with db_session() as db:
            q = db.query(Quote).filter_by(id=quote_id).first()
            # notification_sent_at NOT set (so a retry can still fire)
            assert q.notification_sent_at is None
            # error persisted for the dashboard to surface
            assert q.notification_last_error is not None
            assert "Resend down" in q.notification_last_error

    def test_failed_then_succeeded_on_retry(self, fresh_settings):
        """Resend down on first attempt, recovers on second. Customer
        flow continues either way; on the second retry, the email
        finally lands."""
        from notifications import trigger_approval_notification
        _, quote_id = _new_conv_and_quote()
        attempts = {"n": 0}
        def _flaky(params):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise RuntimeError("transient 500")
            return {"id": "msg_recovered"}
        with patch.dict(os.environ, {"RESEND_API_KEY": "re_test_key"}):
            with patch("resend.Emails") as mock_emails:
                mock_emails.send = MagicMock(side_effect=_flaky)
                with db_session() as db:
                    r1 = trigger_approval_notification(db, DEFAULT_ORG_SLUG, quote_id)
                with db_session() as db:
                    r2 = trigger_approval_notification(db, DEFAULT_ORG_SLUG, quote_id)
        assert r1["ok"] is False
        assert r2["ok"] is True
        with db_session() as db:
            q = db.query(Quote).filter_by(id=quote_id).first()
            assert q.notification_message_id == "msg_recovered"

    def test_quote_not_found(self, fresh_settings):
        from notifications import trigger_approval_notification
        with db_session() as db:
            r = trigger_approval_notification(db, DEFAULT_ORG_SLUG, 999999)
        assert r["ok"] is False
        assert r["error"] == "quote_not_found"


# ---------------------------------------------------------------------------
# Multi-recipient support — comma-separated `notification_to_address`
# ---------------------------------------------------------------------------
#
# The operator may store a single email (legacy) OR a comma-separated
# list (`"alice@x.com,bob@y.com"`) so a single notification fans out
# to multiple inboxes — used so Justin (the print shop owner) can be
# CC'd alongside Sebastian (the agency operator) without a settings
# schema change. The send path parses the value through
# `_parse_recipients` and passes the resulting list straight to
# Resend's `to` field.
# ---------------------------------------------------------------------------


class TestParseRecipients:
    """Pure-function tests of the comma-separated splitter."""

    def test_single_recipient(self):
        from notifications import _parse_recipients
        assert _parse_recipients("alice@example.com") == ["alice@example.com"]

    def test_two_recipients(self):
        from notifications import _parse_recipients
        assert _parse_recipients(
            "alice@example.com,bob@example.com"
        ) == ["alice@example.com", "bob@example.com"]

    def test_three_recipients_preserves_order(self):
        from notifications import _parse_recipients
        assert _parse_recipients(
            "a@x.com,b@x.com,c@x.com"
        ) == ["a@x.com", "b@x.com", "c@x.com"]

    def test_whitespace_around_addresses_stripped(self):
        from notifications import _parse_recipients
        assert _parse_recipients(
            "  alice@x.com  ,  bob@y.com  "
        ) == ["alice@x.com", "bob@y.com"]

    def test_empty_entries_dropped(self):
        # Operator might leave a trailing comma or double comma by accident.
        from notifications import _parse_recipients
        assert _parse_recipients(
            "alice@x.com,,bob@y.com,"
        ) == ["alice@x.com", "bob@y.com"]

    def test_none_returns_empty_list(self):
        from notifications import _parse_recipients
        assert _parse_recipients(None) == []

    def test_empty_string_returns_empty_list(self):
        from notifications import _parse_recipients
        assert _parse_recipients("") == []

    def test_whitespace_only_returns_empty_list(self):
        # Treat `"   "` and `"  ,  "` the same as "no recipient configured"
        # so the caller's `if not to_addr` empty-check still fires.
        from notifications import _parse_recipients
        assert _parse_recipients("   ") == []
        assert _parse_recipients("  ,  ,  ") == []


class TestSendQuoteReadyMultiRecipient:
    """Integration: comma-separated setting flows through to Resend `to`."""

    def test_two_recipients_csv_setting_sent_as_list(self, fresh_settings):
        """Operator stores `notification_to_address` as `a@x.com,b@y.com`
        → Resend receives BOTH addresses in its `to` list."""
        from notifications import send_quote_ready_for_approval

        with db_session() as db:
            row = db.query(Setting).filter_by(
                organization_slug=DEFAULT_ORG_SLUG,
                key="notification_to_address",
            ).first()
            row.value = "sebastian@strategos-ai.com,justprintie@gmail.com"
            db.commit()

        _, quote_id = _new_conv_and_quote()
        captured: dict = {}

        def _fake_send(params):
            captured["params"] = params
            return {"id": "msg_multi"}

        with patch.dict(os.environ, {"RESEND_API_KEY": "re_test_key"}):
            with patch("resend.Emails") as mock_emails:
                mock_emails.send = MagicMock(side_effect=_fake_send)
                with db_session() as db:
                    quote = db.query(Quote).filter_by(id=quote_id).first()
                    result = send_quote_ready_for_approval(
                        db, quote, DEFAULT_ORG_SLUG,
                    )

        assert result["ok"] is True
        assert captured["params"]["to"] == [
            "sebastian@strategos-ai.com",
            "justprintie@gmail.com",
        ]

    def test_whitespace_around_csv_addresses_normalised(self, fresh_settings):
        """Whitespace around commas is tolerated — operator may save
        the value with spaces for readability."""
        from notifications import send_quote_ready_for_approval

        with db_session() as db:
            row = db.query(Setting).filter_by(
                organization_slug=DEFAULT_ORG_SLUG,
                key="notification_to_address",
            ).first()
            row.value = "  alice@x.com  ,  bob@y.com  "
            db.commit()

        _, quote_id = _new_conv_and_quote()
        captured: dict = {}

        def _fake_send(params):
            captured["params"] = params
            return {"id": "msg_ws"}

        with patch.dict(os.environ, {"RESEND_API_KEY": "re_test_key"}):
            with patch("resend.Emails") as mock_emails:
                mock_emails.send = MagicMock(side_effect=_fake_send)
                with db_session() as db:
                    quote = db.query(Quote).filter_by(id=quote_id).first()
                    result = send_quote_ready_for_approval(
                        db, quote, DEFAULT_ORG_SLUG,
                    )

        assert result["ok"] is True
        assert captured["params"]["to"] == ["alice@x.com", "bob@y.com"]

    def test_whitespace_only_setting_behaves_as_missing(self, fresh_settings):
        """`"   ,  ,   "` should fail with the same error as `""` so
        operators don't get silent drops on a typo."""
        from notifications import send_quote_ready_for_approval

        with db_session() as db:
            row = db.query(Setting).filter_by(
                organization_slug=DEFAULT_ORG_SLUG,
                key="notification_to_address",
            ).first()
            row.value = "   ,  ,   "
            db.commit()

        _, quote_id = _new_conv_and_quote()
        with patch.dict(os.environ, {"RESEND_API_KEY": "re_test_key"}):
            with db_session() as db:
                quote = db.query(Quote).filter_by(id=quote_id).first()
                result = send_quote_ready_for_approval(
                    db, quote, DEFAULT_ORG_SLUG,
                )

        assert result["ok"] is False
        assert result["error"] == "missing_notification_to_address"

    def test_single_recipient_setting_still_works(self, fresh_settings):
        """Existing tenants with a single-address setting see no
        behaviour change — Resend still gets a 1-element list."""
        from notifications import send_quote_ready_for_approval

        # fresh_settings seeds "info@just-print.ie" — no change needed
        _, quote_id = _new_conv_and_quote()
        captured: dict = {}

        def _fake_send(params):
            captured["params"] = params
            return {"id": "msg_single"}

        with patch.dict(os.environ, {"RESEND_API_KEY": "re_test_key"}):
            with patch("resend.Emails") as mock_emails:
                mock_emails.send = MagicMock(side_effect=_fake_send)
                with db_session() as db:
                    quote = db.query(Quote).filter_by(id=quote_id).first()
                    result = send_quote_ready_for_approval(
                        db, quote, DEFAULT_ORG_SLUG,
                    )

        assert result["ok"] is True
        assert captured["params"]["to"] == ["info@just-print.ie"]
