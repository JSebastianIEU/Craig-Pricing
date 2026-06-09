"""
V44 — seed per-tenant `deepseek_temperature` setting.

The DeepSeek tool-calling temperature was hardcoded at 0.3 from launch
through v40.8.7. After running the D5 comprehensive smoke (28 scenarios
across all product families) we observed that temp=0.3 caused
inconsistent confirm-vs-tool-call behavior on single-message orders
("500 cards single sided I have artwork" — sometimes Craig confirms,
sometimes calls the tool). The graded confirm rule from v40.8.7 helped
but didn't fully resolve it.

This migration adds a tenant Setting so the temperature can be tuned
without a redeploy. Default seeded value is 0.3 (preserves existing
behavior). Sebastian can PATCH it down to 0.1 (or 0.0 for fully
deterministic) for just-print after deploy via the admin API:

  PATCH /admin/api/orgs/just-print/settings/deepseek_temperature
  body: {"value": "0.1"}

Idempotent (insert-if-missing).
Wired into scripts/startup.py after v43.

Usage:
    python -m scripts.v44_deepseek_temperature_seed
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db import db_session  # noqa: E402
from db.models import Setting  # noqa: E402


def seed() -> None:
    print("V44: seeding deepseek_temperature setting per tenant...")
    inserted = 0
    with db_session() as db:
        # Enumerate tenants via system_prompt (guaranteed by V4 seed).
        prompts = db.query(Setting).filter(Setting.key == "system_prompt").all()
        tenant_slugs = sorted({s.organization_slug for s in prompts})

        for slug in tenant_slugs:
            existing = (
                db.query(Setting)
                .filter_by(organization_slug=slug, key="deepseek_temperature")
                .first()
            )
            if existing:
                continue
            db.add(Setting(
                organization_slug=slug,
                key="deepseek_temperature",
                value="0.3",
                value_type="float",
                description=(
                    "Temperature passed to DeepSeek chat completions. "
                    "Range 0.0-2.0. Lower = more deterministic (better "
                    "for tool-calling consistency); higher = more creative."
                ),
            ))
            inserted += 1
            print(f"  + {slug}/deepseek_temperature = 0.3")

    print()
    print(f"✓ {inserted} deepseek_temperature settings inserted.")


if __name__ == "__main__":
    seed()
