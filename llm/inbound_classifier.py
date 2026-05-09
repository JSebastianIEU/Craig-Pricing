"""
Inbound email triage filter for the Missive integration.

Why this exists: Missive POSTs every inbound email to our webhook,
including spam, mailing-list noise, auto-replies, and totally
unrelated mail. Without a filter Craig drafts a reply to ALL of them
— Justin's drafts inbox fills up with junk.

This module is a thin wrapper around the existing DeepSeek client
that classifies a single inbound email as either:
    {"is_quote_inquiry": true|false, "reason": "<short reason>"}

We use this AFTER a cheap structural pre-reject (`_obvious_junk` in
`app.py`) — that catches no-reply senders / mailer-daemons / bounces
without spending a token. The LLM call is only for ambiguous mail
that passes the prefilter (most actual inbound).

Failure modes (timeout, network error, bad JSON): default to
`is_quote_inquiry=True` (fail-open). Better to draft a draft Justin
can throw away than to silently swallow a real lead.
"""

from __future__ import annotations

import json
import os
from typing import Any

from openai import OpenAI


DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1"
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")


# ---------------------------------------------------------------------------
# Hard-reject prefilter — runs before the LLM, costs nothing.
# ---------------------------------------------------------------------------


# Local-part prefixes that signal an automated / no-reply mailbox.
# We compare against the part BEFORE the `@` so `bounces+abc@mg.org`
# matches via the `bounce` prefix (Mailgun's tagged-bounce convention).
_NO_REPLY_LOCAL_PREFIXES = (
    "no-reply", "noreply", "no_reply",
    "do-not-reply", "donotreply", "do_not_reply",
    "mailer-daemon", "postmaster",
    "bounce", "bounces",
    "notifications", "notify", "alerts",
)

_BAD_SUBJECT_KEYWORDS = (
    "out of office", "auto-reply", "auto reply", "automatic reply",
    "delivery failure", "undeliverable", "delivery status notification",
    "mail delivery", "returned mail", "failure notice",
    "unsubscribe",
)


def obvious_junk(
    *,
    from_address: str,
    subject: str,
    headers: dict | None = None,
) -> str | None:
    """
    Cheap structural prefilter — returns a reason string if the email
    is obviously not a quote inquiry, else None. Runs before the LLM
    classifier so we don't burn tokens on bouncebacks and mailing-list
    noise.

    Detects:
      - no-reply / mailer-daemon / postmaster / bounces / notifications senders
      - subject keywords for out-of-office, auto-reply, undeliverable, unsubscribe
      - List-Unsubscribe / X-Auto-Response-Suppress / Auto-Submitted headers

    `headers` is optional — Missive doesn't always populate it. Treated
    as case-insensitive when present.
    """
    fa = (from_address or "").lower().strip()
    subj = (subject or "").lower().strip()

    # Check the local part (before @) against the no-reply prefix list.
    # Catches both "noreply@x.com" and "bounces+abc@mg.org".
    local_part = fa.split("@", 1)[0] if "@" in fa else fa
    if any(local_part.startswith(p) for p in _NO_REPLY_LOCAL_PREFIXES):
        return f"no-reply sender ({fa[:60]})"

    for kw in _BAD_SUBJECT_KEYWORDS:
        if kw in subj:
            return f"subject keyword: {kw!r}"

    if headers:
        # Lowercase the header names once
        lc = {str(k).lower(): str(v) for k, v in headers.items()}
        if "list-unsubscribe" in lc:
            return "mailing list (list-unsubscribe header)"
        if "x-auto-response-suppress" in lc:
            return "auto-response (suppress header)"
        auto_sub = lc.get("auto-submitted", "").lower()
        if auto_sub and auto_sub != "no":
            return f"auto-submitted: {auto_sub[:40]}"

    return None


# v37 — confidence floor below which we treat the email as outright junk
# (silent drop, same as `is_quote_inquiry=False`). Above this floor but
# below the per-tenant `engagement_confidence_threshold` setting → the
# webhook pauses Craig and notifies Justin to approve engagement.
LOW_CONFIDENCE_FLOOR = 0.2


_SYSTEM_PROMPT = (
    "You are a triage filter for an Irish print shop (Just Print). "
    "Decide whether the email below is from a real human asking about "
    "printing services, design, signage, or a quote / quote follow-up.\n"
    "\n"
    "TRUE (quote-related): quote requests (cards, flyers, brochures, "
    "letterheads, posters, signage, banners, NCR, stationery, "
    "booklets, etc), questions about pricing or turnaround, design "
    "service inquiries, replies to an in-progress quote.\n"
    "\n"
    "FALSE (not quote-related): cold sales pitches (someone selling US "
    "a service), promotional / marketing emails, newsletters, "
    "automated notifications, mailing-list traffic, out-of-office "
    "replies, anything spammy.\n"
    "\n"
    "Also return a CONFIDENCE score from 0.0 to 1.0 reflecting how "
    "sure you are about your verdict. Calibration:\n"
    "  >=0.95  obvious quote request with explicit product+qty, OR "
    "obvious spam/sales pitch.\n"
    "  0.85–0.94  clearly print-related but light on detail "
    "(\"can you do business cards?\").\n"
    "  0.50–0.84  ambiguous: vague greeting, asking generally about "
    "services, could be a customer or a vendor pitch.\n"
    "  0.20–0.49  probably not a quote but unsure (random question, "
    "off-topic message from a real human).\n"
    "  <0.20  almost certainly junk / spam / mailing list.\n"
    "\n"
    "If you're not 90%+ sure the sender wants a print quote, return a "
    "confidence below 0.85 — Justin will be asked to approve before "
    "Craig replies.\n"
    "\n"
    "Respond with JSON ONLY in this exact shape:\n"
    '  {"is_quote_inquiry": true, "confidence": 0.92, '
    '"reason": "short reason ≤15 words"}\n'
    "or\n"
    '  {"is_quote_inquiry": false, "confidence": 0.97, '
    '"reason": "short reason ≤15 words"}'
)


def classify_inbound_email(
    *,
    from_address: str,
    subject: str,
    body_preview: str,
    is_thread_reply: bool = False,
    last_assistant_snippet: str = "",
) -> dict[str, Any]:
    """
    Returns {"is_quote_inquiry": bool, "confidence": float, "reason": str}.

    Args:
      from_address:    sender's email
      subject:         email subject line
      body_preview:    first ~800 chars of the email body (we cap to
                       keep tokens low — the LLM doesn't need the full
                       message to make this call)
      is_thread_reply: if True, this email is part of a Missive
                       conversation Craig already drafted in. v37.4 —
                       this is now a HINT to the LLM (added to the
                       user message), NOT a hard short-circuit. The
                       LLM weighs it: a clear quote-related continuation
                       comes back with verdict=True high confidence; an
                       off-topic follow-up still gets routed to the
                       gate (Tier 2) so Justin can decide.
      last_assistant_snippet: optional, ~200 chars from Craig's last
                       assistant turn — gives the LLM the context it
                       needs to judge short customer replies like
                       'yes please' or '250 of those' that are
                       meaningless without prior context.

    v37 — adds `confidence` (0.0–1.0). The webhook uses it for a
    three-tier decision: <LOW_CONFIDENCE_FLOOR drop, <threshold pause +
    notify Justin, ≥threshold respond.

    v37.4 — removed the is_thread_reply short-circuit. Off-topic
    follow-ups in already-engaged conversations now route to Justin
    instead of auto-replying.

    Failure modes (timeout, network, malformed JSON) → return
    {"is_quote_inquiry": True, "confidence": 1.0,
     "reason": "classifier-error fail-open"} so a real lead is never
    silently swallowed.
    """
    if not DEEPSEEK_API_KEY:
        # Defensive: in dev / tests where the key isn't set, don't
        # accidentally drop emails. Same fail-open posture.
        return {
            "is_quote_inquiry": True,
            "confidence": 1.0,
            "reason": "no DEEPSEEK_API_KEY (fail-open)",
        }

    # Cap body to 800 chars — enough context for the verdict, cheap
    # in tokens. Trim trailing whitespace so the LLM doesn't waste
    # tokens on signatures.
    preview = (body_preview or "").strip()[:800]

    # v37.4 — surface the conversation state in the user message so
    # short replies ('yes', '250') in active threads are classified
    # against their context, not in isolation.
    thread_hint = ""
    if is_thread_reply:
        thread_hint = (
            "Note: this email is a reply in a thread Craig (the print-"
            "shop bot) already wrote in. The customer is mid-"
            "conversation — short or context-light messages are "
            "normal IF they make sense as a continuation of a quote.\n"
        )
        if last_assistant_snippet:
            snip = last_assistant_snippet.strip()[:300]
            thread_hint += (
                f"Craig's last message (for context): {snip!r}\n"
            )
    user_msg = (
        f"{thread_hint}"
        f"From: {from_address!r}\n"
        f"Subject: {subject!r}\n"
        f"Body (first ~800 chars):\n"
        f"{preview!r}"
    )

    try:
        client = OpenAI(api_key=DEEPSEEK_API_KEY, base_url=DEEPSEEK_BASE_URL)
        resp = client.chat.completions.create(
            model=DEEPSEEK_MODEL,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            response_format={"type": "json_object"},
            temperature=0.0,
            max_tokens=80,
            timeout=10.0,
        )
        content = (resp.choices[0].message.content or "").strip()
        parsed = json.loads(content)
        verdict = bool(parsed.get("is_quote_inquiry", True))
        # v37 — confidence is float 0..1. Tolerate either a float or a
        # 0..100 percentage from the LLM; clamp to [0,1] either way.
        raw_conf = parsed.get("confidence", 1.0 if verdict else 0.0)
        try:
            conf = float(raw_conf)
        except (TypeError, ValueError):
            conf = 1.0 if verdict else 0.0
        if conf > 1.5:  # the LLM returned 0..100 instead of 0..1
            conf = conf / 100.0
        conf = max(0.0, min(1.0, conf))
        reason = str(parsed.get("reason", ""))[:200]
        result = {
            "is_quote_inquiry": verdict,
            "confidence": conf,
            "reason": reason,
        }
        print(
            f"[inbound_classifier] from={from_address!r} "
            f"subject={subject[:60]!r} verdict={verdict} "
            f"confidence={conf:.2f} reason={reason!r}",
            flush=True,
        )
        return result
    except Exception as e:
        # Fail-open: any error returns True at full confidence. Better
        # a junk draft than a missed lead.
        msg = f"classifier-error: {type(e).__name__}: {str(e)[:120]}"
        print(
            f"[inbound_classifier] from={from_address!r} "
            f"subject={subject[:60]!r} ERROR fail-open. {msg}",
            flush=True,
        )
        return {"is_quote_inquiry": True, "confidence": 1.0, "reason": msg}
