"""Unit tests for the Missive thin client.

These do NOT talk to the real Missive API — they just exercise the HMAC
signature verification + webhook payload extraction, which is where the
security-critical logic lives.
"""

import hashlib
import hmac as _hmac

import missive


def _sign(body: bytes, secret: str) -> str:
    return _hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_verify_webhook_happy_path():
    body = b'{"rule":{"id":"r1"},"conversation":{"id":"c1"}}'
    secret = "super-secret"
    sig = _sign(body, secret)
    assert missive.verify_webhook(body, sig, secret) is True


def test_verify_webhook_accepts_sha256_prefix():
    body = b'{"x":"y"}'
    secret = "abc"
    sig = "sha256=" + _sign(body, secret)
    assert missive.verify_webhook(body, sig, secret) is True


def test_verify_webhook_rejects_wrong_secret():
    body = b'{"x":"y"}'
    sig = _sign(body, "real-secret")
    assert missive.verify_webhook(body, sig, "different-secret") is False


def test_verify_webhook_rejects_tampered_body():
    secret = "abc"
    sig = _sign(b'{"x":"y"}', secret)
    assert missive.verify_webhook(b'{"x":"z"}', sig, secret) is False


def test_verify_webhook_rejects_empty_inputs():
    assert missive.verify_webhook(b"any", "", "secret") is False
    assert missive.verify_webhook(b"any", "deadbeef", "") is False
    assert missive.verify_webhook(b"", "", "") is False


def test_extract_inbound_email_ok():
    payload = {
        "rule": {"id": "r", "type": "webhook"},
        "conversation": {"id": "conv-1", "subject": "Price on 500 biz cards"},
        "latest_message": {
            "id": "msg-1",
            "type": "email",
            "from_field": {"address": "jane@acme.co", "name": "Jane"},
            "preview": "Hi Craig, how much for 500?",
        },
    }
    evt = missive.extract_inbound_email(payload)
    assert evt is not None
    assert evt["conversation_id"] == "conv-1"
    assert evt["message_id"] == "msg-1"
    assert evt["from_address"] == "jane@acme.co"
    assert evt["from_name"] == "Jane"
    assert evt["subject"] == "Price on 500 biz cards"


def test_extract_inbound_email_skips_non_email():
    # e.g. a label-changed event
    payload = {
        "conversation": {"id": "c1"},
        "latest_message": {"id": "m1", "type": "sms"},
    }
    assert missive.extract_inbound_email(payload) is None


def test_extract_inbound_email_missing_ids_returns_none():
    assert missive.extract_inbound_email({}) is None
    assert missive.extract_inbound_email({"conversation": {}}) is None


# ---------------------------------------------------------------------------
# v32 — auto-send vs draft (the new `send` parameter on create_draft)
# ---------------------------------------------------------------------------


def test_create_draft_default_keeps_draft_state():
    """Default behaviour (no `send` arg) must still produce send=false
    in the payload. Locks in backwards compatibility."""
    import asyncio
    import json
    import httpx
    import respx

    with respx.mock() as mock:
        route = mock.post("https://public.missiveapp.com/v1/drafts").mock(
            return_value=httpx.Response(200, json={"drafts": {"id": "draft-1"}})
        )
        asyncio.run(missive.create_draft(
            conversation_id="conv-1",
            html_body="<p>hi</p>",
            from_address="info@just-print.ie",
            from_name="Justin",
            to_fields=[{"address": "c@example.com", "name": "C"}],
            token="fake-token",
        ))
        body = json.loads(route.calls[0].request.read().decode())
    assert body["drafts"]["send"] is False


def test_create_draft_send_true_marks_payload_send_true():
    """v32 — passing `send=True` must produce send=true in the payload
    so Missive sends the message immediately on draft creation."""
    import asyncio
    import json
    import httpx
    import respx

    with respx.mock() as mock:
        route = mock.post("https://public.missiveapp.com/v1/drafts").mock(
            return_value=httpx.Response(200, json={"drafts": {"id": "draft-2"}})
        )
        asyncio.run(missive.create_draft(
            conversation_id="conv-2",
            html_body="<p>STEP 1: please clarify the finish</p>",
            from_address="info@just-print.ie",
            from_name="Justin",
            to_fields=[{"address": "c@example.com", "name": "C"}],
            token="fake-token",
            send=True,
        ))
        body = json.loads(route.calls[0].request.read().decode())
    assert body["drafts"]["send"] is True
    # And nothing else should change about the payload shape.
    assert body["drafts"]["body"] == "<p>STEP 1: please clarify the finish</p>"
    assert body["drafts"]["conversation"] == "conv-2"


def test_create_draft_send_false_explicit_marks_payload_send_false():
    """Explicit send=False must round-trip the same as the default."""
    import asyncio
    import json
    import httpx
    import respx

    with respx.mock() as mock:
        route = mock.post("https://public.missiveapp.com/v1/drafts").mock(
            return_value=httpx.Response(200, json={"drafts": {"id": "draft-3"}})
        )
        asyncio.run(missive.create_draft(
            conversation_id="conv-3",
            html_body="<p>STEP 4: binding quote with PDF</p>",
            from_address="info@just-print.ie",
            from_name="Justin",
            to_fields=[{"address": "c@example.com", "name": "C"}],
            token="fake-token",
            send=False,
        ))
        body = json.loads(route.calls[0].request.read().decode())
    assert body["drafts"]["send"] is False


def test_create_new_thread_draft_default_send_false():
    """Outbound-from-web flow keeps the pre-v32 default: always drafts."""
    import asyncio
    import json
    import httpx
    import respx

    with respx.mock() as mock:
        route = mock.post("https://public.missiveapp.com/v1/drafts").mock(
            return_value=httpx.Response(200, json={"drafts": {"id": "draft-4"}})
        )
        asyncio.run(missive.create_new_thread_draft(
            html_body="<p>quote</p>",
            from_address="info@just-print.ie",
            from_name="Justin",
            to_fields=[{"address": "c@example.com", "name": "C"}],
            token="fake-token",
            subject="Your quote from Just Print",
        ))
        body = json.loads(route.calls[0].request.read().decode())
    assert body["drafts"]["send"] is False


# ---------------------------------------------------------------------------
# v37.2 — self-sent / notification-loop prevention
# ---------------------------------------------------------------------------


class TestIsSelfSentEmail:
    """v37.2 — `_is_self_sent_email` is the gate that drops emails the
    webhook would otherwise treat as customer mail and reply to. It
    prevents Craig from auto-responding to its own quote-approval and
    manual-review notification emails when those land in a Missive-
    watched inbox.
    """

    def test_real_customer_passes(self):
        from app import _is_self_sent_email
        assert _is_self_sent_email(
            from_address="bob@example.com",
            subject="500 business cards quote please",
            missive_from_address="info@just-print.ie",
            notification_sender_address="craig@strategos-ai.com",
        ) is None

    def test_blocks_missive_from_address(self):
        from app import _is_self_sent_email
        r = _is_self_sent_email(
            from_address="info@just-print.ie",
            subject="Re: anything",
            missive_from_address="info@just-print.ie",
            notification_sender_address="craig@strategos-ai.com",
        )
        assert r is not None and "address-match" in r

    def test_blocks_notification_sender(self):
        """The bug from production — Craig's own notification email
        landed in Missive and Craig replied to it."""
        from app import _is_self_sent_email
        r = _is_self_sent_email(
            from_address="craig@strategos-ai.com",
            subject="[Just Print — needs your eyes] Quote JP-0097 — manual pricing required",
            missive_from_address="info@just-print.ie",
            notification_sender_address="craig@strategos-ai.com",
        )
        assert r is not None and "address-match" in r

    def test_blocks_just_print_subject_prefix_even_without_addr_match(self):
        """If the operator changed notification_sender_address but a
        forward rule still drops the email into Missive, the subject
        prefix is the last-ditch giveaway."""
        from app import _is_self_sent_email
        r = _is_self_sent_email(
            from_address="some-new-mailer@third-party.com",
            subject="[Just Print] Quote JP-0123 ready for approval",
            missive_from_address="info@just-print.ie",
            notification_sender_address="",  # not set
        )
        assert r is not None and "subject-prefix" in r

    def test_case_insensitive_address(self):
        from app import _is_self_sent_email
        r = _is_self_sent_email(
            from_address="Craig@Strategos-AI.com",
            subject="hi",
            missive_from_address="info@just-print.ie",
            notification_sender_address="craig@strategos-ai.com",
        )
        assert r is not None

    def test_empty_settings_still_pass_real_customer(self):
        """When no settings are configured (fresh tenant / dev env),
        only the subject sniff fires — real customer mail still passes."""
        from app import _is_self_sent_email
        assert _is_self_sent_email(
            from_address="alice@gmail.com",
            subject="hello, do you do banners?",
            missive_from_address="",
            notification_sender_address="",
        ) is None

    def test_empty_subject_with_self_address_still_blocks(self):
        from app import _is_self_sent_email
        r = _is_self_sent_email(
            from_address="info@just-print.ie",
            subject="",
            missive_from_address="info@just-print.ie",
            notification_sender_address="",
        )
        assert r is not None

    def test_just_print_lowercase_subject_blocks(self):
        from app import _is_self_sent_email
        r = _is_self_sent_email(
            from_address="x@y.com",
            subject="[just print] something",
            missive_from_address="",
            notification_sender_address="",
        )
        assert r is not None

    def test_normal_subject_with_just_print_in_middle_passes(self):
        """Make sure we don't false-positive on legitimate emails that
        happen to mention 'Just Print' inside the subject."""
        from app import _is_self_sent_email
        assert _is_self_sent_email(
            from_address="bob@example.com",
            subject="Question about Just Print services",
            missive_from_address="",
            notification_sender_address="",
        ) is None

    # ── v37.7 — internal team allowlist ────────────────────────────

    def test_blocks_internal_team_domain(self):
        """Eva sends an internal-team email from eva@just-print.ie to a
        customer thread in the Missive-watched inbox. Craig must see
        the internal domain and drop the message — not classify it,
        not Tier 2 it. This is the catastrophic bug v37.7 prevents."""
        from app import _is_self_sent_email
        r = _is_self_sent_email(
            from_address="eva@just-print.ie",
            subject="quick question about a job",
            missive_from_address="info@just-print.ie",
            notification_sender_address="craig@strategos-ai.com",
            internal_team_domains=["just-print.ie"],
        )
        assert r is not None
        assert "internal-team-domain" in r
        assert "just-print.ie" in r

    def test_blocks_internal_team_domain_case_insensitive(self):
        from app import _is_self_sent_email
        r = _is_self_sent_email(
            from_address="EVA@JUST-PRINT.IE",
            subject="hi",
            missive_from_address="",
            notification_sender_address="",
            internal_team_domains=["just-print.ie"],
        )
        assert r is not None

    def test_blocks_internal_team_address_for_personal_email(self):
        """Team member uses a personal Gmail. Operator added them to
        internal_team_addresses."""
        from app import _is_self_sent_email
        r = _is_self_sent_email(
            from_address="eva.personal@gmail.com",
            subject="anything",
            missive_from_address="",
            notification_sender_address="",
            internal_team_addresses=["eva.personal@gmail.com"],
        )
        assert r is not None
        assert "internal-team-address" in r

    def test_empty_allowlists_still_pass_real_customers(self):
        """Default state — no allowlists configured — must not break
        real customer mail."""
        from app import _is_self_sent_email
        assert _is_self_sent_email(
            from_address="alice@gmail.com",
            subject="quote please",
            internal_team_domains=[],
            internal_team_addresses=[],
        ) is None
        assert _is_self_sent_email(
            from_address="alice@gmail.com",
            subject="quote please",
            internal_team_domains=None,
            internal_team_addresses=None,
        ) is None

    def test_customer_at_unrelated_domain_passes_through_team_check(self):
        """A real customer at unrelated.com must still pass even when
        a team-domain allowlist is configured."""
        from app import _is_self_sent_email
        assert _is_self_sent_email(
            from_address="alice@unrelated.com",
            subject="quote please",
            internal_team_domains=["just-print.ie"],
            internal_team_addresses=["eva.personal@gmail.com"],
        ) is None

    def test_team_domain_takes_priority_over_subject_sniff(self):
        """If a team member emails with a '[Just Print' subject, the
        domain check still fires first (and gives a more specific
        reason in the log)."""
        from app import _is_self_sent_email
        r = _is_self_sent_email(
            from_address="alfred@just-print.ie",
            subject="[Just Print — needs your eyes] from Alfred",
            internal_team_domains=["just-print.ie"],
        )
        assert r is not None
        # Domain check runs before subject sniff in the helper
        assert "internal-team-domain" in r


# ---------------------------------------------------------------------------
# v37.7 — webhook honours missive_enabled=false (kill switch contract)
# ---------------------------------------------------------------------------


class TestWebhookHonoursDisabledFlag:
    """Critical kill-switch regression test. When `missive_enabled` is
    `false` in Settings, the webhook handler MUST return early without
    calling the LLM (no token burn) and without posting any drafts
    (no customer noise) and without triggering Justin notifications
    (no operator noise). This is the contract Justin relies on when
    he flips the toggle OFF mid-day for emergencies."""

    def _payload(self):
        return {
            "rule": {"id": "r1", "type": "webhook"},
            "conversation": {"id": "conv-killswitch", "subject": "test"},
            "message": {
                "id": "msg-killswitch",
                "type": "email",
                "from_field": {"address": "bob@example.com", "name": "Bob"},
                "to_fields": [{"address": "info@just-print.ie"}],
                "preview": "Hi, 500 business cards please",
                "body": "Hi, 500 business cards please",
                "subject": "Quote please",
                "headers": {},
            },
        }

    def test_disabled_flag_short_circuits_no_llm_no_draft(self, monkeypatch):
        """The kill switch core contract: handler enters, sees
        missive_enabled=false, returns immediately. No LLM call. No
        Missive draft post. No Justin Resend email."""
        from app import _handle_missive_event
        from unittest.mock import MagicMock, patch as _patch

        # Capture any LLM / Missive / Resend call attempts.
        chat_called = MagicMock()
        create_draft_called = MagicMock()
        trigger_notif_called = MagicMock()

        # Stub _get_setting so we don't depend on the test DB's state.
        # Returns missive_enabled=false; everything else returns a
        # plausible value so the handler doesn't fail for unrelated
        # reasons before reaching the enabled check.
        def fake_get_setting(db, key, default="", organization_slug=None):
            if key == "missive_enabled":
                return "false"
            if key == "missive_api_token":
                return "fake-token-would-work-if-enabled"
            return default

        with _patch("pricing_engine._get_setting", side_effect=fake_get_setting), \
             _patch("llm.craig_agent.chat_with_craig", chat_called), \
             _patch("missive.create_draft", create_draft_called), \
             _patch(
                 "notifications.trigger_engagement_approval_notification",
                 trigger_notif_called,
             ):
            _handle_missive_event("just-print", self._payload())

        # The CORE assertions: kill switch held the line.
        assert chat_called.call_count == 0, (
            "chat_with_craig was called despite missive_enabled=false — kill switch leak"
        )
        assert create_draft_called.call_count == 0, (
            "missive.create_draft was called despite missive_enabled=false — kill switch leak"
        )
        assert trigger_notif_called.call_count == 0, (
            "engagement-approval notification fired despite missive_enabled=false"
        )

    def test_missing_token_also_short_circuits(self, monkeypatch):
        """Belt + braces: even with missive_enabled=true, an empty
        missive_api_token must short-circuit too. (Defensive — the
        Switch in the dashboard requires a token before letting the
        operator turn ON, but config drift / DB corruption could
        produce this state and we don't want Craig leaking through.)"""
        from app import _handle_missive_event
        from unittest.mock import MagicMock, patch as _patch

        chat_called = MagicMock()
        create_draft_called = MagicMock()

        def fake_get_setting(db, key, default="", organization_slug=None):
            if key == "missive_enabled":
                return "true"
            if key == "missive_api_token":
                return ""  # ← missing token
            return default

        with _patch("pricing_engine._get_setting", side_effect=fake_get_setting), \
             _patch("llm.craig_agent.chat_with_craig", chat_called), \
             _patch("missive.create_draft", create_draft_called):
            _handle_missive_event("just-print", self._payload())

        assert chat_called.call_count == 0
        assert create_draft_called.call_count == 0
