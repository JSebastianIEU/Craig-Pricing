"""
Cloud Run startup script.

Runs on every container boot. Connects to whatever `CRAIG_DATABASE_URL`
points at (Postgres on Cloud SQL in production, SQLite locally) and makes
sure the schema + seed data are in the right state. Idempotent.

Order:
  1. init_db()                         — create any missing tables
  2. migrate_json_to_db                — ONLY if the products table is empty
                                         (first-time bootstrap; wiping on
                                         every restart would nuke live data)
  3. v2 multi-tenancy + tax rates      — idempotent
  4. v3 categories + images            — idempotent
  5. v4 system prompt + widget config  — idempotent
  6. v5 strip legacy catalog           — idempotent

Exits non-zero on any failure so Cloud Run flags the revision as broken
before serving traffic.
"""

from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _run(label: str, fn) -> None:
    print(f"[startup] {label}...", flush=True)
    fn()
    print(f"[startup] {label} \u2713", flush=True)


def main() -> None:
    from db import db_session, init_db
    from db.models import Product

    _run("init_db (create tables)", init_db)

    # Only bootstrap pricing data if the DB is empty — otherwise this wipes
    # everything the user has edited since first deploy (system_prompt,
    # business_rules, catalog edits, etc.).
    with db_session() as db:
        has_products = db.query(Product).first() is not None

    if not has_products:
        from scripts.migrate_json_to_db import migrate as migrate_json

        _run("migrate_json_to_db (first bootstrap)", migrate_json)
    else:
        print("[startup] migrate_json_to_db: skipped (products already exist)", flush=True)

    from scripts.v2_multitenancy_pricing import migrate as v2_migrate
    from scripts.v3_categories_images import migrate as v3_migrate
    from scripts.v4_system_prompt_seed import seed as v4_seed
    from scripts.v5_strip_legacy_catalog import migrate as v5_migrate
    from scripts.v6_default_business_rules import seed as v6_seed
    from scripts.v7_patch_contact_contradiction import migrate as v7_migrate
    from scripts.v8_refresh_default_rules import migrate as v8_migrate
    from scripts.v9_missive_settings_seed import seed as v9_seed
    from scripts.v10_soft_touch_flat_fee import migrate as v10_migrate
    from scripts.v11_silk_finish import migrate as v11_migrate
    from scripts.v12_printlogic_cols import migrate as v12_migrate
    from scripts.v13_printlogic_settings_seed import seed as v13_seed
    from scripts.v14_client_multiplier_seed import seed as v14_seed
    from scripts.v15_stripe_cols import migrate as v15_migrate
    from scripts.v16_stripe_settings_seed import seed as v16_seed
    from scripts.v17_encrypt_existing_secrets import migrate as v17_migrate
    from scripts.v18_stripe_connect_migration import migrate as v18_migrate
    from scripts.v19_missive_outbound_cols import migrate as v19_migrate
    from scripts.v20_client_confirmed_at import migrate as v20_migrate
    from scripts.v21_secretest_demo_product import migrate as v21_migrate

    _run("v2 multi-tenancy", v2_migrate)
    _run("v3 categories + images", v3_migrate)
    _run("v4 system prompt + widget config", v4_seed)
    _run("v5 strip legacy catalog", v5_migrate)
    _run("v6 default business rules", v6_seed)
    _run("v7 patch contact contradiction", v7_migrate)
    _run("v8 refresh default rules", v8_migrate)
    _run("v9 missive settings seed", v9_seed)
    _run("v10 soft-touch flat fee", v10_migrate)
    _run("v11 silk finish for flyers", v11_migrate)
    _run("v12 printlogic columns on quotes", v12_migrate)
    _run("v13 printlogic settings seed", v13_seed)
    _run("v14 client multiplier seed", v14_seed)
    _run("v15 stripe columns on quotes", v15_migrate)
    _run("v16 stripe settings seed", v16_seed)
    _run("v17 encrypt existing secrets (soft-skips if no key)", v17_migrate)
    _run("v18 remove legacy stripe paste-flow keys", v18_migrate)
    _run("v19 missive outbound draft columns + setting", v19_migrate)
    _run("v20 client_confirmed_at on quotes", v20_migrate)
    _run("v21 secretest demo product seed", v21_migrate)

    print(f"[startup] all migrations complete. DATABASE_URL={os.environ.get('CRAIG_DATABASE_URL', '<default sqlite>')[:40]}...", flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        sys.exit(1)
