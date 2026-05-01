"""
V27 migration — two settings tweaks Justin asked for after live smoke
on 2026-05-01:

  1. PrintLogic order_status: workshop uses "In Progress" for everything,
     not "Awaiting Production". Change the default + force-update the
     existing setting so the next push lands with the right state.

  2. Design-service copy: previous wording was "flat per-order fee, not
     per hour". Justin clarified the spec — €65 IS one hour of design.
     Force-reseed business_rules to phrase it that way consistently.

Idempotent. Safe to re-run.

Usage:
    python -m scripts.v27_one_hour_design_status
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db import db_session
from db.models import DEFAULT_ORG_SLUG, Setting


# Phase G v27 — business_rules text. Same structure as v25 with two
# changes: Rule #2 references "one hour of design" (not "flat per-order
# fee") and Rule #3 keeps the same wording. CRITICAL block updated.
BUSINESS_RULES_V27 = [
    # Rule 1 — first-turn greeting
    "On the first turn, do not duplicate the widget greeting. Reply "
    "with substance: ask one specific question or give a price. "
    "Never repeat 'Craig here' or 'I handle pricing'.",

    # Rule 2 — ARTWORK QUESTION IS ITS OWN MESSAGE
    "Once you have all required specs (product, quantity, finish, "
    "sides), DO NOT call the pricing tool, DO NOT confirm specs in "
    "this message, DO NOT mention 'full quote'. Your VERY NEXT "
    "message must be ONLY this question, nothing else:\n"
    "\n"
    "  'Got it. Quick question before I price it: do you have your "
    "  own print-ready artwork, or would you like our design service "
    "  (€65 ex VAT, €79.95 inc — one hour of design work)?'\n"
    "\n"
    "After you ask, WAIT for the customer's answer. Only on the NEXT "
    "turn do you call the pricing tool with the right needs_artwork "
    "value:\n"
    "  - artwork=true  -> needs_artwork=false (no design line item)\n"
    "  - design=true   -> needs_artwork=true, artwork_hours=1.0\n"
    "\n"
    "CRITICAL: the design service is €65 ex VAT for ONE HOUR of "
    "design. Always frame it as 'one hour of design' so the customer "
    "knows what €65 buys them. Do NOT say 'flat fee per order' or "
    "'per piece'. Just 'one hour of design'.\n"
    "\n"
    "Bundling the artwork question with anything else (spec recap, "
    "'want full quote?', the price itself) is a contractual breach. "
    "The widget renders an upload button for the customer right after "
    "this question — bundling other content makes that button render "
    "in the wrong place.",

    # Rule 3 — verbal price + offer full quote
    "AFTER the customer answers the artwork question (and you've "
    "captured it), call the pricing tool. Quote the inc-VAT total in "
    "one short sentence (e.g. 'That'll be €34.05 for 100 single-sided "
    "matte business cards 👍'), then ask 'Want me to put together "
    "the full quote for you? 📋'. Do NOT emit [QUOTE_READY] yet — "
    "the form/upload steps come first.",
]


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
    print("V27: workshop status fix + 'one hour of design' copy...")

    with db_session() as db:
        # ── 1. PrintLogic initial order status ─────────────────────────
        r = _seed_setting(
            db, "printlogic_initial_order_status", "In Progress", force=True,
        )
        print(f"  {r:>8}  setting printlogic_initial_order_status='In Progress'")

        # ── 2. Force-reseed business_rules with new design-service copy
        rules_json = json.dumps(BUSINESS_RULES_V27, ensure_ascii=False)
        r = _seed_setting(db, "business_rules", rules_json, force=True)
        print(f"  {r:>8}  setting business_rules ({len(BUSINESS_RULES_V27)} rules — force-reseeded)")


if __name__ == "__main__":
    migrate()
