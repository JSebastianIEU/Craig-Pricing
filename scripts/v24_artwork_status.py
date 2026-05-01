"""
V24 migration — Phase F refined: explicit artwork status + flow fix.

Justin's smoke-test of v23 surfaced two issues:

  (a) Craig priced after spec confirmation BUT skipped the artwork
      question, so the [ARTWORK_UPLOAD] button never appeared and the
      [CUSTOMER_FORM] gate fired before the customer saw a price.
  (b) The upload button auto-emitted whenever pricing tool ran with
      needs_artwork=False (the default) — so even when the customer
      didn't say they had artwork, the button showed up.

Fix:

  1. New `Conversation.customer_has_own_artwork` column (BOOLEAN NULL).
     Set by a server-side sniff (in `chat_with_craig`) that looks at
     the previous assistant message and the current user message.
     - "do you have artwork?" + "yes" / "I have"   -> True
     - "do you have artwork?" + "design" / "no"    -> False
     - everything else                              -> stays null

  2. Auto-emit gates now require `customer_has_own_artwork` to be set
     before they fire. [ARTWORK_UPLOAD] only when True; [CUSTOMER_FORM]
     only after the question has been answered (not null).

  3. business_rules force-reseeded with a stricter rule #3 — Craig
     MUST ask artwork as a separate step BEFORE pricing, in its OWN
     message (not bundled with spec confirmation). Asking + pricing
     in one shot is what made the LLM skip the question last time.

  4. The form-submit endpoint now writes a canned [QUOTE_READY] reply
     directly into the conversation transcript (no LLM round-trip),
     which removes the cosmetic "re-quote" duplication that happened
     when the LLM responded to the synthetic [SYSTEM] trigger.

Idempotent. Safe to re-run.

Usage:
    python -m scripts.v24_artwork_status
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import inspect, text

from db import db_session, engine, init_db
from db.models import DEFAULT_ORG_SLUG, Setting


_COLUMN_DEFS = [
    ("conversations", "customer_has_own_artwork", "BOOLEAN NULL"),
]


# Stricter Phase F flow — rule #3 split into 3a (ask artwork) + 3b
# (price + upload button). Crucial fix: the artwork question is now
# its OWN message, not bundled with spec confirmation. That gives the
# LLM zero excuse to skip it.
BUSINESS_RULES_V24 = [
    # Rule 1 — first-turn greeting
    "On the first turn, do not duplicate the widget greeting. The widget "
    "already opened with 'Hey — Craig here'. Reply with substance: ask one "
    "specific question or give a price. Never repeat 'Craig here' or 'I "
    "handle pricing'.",

    # Rule 2 — ALWAYS ask artwork before pricing (separate turn)
    "Once you have all required specs (product, quantity, finish where "
    "applicable, sides), DO NOT call the pricing tool yet. First ask, in a "
    "SEPARATE message dedicated to this question only:\n"
    "  'Do you have print-ready artwork, or would you like our design "
    "  service? It's a flat €65 ex VAT (€79.95 inc VAT) one-time fee per "
    "  order.'\n"
    "CRITICAL: the design service is a FLAT per-order fee, NOT per hour. "
    "NEVER say 'per hour', '/hr', 'hourly', or anything implying hours — "
    "it's a one-time charge for designing the artwork for this order, "
    "regardless of how long it takes us. Wait for the customer to answer. "
    "ONLY THEN call the pricing tool with the right needs_artwork value:\n"
    "  - artwork=true  -> needs_artwork=false (no design line item)\n"
    "  - design=true   -> needs_artwork=true, artwork_hours=1.0 (€65 added)\n"
    "Skipping this question is a contractual breach — we cannot quote "
    "without knowing whether design service is included.",

    # Rule 3 — verbal price + offer full quote
    "After the customer answers the artwork question, call the pricing "
    "tool and quote the inc-VAT total in plain text. If the customer "
    "said they HAVE artwork, end the message with [ARTWORK_UPLOAD] on "
    "its own line so the widget renders the upload button. Then ask "
    "'Want me to put together the full quote? 📋'. Don't gate the "
    "verbal price behind contact collection — it's free and non-binding.",

    # Rule 4 — full-quote ask + form trigger
    "When the customer says yes to 'want full quote?', emit "
    "[CUSTOMER_FORM] on its own line. The widget renders the structured "
    "form (name, email, company/individual, returning?, delivery method, "
    "address). Do NOT ask these questions in free text — the form has "
    "validation and is faster + cleaner.",

    # Rule 5 — post-form acknowledgement (rarely fires now — server short-
    # circuits the form-submit response, but keep this as a fallback in
    # case the LLM is invoked on a [SYSTEM] turn).
    "If you receive a system message saying the customer submitted the "
    "form, reply briefly — 'All set 👍' — and emit [QUOTE_READY] on its "
    "own line. The PDF card will render. Do NOT re-quote the price; the "
    "customer already saw it.",

    # Rule 6 — escalations gated on contact
    "Escalations to Justin (escalate_to_justin tool) require contact info "
    "first. If you need to escalate but don't have name + email/phone, "
    "ask for them first, save_customer_info, then escalate.",

    # Rule 7 — FAQ knowledge inline
    "A list of frequently asked questions is injected separately under "
    "## Frequently asked questions. When the customer asks one (or "
    "anything close), answer it INLINE in your own voice — paraphrase, "
    "don't read verbatim. Do not escalate FAQs.",

    # Rule 8 — Ireland-only
    "Just Print operates in Ireland only. If a customer asks for delivery "
    "outside Ireland (UK, EU, US, anywhere else), politely tell them we "
    "only ship within Ireland and offer collection from our Ballymount "
    "shop as an alternative. Don't proceed with the quote until they "
    "confirm an Irish address or pick collection.",
]


def _column_exists(conn, table: str, column: str) -> bool:
    return column in [c["name"] for c in inspect(conn).get_columns(table)]


def _add_column_if_missing(conn, table: str, column_name: str, column_def: str) -> bool:
    if _column_exists(conn, table, column_name):
        return False
    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column_name} {column_def}"))
    return True


def _seed_setting(db, key: str, value: str, *, force: bool = False) -> str:
    existing = (
        db.query(Setting)
        .filter_by(organization_slug=DEFAULT_ORG_SLUG, key=key)
        .first()
    )
    if existing:
        if not force:
            return "skipped"
        existing.value = value
        return "updated"
    db.add(Setting(
        organization_slug=DEFAULT_ORG_SLUG,
        key=key, value=value, value_type="string",
    ))
    return "added"


def migrate() -> None:
    print("V24: Phase F refined — artwork status + flow fix...")
    init_db()

    added = 0
    with engine.begin() as conn:
        for table, name, defn in _COLUMN_DEFS:
            if _add_column_if_missing(conn, table, name, defn):
                print(f"  + {table}.{name}")
                added += 1
            else:
                print(f"  · {table}.{name} already present")

    with db_session() as db:
        rules_json = json.dumps(BUSINESS_RULES_V24, ensure_ascii=False)
        r = _seed_setting(db, "business_rules", rules_json, force=True)
        print(f"  {r:>8}  setting business_rules ({len(BUSINESS_RULES_V24)} rules — force-reseeded)")
        db.commit()

    print()
    print(f"✓ {added} columns added.")


if __name__ == "__main__":
    migrate()
