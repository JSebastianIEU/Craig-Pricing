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
