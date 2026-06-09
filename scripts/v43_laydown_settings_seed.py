"""
V43 — seed laydown calculator settings.

Justin's `Laydown1.xls` (Sheet-Fit calculator) drives custom-size pricing
for board products. The math needs four constants that vary per print
shop. Defaults below come from Just-Print's actual settings (Justin
confirmed June 2026):

    laydown_bleed_mm       =  6   # per side, customer art panel
    laydown_grip_front_mm  = 15   # press grip, leading edge
    laydown_grip_back_mm   =  5   # press grip, trailing edge
    laydown_grip_side_mm   =  5   # left + right combined (each)

Engine usage (in `_quote_large_format_with_laydown`):

    effective_panel_w = custom_width_mm  + 2 * bleed_mm
    effective_panel_h = custom_height_mm + 2 * bleed_mm
    effective_sheet_w = sheet_w - 2 * grip_side_mm
    effective_sheet_h = sheet_h - grip_front_mm - grip_back_mm
    units_per_sheet   = _units_per_sheet(panel_w, panel_h, sheet_w, sheet_h)
    sheets_needed     = ceil(qty / units_per_sheet)
    price             = tier lookup at spec_key="2440x1220" qty=sheets_needed

Idempotent (insert-if-missing). Multi-tenant scoped via Setting.organization_slug.
Wired into scripts/startup.py after v42.

Usage:
    python -m scripts.v43_laydown_settings_seed
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db import db_session  # noqa: E402
from db.models import Setting  # noqa: E402


# (key, default_value_str, value_type, description)
SEED_ROWS = [
    (
        "laydown_bleed_mm",
        "6",
        "int",
        "Bleed (mm) added per side to customer's panel dimensions before laydown calculation.",
    ),
    (
        "laydown_grip_front_mm",
        "15",
        "int",
        "Press grip (mm) on the leading edge of the sheet; subtracted from effective sheet height.",
    ),
    (
        "laydown_grip_back_mm",
        "5",
        "int",
        "Press grip (mm) on the trailing edge of the sheet; subtracted from effective sheet height.",
    ),
    (
        "laydown_grip_side_mm",
        "5",
        "int",
        "Press grip (mm) on each side of the sheet; subtracted twice from effective sheet width.",
    ),
]


def seed() -> None:
    print("V43: seeding laydown calculator settings per tenant...")
    inserted = 0
    with db_session() as db:
        # Enumerate tenants via system_prompt (guaranteed by V4 seed) —
        # same pattern as V9 missive settings.
        prompts = db.query(Setting).filter(Setting.key == "system_prompt").all()
        tenant_slugs = sorted({s.organization_slug for s in prompts})

        for slug in tenant_slugs:
            for key, default_value, value_type, description in SEED_ROWS:
                existing = (
                    db.query(Setting)
                    .filter_by(organization_slug=slug, key=key)
                    .first()
                )
                if existing:
                    continue
                db.add(Setting(
                    organization_slug=slug,
                    key=key,
                    value=default_value,
                    value_type=value_type,
                    description=description,
                ))
                inserted += 1
                print(f"  + {slug}/{key} = {default_value}")

    print()
    print(f"✓ {inserted} laydown settings inserted.")


if __name__ == "__main__":
    seed()
