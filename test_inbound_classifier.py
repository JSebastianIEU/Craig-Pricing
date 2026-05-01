"""
Tests for the Missive inbound spam / non-quote filter.

Two pieces:
  1. `obvious_junk()` — cheap structural prefilter (no LLM call).
     Pure function tests.
  2. `classify_inbound_email()` — DeepSeek-backed classifier. Mocked
     so we don't hit the real API. Verifies fail-open posture and
     thread-reply bypass.
"""

from __future__ import annotations

import json
import os
from unittest.mock import patch, MagicMock

import pytest

os.environ.setdefault("STRATEGOS_JWT_SECRET", "test-secret-32b-pad-enough-now")

from llm.inbound_classifier import (  # noqa: E402
    classify_inbound_email,
    obvious_junk,
)


# ---------------------------------------------------------------------------
# obvious_junk — hard-reject prefilter
# ---------------------------------------------------------------------------


class TestObviousJunkSenders:
    def test_no_reply_dash_sender_rejected(self):
        r = obvious_junk(from_address="no-reply@example.com", subject="hi")
        assert r is not None and "no-reply sender" in r

    def test_noreply_no_dash_sender_rejected(self):
        r = obvious_junk(from_address="noreply@example.com", subject="hi")
        assert r is not None

    def test_mailer_daemon_rejected(self):
        r = obvious_junk(
            from_address="MAILER-DAEMON@gmail.com",
            subject="Delivery Status Notification",
        )
        # Either the sender match or the subject match is enough
        assert r is not None

    def test_postmaster_rejected(self):
        r = obvious_junk(from_address="postmaster@example.com", subject="hi")
        assert r is not None

    def test_bounces_sender_rejected(self):
        r = obvious_junk(from_address="bounces+abc@mailgun.org", subject="hi")
        assert r is not None

    def test_real_customer_passes(self):
        r = obvious_junk(
            from_address="bryan@example.com",
            subject="Quote for 500 business cards",
        )
        assert r is None


class TestObviousJunkSubjects:
    def test_out_of_office_rejected(self):
        r = obvious_junk(
            from_address="real@customer.com",
            subject="Out of Office: Re: your quote",
        )
        assert r is not None and "out of office" in r

    def test_auto_reply_rejected(self):
        r = obvious_junk(
            from_address="real@customer.com",
            subject="Automatic reply: thanks for your message",
        )
        assert r is not None

    def test_undeliverable_rejected(self):
        r = obvious_junk(
            from_address="real@customer.com",
            subject="Undeliverable: Re: business cards",
        )
        assert r is not None

    def test_unsubscribe_rejected(self):
        r = obvious_junk(
            from_address="newsletter@somecompany.com",
            subject="Unsubscribe from this list",
        )
        assert r is not None

    def test_quote_subject_passes(self):
        r = obvious_junk(
            from_address="real@customer.com",
            subject="Looking for a quote on 500 flyers",
        )
        assert r is None


class TestObviousJunkHeaders:
    def test_list_unsubscribe_rejected(self):
        r = obvious_junk(
            from_address="news@somecorp.com",
            subject="May newsletter",
            headers={"List-Unsubscribe": "<mailto:u@somecorp.com>"},
        )
        assert r is not None and "mailing list" in r

    def test_x_auto_response_rejected(self):
        r = obvious_junk(
            from_address="bot@somesys.com",
            subject="Re: ticket #123",
            headers={"X-Auto-Response-Suppress": "All"},
        )
        assert r is not None and "auto-response" in r

    def test_auto_submitted_rejected(self):
        r = obvious_junk(
            from_address="bot@somesys.com",
            subject="Notification",
            headers={"Auto-Submitted": "auto-replied"},
        )
        assert r is not None

    def test_auto_submitted_no_passes(self):
        # "Auto-Submitted: no" means it's NOT auto-submitted
        r = obvious_junk(
            from_address="real@customer.com",
            subject="Quote please",
            headers={"Auto-Submitted": "no"},
        )
        assert r is None

    def test_no_headers_doesnt_crash(self):
        r = obvious_junk(
            from_address="real@customer.com",
            subject="Quote please",
            headers=None,
        )
        assert r is None


# ---------------------------------------------------------------------------
# classify_inbound_email — DeepSeek-backed classifier
# ---------------------------------------------------------------------------


class TestClassifyThreadReplyBypass:
    """If the email is a reply in a thread Craig already drafted in,
    we skip the LLM call (real customers replying to Craig are by
    definition real customers)."""

    def test_thread_reply_bypasses_llm(self):
        # No mock — if the LLM were called, it would hit the network
        # (no API key in tests) and return fail-open. But we want to
        # assert no call happened. We patch OpenAI to raise if called.
        with patch("llm.inbound_classifier.OpenAI") as mock_client_cls:
            mock_client_cls.side_effect = AssertionError("LLM should not be called")
            verdict = classify_inbound_email(
                from_address="customer@example.com",
                subject="Re: your quote",
                body_preview="Cheers, see you soon",
                is_thread_reply=True,
            )
        assert verdict["is_quote_inquiry"] is True
        assert "thread reply" in verdict["reason"].lower()


class TestClassifyFailOpen:
    """Errors at any point default to is_quote_inquiry=True so we
    don't silently drop real customers."""

    def test_missing_api_key_passes(self):
        with patch("llm.inbound_classifier.DEEPSEEK_API_KEY", ""):
            verdict = classify_inbound_email(
                from_address="customer@example.com",
                subject="Quote please",
                body_preview="500 business cards",
                is_thread_reply=False,
            )
        assert verdict["is_quote_inquiry"] is True
        assert "no DEEPSEEK_API_KEY" in verdict["reason"]

    def test_llm_timeout_passes(self):
        with patch("llm.inbound_classifier.DEEPSEEK_API_KEY", "fake-key"):
            with patch("llm.inbound_classifier.OpenAI") as mock_client_cls:
                mock_client = MagicMock()
                mock_client.chat.completions.create.side_effect = TimeoutError("slow")
                mock_client_cls.return_value = mock_client
                verdict = classify_inbound_email(
                    from_address="customer@example.com",
                    subject="Quote please",
                    body_preview="500 business cards",
                    is_thread_reply=False,
                )
        assert verdict["is_quote_inquiry"] is True
        assert "classifier-error" in verdict["reason"].lower()

    def test_llm_malformed_json_passes(self):
        with patch("llm.inbound_classifier.DEEPSEEK_API_KEY", "fake-key"):
            with patch("llm.inbound_classifier.OpenAI") as mock_client_cls:
                mock_client = MagicMock()
                mock_resp = MagicMock()
                mock_resp.choices = [MagicMock()]
                mock_resp.choices[0].message.content = "not json at all"
                mock_client.chat.completions.create.return_value = mock_resp
                mock_client_cls.return_value = mock_client
                verdict = classify_inbound_email(
                    from_address="customer@example.com",
                    subject="Quote please",
                    body_preview="500 business cards",
                    is_thread_reply=False,
                )
        assert verdict["is_quote_inquiry"] is True


class TestClassifyHappyPath:
    """LLM returns valid JSON — verdict + reason flow through."""

    def _mock_with_response(self, payload: dict):
        mock_client = MagicMock()
        mock_resp = MagicMock()
        mock_resp.choices = [MagicMock()]
        mock_resp.choices[0].message.content = json.dumps(payload)
        mock_client.chat.completions.create.return_value = mock_resp
        return mock_client

    def test_quote_inquiry_passes(self):
        with patch("llm.inbound_classifier.DEEPSEEK_API_KEY", "fake-key"):
            mock_client = self._mock_with_response({
                "is_quote_inquiry": True,
                "reason": "asking about business cards",
            })
            with patch("llm.inbound_classifier.OpenAI", return_value=mock_client):
                verdict = classify_inbound_email(
                    from_address="customer@example.com",
                    subject="500 business cards quote?",
                    body_preview="Hi, I need 500 cards, soft-touch.",
                    is_thread_reply=False,
                )
        assert verdict["is_quote_inquiry"] is True
        assert "business cards" in verdict["reason"]

    def test_promotional_email_rejected(self):
        with patch("llm.inbound_classifier.DEEPSEEK_API_KEY", "fake-key"):
            mock_client = self._mock_with_response({
                "is_quote_inquiry": False,
                "reason": "cold sales pitch for SEO services",
            })
            with patch("llm.inbound_classifier.OpenAI", return_value=mock_client):
                verdict = classify_inbound_email(
                    from_address="sales@seocompany.com",
                    subject="Boost your Google rankings!",
                    body_preview="Hi, I noticed your website needs SEO help...",
                    is_thread_reply=False,
                )
        assert verdict["is_quote_inquiry"] is False
        assert "seo" in verdict["reason"].lower()

    def test_long_body_truncated_to_800(self):
        """Helps keep token costs down. Sanity check that we cap."""
        with patch("llm.inbound_classifier.DEEPSEEK_API_KEY", "fake-key"):
            mock_client = self._mock_with_response({
                "is_quote_inquiry": True,
                "reason": "ok",
            })
            with patch("llm.inbound_classifier.OpenAI", return_value=mock_client):
                long_body = "x" * 5000
                classify_inbound_email(
                    from_address="customer@example.com",
                    subject="Q",
                    body_preview=long_body,
                    is_thread_reply=False,
                )
                # Inspect what was sent
                call = mock_client.chat.completions.create.call_args
                user_msg = call.kwargs["messages"][1]["content"]
                # Body in prompt is repr()'d — count x's between the
                # body marker and the closing quote.
                assert user_msg.count("x") <= 810  # 800 cap + small slop
