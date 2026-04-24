"""
V13 seed — per-tenant PrintLogic settings.

Idempotent. For every tenant that has a `system_prompt` row (i.e. exists
in our multi-tenant universe), insert three defaults if they're missing:

  - printlogic_api_key   = ""              (user pastes it via the dashboard)
  - printlogic_dry_run   = "true"          (SAFE DEFAULT — no real writes)
  - printlogic_firm_id   = ""              (optional, for multi-firm accounts)

The `printlogic_dry_run=true` default is the core safety invariant of
Phase A: even if someone deploys the code and enables the integration in
the dashboard, no real PrintLogic order gets created until this flag is
explicitly flipped to `"false"` in a supervised ceremony.

Never overwrites an existing row — if the user already set an api_key,
this script doesn't touch it.

Usage:
    python -m scripts.v13_printlogic_settings_seed
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db import db_session, init_db
from db.models import Setting


SEED_ROWS: list[tuple[str, str, str, str]] = [
    # (key, value, value_type, description)
    (
        "printlogic_api_key",
        "",
        "string",
        "PrintLogic API token. Paste from Just Print's PrintLogic admin. Never commit.",
    ),
    (
        "printlogic_dry_run",
        "true",
        "string",
        "Safety flag. 'true' makes create_order return a synthetic DRY-xxxx id WITHOUT "
        "hitting the real PrintLogic API. Flip to 'false' only in a supervised ceremony "
        "with Justin watching his PrintLogic UI.",
    ),
    (
        "printlogic_firm_id",
        "",
        "string",
        "Optional. Some PrintLogic accounts span multiple firms; this namespaces pushes.",
    ),
]


def seed() -> None:
    print("V13: seeding PrintLogic settings per tenant...")
    init_db()
    inserted = 0
    with db_session() as db:
        prompts = db.query(Setting).filter(Setting.key == "system_prompt").all()
        tenant_slugs = sorted({s.organization_slug for s in prompts})
        if not tenant_slugs:
            print("  \u00b7 no tenants found (no system_prompt rows yet); nothing to seed.")
            return

        for slug in tenant_slugs:
            for key, value, value_type, description in SEED_ROWS:
                existing = (
                    db.query(Setting)
                    .filter_by(organization_slug=slug, key=key)
                    .first()
                )
                if existing:
                    print(f"  \u00b7 {slug}/{key} already present")
                    continue
                db.add(Setting(
                    organization_slug=slug,
                    key=key,
                    value=value,
                    value_type=value_type,
                    description=description,
                ))
                inserted += 1
                print(f"  + {slug}/{key}  (value={value!r})")

    print()
    print(f"\u2713 {inserted} settings inserted.")


if __name__ == "__main__":
    seed()
