"""
One-shot tenant provisioner — seeds the `demo` tenant from scratch.

Why this exists: there's no dashboard "Create Tenant" UI yet. Spinning up
a sandbox tenant for sales demos / new client onboarding is a 5-step
manual ritual today. This script automates it for the canonical `demo`
slug, and serves as the template for any future tenant
(`tenant_slug = "acme-print"`, etc. — copy the file, change one constant).

What it does, in order:

  1. Create a `system_prompt` Setting for `demo`. This is the trigger
     every other v* seed uses to recognise a tenant exists ("which slugs
     have a system_prompt? loop over those").
  2. Run the existing v4..v16 seeds — all idempotent, all enumerate
     tenants by `system_prompt` rows. They populate widget_config,
     business_rules, missive_*, printlogic_*, stripe_*,
     pricing_client_multiplier with safe defaults (everything disabled,
     every secret empty).
  3. Run `migrate_json_to_db --org-slug demo` to copy Just Print's catalog
     into the demo tenant. (For the `demo` tenant we WANT the same
     catalog; for a real new client, you'd skip this step or replace it
     with that client's own JSON.)

Idempotent — re-running clears the demo tenant's pricing data and
re-seeds it, but leaves Settings / Conversations / Quotes alone.

Usage:
    python -m scripts.seed_demo_tenant
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db import db_session, init_db
from db.models import Setting


DEMO_SLUG = "demo"
DEMO_SYSTEM_PROMPT = (
    "You are Craig, a sandbox version of Just Print's AI quoting agent. "
    "This is the demo tenant — quotes here are NOT real and NOT pushed to "
    "any production system. Use the same casual, helpful Irish-market "
    "tone, but feel free to make up product names if needed for "
    "demonstrations. Always remind the customer this is a demo."
)


def _ensure_system_prompt(slug: str, prompt: str) -> bool:
    """Insert a system_prompt for `slug` if missing. Returns True if it
    was newly created, False if it already existed."""
    with db_session() as db:
        existing = (
            db.query(Setting)
            .filter_by(organization_slug=slug, key="system_prompt")
            .first()
        )
        if existing:
            return False
        db.add(Setting(
            organization_slug=slug,
            key="system_prompt",
            value=prompt,
            value_type="string",
            description="System prompt for the demo tenant.",
        ))
        return True


def main() -> None:
    print(f"=== Seeding demo tenant: {DEMO_SLUG!r} ===\n")

    init_db()

    # ── 1. system_prompt ────────────────────────────────────────────────
    print(f"[1/3] Ensuring system_prompt for {DEMO_SLUG!r}...")
    if _ensure_system_prompt(DEMO_SLUG, DEMO_SYSTEM_PROMPT):
        print(f"  + created.\n")
    else:
        print(f"  · already present.\n")

    # ── 2. tenant-scoped seeds ──────────────────────────────────────────
    # All v4..v16 seeds enumerate tenants by `system_prompt` and skip rows
    # that already exist. Importing inside main() so a failure in one seed
    # doesn't crash the import line of the others.
    print(f"[2/3] Running tenant-scoped seeds (v4..v16)...")

    seeds = []
    try:
        from scripts.v4_system_prompt_seed import seed as v4
        seeds.append(("v4 system_prompt + widget_config", v4))
    except ImportError:
        pass
    try:
        from scripts.v6_default_business_rules import seed as v6
        seeds.append(("v6 business rules", v6))
    except ImportError:
        pass
    try:
        from scripts.v9_missive_settings_seed import seed as v9
        seeds.append(("v9 missive settings", v9))
    except ImportError:
        pass
    try:
        from scripts.v13_printlogic_settings_seed import seed as v13
        seeds.append(("v13 printlogic settings (dry_run=true)", v13))
    except ImportError:
        pass
    try:
        from scripts.v14_client_multiplier_seed import seed as v14
        seeds.append(("v14 client multiplier (1.0)", v14))
    except ImportError:
        pass
    try:
        from scripts.v16_stripe_settings_seed import seed as v16
        seeds.append(("v16 stripe settings (disabled)", v16))
    except ImportError:
        pass

    for label, fn in seeds:
        print(f"  > {label}...")
        fn()
        print()

    # ── 3. catalog ──────────────────────────────────────────────────────
    print(f"[3/3] Copying pricing catalog into {DEMO_SLUG!r}...")
    from scripts.migrate_json_to_db import migrate
    migrate(organization_slug=DEMO_SLUG)

    print()
    print(f"=== Done. Tenant {DEMO_SLUG!r} provisioned. ===")
    print()
    print("Next steps:")
    print(f"  - Widget URL:   https://<your-cloud-run>/widget.js?client={DEMO_SLUG}")
    print(f"  - Widget test:  https://<your-cloud-run>/?client={DEMO_SLUG}")
    print(f"  - Dashboard:    /c/{DEMO_SLUG}/a/craig")
    print()
    print("All integrations (Missive, PrintLogic, Stripe) start DISABLED for safety.")
    print("Flip them in Connections → <integration> when you're ready.")


if __name__ == "__main__":
    main()
