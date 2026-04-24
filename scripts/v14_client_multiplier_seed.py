"""
V14 seed — per-tenant `pricing_client_multiplier` default.

Roi + Justin agreed: one base price per product plus a per-client
percentage multiplier. Applied inside the pricing engine AFTER all
surcharges and BEFORE VAT (see `pricing_engine._get_client_multiplier`
+ each `quote_*` function).

This seed gives every tenant a neutral 1.0 (no adjustment) on first
deploy, so nothing changes for current quotes. Flip from the dashboard
Settings tab per client as needed.

Idempotent — won't overwrite an existing row.

Usage:
    python -m scripts.v14_client_multiplier_seed
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db import db_session, init_db
from db.models import Setting


KEY = "pricing_client_multiplier"
DEFAULT_VALUE = "1.0"
DESCRIPTION = (
    "Per-tenant pricing multiplier applied AFTER surcharges and BEFORE VAT. "
    "`1.0` = no adjustment. Example: `1.10` = +10% across every quote, "
    "`0.90` = -10% (volume discount client)."
)


def seed() -> None:
    print("V14: seeding pricing_client_multiplier per tenant...")
    init_db()
    inserted = 0
    with db_session() as db:
        # Enumerate tenants by their system_prompt rows (guaranteed by V4)
        prompts = db.query(Setting).filter(Setting.key == "system_prompt").all()
        tenant_slugs = sorted({s.organization_slug for s in prompts})
        if not tenant_slugs:
            print("  \u00b7 no tenants found; nothing to seed.")
            return

        for slug in tenant_slugs:
            existing = (
                db.query(Setting)
                .filter_by(organization_slug=slug, key=KEY)
                .first()
            )
            if existing:
                print(f"  \u00b7 {slug}/{KEY} already present (value={existing.value!r})")
                continue
            db.add(Setting(
                organization_slug=slug,
                key=KEY,
                value=DEFAULT_VALUE,
                value_type="float",
                description=DESCRIPTION,
            ))
            inserted += 1
            print(f"  + {slug}/{KEY} = {DEFAULT_VALUE}")

    print()
    print(f"\u2713 {inserted} tenants seeded.")


if __name__ == "__main__":
    seed()
