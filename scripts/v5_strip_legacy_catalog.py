"""
V5 migration: strip the legacy hardcoded catalog block from stored
`system_prompt` settings + seed an empty `business_rules` row.

Why: as of V5 the LLM is handed a live catalog snapshot (built from the
Products / PriceTiers tables) plus any tenant-configured business rules.
Keeping the old hardcoded "## Products and their ACTUAL available options"
block in the stored prompt duplicates that info and can drift from reality.

This script is idempotent: if a prompt doesn't contain the legacy marker it
is left untouched, and if `business_rules` is already present it is skipped.

Usage:
    python -m scripts.v5_strip_legacy_catalog
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db import db_session, init_db
from db.models import Setting
from llm.craig_agent import LEGACY_CATALOG_MARKER


def _strip_legacy_catalog(prompt: str) -> str:
    """Remove everything from the legacy catalog marker to end-of-string."""
    idx = prompt.find(LEGACY_CATALOG_MARKER)
    if idx == -1:
        return prompt
    return prompt[:idx].rstrip() + "\n"


def migrate() -> None:
    print("V5: stripping legacy catalog block + seeding business_rules...")
    stripped = 0
    rules_seeded = 0
    with db_session() as db:
        # 1) Strip the legacy catalog block from every tenant's system_prompt
        prompts = db.query(Setting).filter(Setting.key == "system_prompt").all()
        for s in prompts:
            if not s.value or LEGACY_CATALOG_MARKER not in s.value:
                continue
            new_value = _strip_legacy_catalog(s.value)
            if new_value != s.value:
                s.value = new_value
                stripped += 1
                print(f"  \u2702 stripped catalog from {s.organization_slug}/system_prompt")

        # 2) Seed an empty business_rules row per tenant that has a system_prompt.
        #    Upsert path via admin API also works, but this guarantees the row
        #    exists so the dashboard list view starts from a clean empty state.
        tenant_slugs = {s.organization_slug for s in prompts}
        for slug in tenant_slugs:
            existing = (
                db.query(Setting)
                .filter_by(organization_slug=slug, key="business_rules")
                .first()
            )
            if existing:
                continue
            db.add(Setting(
                organization_slug=slug,
                key="business_rules",
                value="[]",
                value_type="json",
                description=(
                    "Extra business rules the LLM sees on every turn. "
                    "JSON array of plain-English strings."
                ),
            ))
            rules_seeded += 1
            print(f"  + seeded empty business_rules for {slug}")

    print()
    print(f"\u2713 {stripped} prompts stripped, {rules_seeded} business_rules rows seeded.")


if __name__ == "__main__":
    init_db()
    migrate()
