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
