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


_SYSTEM_PROMPT = (
    "You are a triage filter for an Irish print shop (Just Print). "
    "Decide whether the email below is from a real human asking about "
    "printing services, design, signage, or a quote / quote follow-up.\n"
    "\n"
    "Return TRUE for: quote requests (cards, flyers, brochures, "
    "letterheads, posters, signage, banners, NCR, stationery, "
    "booklets, etc), questions about pricing or turnaround, design "
    "service inquiries, or replies to an in-progress quote.\n"
    "\n"
    "Return FALSE for: cold sales pitches (someone selling US a "
    "service), promotional / marketing emails, newsletters, "
    "automated notifications, mailing-list traffic, out-of-office "
    "replies, anything spammy.\n"
    "\n"
    "Respond with JSON ONLY in this exact shape:\n"
    '  {"is_quote_inquiry": true, "reason": "short reason ≤15 words"}\n'
    "or\n"
    '  {"is_quote_inquiry": false, "reason": "short reason ≤15 words"}'
)


def classify_inbound_email(
    *,
    from_address: str,
    subject: str,
    body_preview: str,
    is_thread_reply: bool = False,
) -> dict[str, Any]:
    """
    Returns {"is_quote_inquiry": bool, "reason": str}.

    Args:
      from_address:    sender's email
      subject:         email subject line
      body_preview:    first ~800 chars of the email body (we cap to
                       keep tokens low — the LLM doesn't need the full
                       message to make this call)
      is_thread_reply: if True, this email is part of a Missive
                       conversation Craig already drafted in. We
                       short-circuit to True without an LLM call —
                       customers replying to Craig's drafts are by
                       definition real customers.

    Failure modes (timeout, network, malformed JSON) → return
    {"is_quote_inquiry": True, "reason": "classifier-error fail-open"}.
    """
    if is_thread_reply:
        return {
            "is_quote_inquiry": True,
            "reason": "thread reply (skip LLM)",
        }

    if not DEEPSEEK_API_KEY:
        # Defensive: in dev / tests where the key isn't set, don't
        # accidentally drop emails. Same fail-open posture.
        return {
            "is_quote_inquiry": True,
            "reason": "no DEEPSEEK_API_KEY (fail-open)",
        }

    # Cap body to 800 chars — enough context for the verdict, cheap
    # in tokens. Trim trailing whitespace so the LLM doesn't waste
    # tokens on signatures.
    preview = (body_preview or "").strip()[:800]
    user_msg = (
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
        reason = str(parsed.get("reason", ""))[:200]
        result = {"is_quote_inquiry": verdict, "reason": reason}
        print(
            f"[inbound_classifier] from={from_address!r} "
            f"subject={subject[:60]!r} verdict={verdict} reason={reason!r}",
            flush=True,
        )
        return result
    except Exception as e:
        # Fail-open: any error returns True. Better a junk draft than
        # a missed lead.
        msg = f"classifier-error: {type(e).__name__}: {str(e)[:120]}"
        print(
            f"[inbound_classifier] from={from_address!r} "
            f"subject={subject[:60]!r} ERROR fail-open. {msg}",
            flush=True,
        )
        return {"is_quote_inquiry": True, "reason": msg}
