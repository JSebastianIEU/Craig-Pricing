"""
Marketing attribution helpers.

The widget captures UTM params + ad click IDs from the landing-page URL
and sends them on every /chat and customer-info call. This module owns
the server-side merge logic so the rules live in ONE place:

  * first_touch is WRITE-ONCE (server-enforced) — a cleared or forged
    localStorage on a later visit can never overwrite the genuine first
    touch we already recorded.
  * last_touch is ALWAYS updated to the most recent click data.
  * identity backfill stitches a later email/phone-only conversation
    (e.g. an email enquiry) back to a prior attributed WEB session by
    the same customer — the only honest cross-channel link we can make.

No pricing or LLM deps here — keep it import-light so app.py, widget_api
and craig_agent can all use it.
"""

from __future__ import annotations

import re
from typing import Any, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from db.models import Conversation

# The only keys we persist. Anything else on the incoming dict is dropped
# (defends against a malicious page stuffing junk into the JSON column).
_ALLOWED_KEYS = (
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "gclid", "gbraid", "wbraid",
    "fbclid", "fbc", "fbp",
    "ttclid", "msclkid", "li_fat_id",
    "landing_page", "referrer", "captured_at",
)

# Cap stored string length so a hostile page can't bloat the row.
_MAX_VALUE_LEN = 512


def _clean_touch(d: Any) -> dict[str, str]:
    """Keep only allowed keys with non-empty string values, trimmed."""
    if not isinstance(d, dict):
        return {}
    out: dict[str, str] = {}
    for k in _ALLOWED_KEYS:
        v = d.get(k)
        if v is None:
            continue
        s = str(v).strip()
        if s:
            out[k] = s[:_MAX_VALUE_LEN]
    return out


def _split_touches(incoming: Any) -> tuple[dict, dict]:
    """Normalize an incoming attribution payload into (first, last).

    Accepts either the structured shape {first_touch, last_touch} that
    the widget stores, OR a flat dict of params (treated as both first
    and last). Returns cleaned dicts."""
    if not isinstance(incoming, dict):
        return {}, {}
    first = _clean_touch(incoming.get("first_touch"))
    last = _clean_touch(incoming.get("last_touch"))
    if not first and not last:
        # Flat payload — treat the whole thing as a single touch.
        flat = _clean_touch(incoming)
        return flat, flat
    return first, last


def merge_attribution(conv: Conversation, incoming: Any) -> bool:
    """Merge an incoming attribution payload into conv.attribution.

    first_touch is set only if not already present (write-once).
    last_touch is always overwritten with the latest non-empty touch.
    Returns True if anything changed. Reassigns conv.attribution (never
    mutates in place) because SQLAlchemy JSON columns don't track
    in-place mutation."""
    incoming_first, incoming_last = _split_touches(incoming)
    if not incoming_first and not incoming_last:
        return False

    current = dict(conv.attribution or {})
    changed = False

    if not current.get("first_touch") and incoming_first:
        current["first_touch"] = incoming_first
        changed = True

    if incoming_last and current.get("last_touch") != incoming_last:
        current["last_touch"] = incoming_last
        changed = True

    if changed:
        conv.attribution = current  # reassign so SQLAlchemy persists it
    return changed


def _digits(s: Optional[str]) -> str:
    return re.sub(r"\D", "", s or "")


def backfill_attribution_by_identity(db: Session, conv: Conversation) -> bool:
    """When `conv` has an email/phone but no first_touch yet, look for a
    prior attributed WEB conversation by the same customer and copy its
    first_touch over. Records the stitched session in `merged_from`.

    This is the only honest cross-channel link: an email/WhatsApp lead
    carries no UTM/click IDs of its own, but if the same person clicked
    an ad and landed on the site earlier, we can attribute them by
    identity. Returns True if a backfill happened."""
    # Already has a first touch — nothing to backfill.
    if (conv.attribution or {}).get("first_touch"):
        return False

    email = (conv.customer_email or "").strip().lower()
    phone_digits = _digits(conv.customer_phone)
    if not email and not phone_digits:
        return False

    q = (
        db.query(Conversation)
        .filter(
            Conversation.organization_slug == conv.organization_slug,
            Conversation.id != conv.id,
            Conversation.channel == "web",
            Conversation.attribution.isnot(None),
        )
        .order_by(Conversation.created_at.desc())
    )
    if email:
        q = q.filter(func.lower(Conversation.customer_email) == email)

    for prior in q.limit(50).all():
        prior_attr = prior.attribution or {}
        prior_first = prior_attr.get("first_touch")
        if not prior_first:
            continue
        # If we matched on phone (no email), verify digits match in Python
        # since phone formats vary too much for a reliable SQL compare.
        if not email:
            if _digits(prior.customer_phone) != phone_digits:
                continue
        # Found a prior attributed session for this identity — copy it.
        current = dict(conv.attribution or {})
        current["first_touch"] = prior_first
        # Seed last_touch too if we have nothing newer.
        if not current.get("last_touch"):
            current["last_touch"] = prior_attr.get("last_touch") or prior_first
        merged_from = list(current.get("merged_from") or [])
        if prior.external_id and prior.external_id not in merged_from:
            merged_from.append(prior.external_id)
        current["merged_from"] = merged_from
        conv.attribution = current
        return True

    return False
