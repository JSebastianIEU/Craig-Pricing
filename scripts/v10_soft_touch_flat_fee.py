"""
V10: align the `soft_touch` surcharge with Justin's pricing sheet.

Justin's canonical pricing (April 2026) says soft-touch is:
  - available ONLY on Business Cards
  - a FLAT +€15 fee per job (not a +25% multiplier)

Craig was previously applying +25% globally, which overcharges at small
quantities (≈€7.50 instead of €15 on 100 cards) and WAY overcharges at
large runs (+€150 on 2500 cards instead of the +€15 Justin expects).

This migration finds the `soft_touch` SurchargeRule for every tenant and
updates it to `kind="additive", multiplier=15.0`. The `multiplier` column
holds the euro amount when `kind="additive"` — see the comment on the
SurchargeRule model.

Idempotent — re-running is a no-op for rows that already look right.

Usage:
    python -m scripts.v10_soft_touch_flat_fee
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db import db_session, init_db
from db.models import SurchargeRule


TARGET_NAME = "soft_touch"
TARGET_KIND = "additive"
TARGET_AMOUNT = 15.0
TARGET_CATEGORY = "small_format"
TARGET_DESCRIPTION = (
    "Soft-touch laminate finish. Flat +\u20ac15 per job. "
    "Only available on business cards."
)


def migrate() -> None:
    print(f"V10: setting soft_touch to additive \u20ac{TARGET_AMOUNT:.0f} flat...")
    updated = 0
    skipped = 0
    with db_session() as db:
        rules = db.query(SurchargeRule).filter(SurchargeRule.name == TARGET_NAME).all()
        for r in rules:
            needs_update = (
                (r.kind or "").strip().lower() != TARGET_KIND
                or abs((r.multiplier or 0.0) - TARGET_AMOUNT) > 0.001
                or (r.applies_to_category or "") != TARGET_CATEGORY
            )
            if not needs_update:
                print(f"  \u00b7 {r.organization_slug}/soft_touch already configured correctly")
                skipped += 1
                continue
            print(
                f"  \u2702 {r.organization_slug}/soft_touch: "
                f"kind {r.kind!r}\u2192{TARGET_KIND!r}, "
                f"amount {r.multiplier!r}\u2192{TARGET_AMOUNT}, "
                f"scope \u2192{TARGET_CATEGORY}"
            )
            r.kind = TARGET_KIND
            r.multiplier = TARGET_AMOUNT
            r.applies_to_category = TARGET_CATEGORY
            r.description = TARGET_DESCRIPTION
            updated += 1

    print()
    print(f"\u2713 {updated} rules updated, {skipped} already aligned.")


if __name__ == "__main__":
    init_db()
    migrate()
