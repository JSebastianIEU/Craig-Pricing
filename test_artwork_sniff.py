"""
Tests for `_sniff_artwork_answer` — the regex-based detector that maps a
customer's reply to "do you have artwork or need design?" into the
boolean we stamp on Conversation.customer_has_own_artwork.

History: this sniffer over-fired in production (conv 97, May 2026) —
the user's first message "hey i need 100 business cards double sided
soft" matched the pattern "i need" in _ARTWORK_NEED_DESIGN, which
stamped customer_has_own_artwork=False *before* Craig had ever asked
the artwork question. That bypassed the pricing-tool guard and Craig
priced without asking. Patterns tightened to require unambiguous
artwork/design references; this file locks the contract in.
"""

from __future__ import annotations

import os

os.environ.setdefault("STRATEGOS_JWT_SECRET", "test-secret-32b-pad-enough-now")

from llm.craig_agent import _sniff_artwork_answer  # noqa: E402


# ---------------------------------------------------------------------------
# False positives — these MUST NOT trip the sniffer
# ---------------------------------------------------------------------------


class TestSniffFalsePositives:
    """Generic product-shopping language that earlier versions misread
    as an artwork answer."""

    def test_first_message_with_i_need_quantity(self):
        # The exact regression from conv 97
        assert _sniff_artwork_answer(None, "hey i need 100 business cards double sided soft") is None

    def test_i_need_a_quote(self):
        assert _sniff_artwork_answer(None, "i need a quote for some flyers") is None

    def test_i_need_help_with_pricing(self):
        # "need help" is too generic — only "need help with the design" should fire
        assert _sniff_artwork_answer(None, "i need help with pricing") is None

    def test_i_have_one_question(self):
        assert _sniff_artwork_answer(None, "i have one question") is None

    def test_i_have_a_deadline(self):
        # Earlier "i have" pattern would trip on this
        assert _sniff_artwork_answer(None, "i have a deadline next week") is None

    def test_i_have_50_cards_to_print(self):
        assert _sniff_artwork_answer(None, "i have 50 cards to print") is None

    def test_got_it(self):
        # Acknowledgment, not artwork answer
        assert _sniff_artwork_answer(None, "got it, thanks") is None

    def test_my_own_company(self):
        assert _sniff_artwork_answer(None, "for my own company") is None

    def test_ready_to_order(self):
        # "ready" alone shouldn't fire — only "ready to print" / "print-ready"
        assert _sniff_artwork_answer(None, "I'm ready to order") is None

    def test_i_dont_have_a_phone_number(self):
        assert _sniff_artwork_answer(None, "i don't have a phone number") is None

    def test_make_one_for_each_person(self):
        # earlier pattern "make one" / "create one" was too loose
        assert _sniff_artwork_answer(None, "make one for each person on the team") is None

    def test_can_you_make_it_smaller(self):
        # "can you make" alone shouldn't trip
        assert _sniff_artwork_answer(None, "can you make it smaller, like A6?") is None


# ---------------------------------------------------------------------------
# True positives — direct artwork affirmatives MUST fire
# ---------------------------------------------------------------------------


class TestSniffArtworkAffirmative:
    def test_i_have_artwork(self):
        assert _sniff_artwork_answer(None, "I have artwork ready") is True

    def test_i_have_my_own_artwork(self):
        assert _sniff_artwork_answer(None, "i have my own artwork") is True

    def test_print_ready(self):
        assert _sniff_artwork_answer(None, "yes everything is print-ready") is True

    def test_ill_send_the_files(self):
        assert _sniff_artwork_answer(None, "i'll send the files later today") is True

    def test_i_have_the_design(self):
        assert _sniff_artwork_answer(None, "I have the design already") is True


# ---------------------------------------------------------------------------
# True positives — design-service requests MUST fire
# ---------------------------------------------------------------------------


class TestSniffNeedsDesign:
    def test_need_design_help(self):
        assert _sniff_artwork_answer(None, "I need design help, can you do that?") is False

    def test_design_service(self):
        assert _sniff_artwork_answer(
            "Do you have artwork or want our design service?",
            "design service please",
        ) is False

    def test_no_artwork(self):
        assert _sniff_artwork_answer(None, "no artwork — can you make it?") is False

    def test_can_you_design(self):
        assert _sniff_artwork_answer(None, "can you design the cards for me?") is False

    def test_dont_have_artwork(self):
        assert _sniff_artwork_answer(None, "i don't have artwork yet") is False


# ---------------------------------------------------------------------------
# Strategy 2 — bare yes/no only when Craig actually asked artwork
# ---------------------------------------------------------------------------


class TestSniffBareYesNoNeedsContext:
    def test_yes_after_artwork_question_means_have(self):
        last = "Do you have print-ready artwork or want our design service?"
        assert _sniff_artwork_answer(last, "yes") is True

    def test_no_after_artwork_question_means_design(self):
        last = "Do you have print-ready artwork or want our design service?"
        assert _sniff_artwork_answer(last, "no") is False

    def test_yes_after_full_quote_question_does_not_fire(self):
        # Bare "yes" after Craig asked "want full quote?" should NOT
        # be read as "I have artwork".
        last = "Want me to put together the full quote for you? 📋"
        assert _sniff_artwork_answer(last, "yes") is None

    def test_yes_after_specs_confirmation_does_not_fire(self):
        last = "Got it — 100 business cards, soft-touch, double-sided?"
        assert _sniff_artwork_answer(last, "yes") is None
