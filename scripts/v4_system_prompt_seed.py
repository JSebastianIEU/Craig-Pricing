"""
V4 seed: store Craig's system prompt + widget greeting as tenant-scoped
Settings rows so they're editable from the dashboard.

Idempotent. Re-running is safe — only inserts rows that don't exist yet.

Seeds for DEFAULT_ORG_SLUG ('just-print'):
  - system_prompt       → the code-level CRAIG_SYSTEM_PROMPT
  - widget_greeting     → Craig's opening line in the widget
  - widget_primary_color, widget_font                → Just Print branding

Usage:
    python -m scripts.v4_system_prompt_seed
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db import db_session, init_db
from db.models import Setting, DEFAULT_ORG_SLUG
from llm.craig_agent import CRAIG_SYSTEM_PROMPT


SEED_SETTINGS: list[tuple[str, str, str, str]] = [
    # (key, value, value_type, description)
    (
        "system_prompt",
        CRAIG_SYSTEM_PROMPT,
        "string",
        "Craig's conversational persona + rules. Editable per-tenant from the dashboard.",
    ),
    (
        "widget_greeting",
        "Hey! Craig here, I handle pricing at Just Print \U0001f5a8\ufe0f What are you looking to get printed?",
        "string",
        "Opening line the widget shows when a customer opens the chat.",
    ),
    (
        "widget_primary_color",
        "#040f2a",
        "string",
        "Main brand color used by the widget (header, buttons, focus ring).",
    ),
    (
        "widget_font",
        "Poppins",
        "string",
        "Google Fonts family the widget loads.",
    ),
    (
        "widget_accent_pink",
        "#e30686",
        "string",
        "Rainbow accent (used in the widget's top stripe + quote card).",
    ),
    (
        "widget_accent_yellow",
        "#feea03",
        "string",
        "Rainbow accent.",
    ),
    (
        "widget_accent_blue",
        "#3e8fcd",
        "string",
        "Rainbow accent.",
    ),
    # V5: Just Print's tiger logo as the default widget avatar. Stored so it
    # appears in the dashboard's "Currently in use" preview alongside the live
    # widget (both now pull from the same field).
    (
        "widget_logo_url",
        "https://just-print.ie/wp-content/themes/just-print/assets/img/tiger_760.png",
        "string",
        "Public URL to the widget avatar / header logo.",
    ),
    # V5: dynamic-length accents + stripe render mode. JSON array of hex strings.
    # The legacy accent_pink/yellow/blue rows are still seeded above for
    # backwards compat; /widget-config falls back to them if this is unset.
    (
        "widget_accents",
        '["#e30686", "#feea03", "#3e8fcd", "#040f2a"]',
        "json",
        "Ordered list of hex colors used by the widget rainbow stripe. Any length.",
    ),
    (
        "widget_stripe_mode",
        "sections",
        "string",
        "How the stripe is rendered: 'sections' (solid bands), 'gradient' (smooth blend), or 'solid' (single color).",
    ),
]


def seed(org_slug: str = DEFAULT_ORG_SLUG) -> None:
    print(f"Seeding V4 settings for '{org_slug}'...")
    created = 0
    with db_session() as db:
        for key, value, value_type, description in SEED_SETTINGS:
            existing = (
                db.query(Setting)
                .filter_by(organization_slug=org_slug, key=key)
                .first()
            )
            if existing:
                print(f"  \u00b7 {key} already exists (skipped)")
                continue
            db.add(Setting(
                organization_slug=org_slug,
                key=key,
                value=value,
                value_type=value_type,
                description=description,
            ))
            created += 1
            print(f"  + {key}")
    print()
    if created == 0:
        print("\u2713 Nothing to seed.")
    else:
        print(f"\u2713 {created} settings seeded.")


if __name__ == "__main__":
    init_db()
    seed()
