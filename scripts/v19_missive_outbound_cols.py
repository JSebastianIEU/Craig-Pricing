"""
V19 migration — add Missive outbound-draft columns to `quotes`.

Phase C wires confirm_order on the web widget to also create a Missive
draft to the customer (with PDF + payment link). Three new columns
mirror the v15 Stripe / v12 PrintLogic pattern:

  - missive_draft_id      VARCHAR(128) — Missive's draft id (idempotency guard)
  - missive_drafted_at    TIMESTAMP    — when the draft was created
  - missive_last_error    TEXT         — failure mode if create_draft errored

Plus the auto-draft setting v19 also seeds:

  - missive_auto_draft_enabled = 'true'  (per tenant; default ON)

Idempotent. Safe on SQLite (dev) and Postgres (prod). Re-running adds
nothing for columns/settings that already exist.

Usage:
    python -m scripts.v19_missive_outbound_cols
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import inspect, text

from db import db_session, engine, init_db
from db.models import DEFAULT_ORG_SLUG, Setting


_COLUMN_DEFS = [
    ("missive_draft_id",     "VARCHAR(128) NULL"),
    ("missive_drafted_at",   "TIMESTAMP NULL"),
    ("missive_last_error",   "TEXT NULL"),
]

_INDEXES = [
    ("ix_quotes_missive_draft_id", "missive_draft_id"),
]


def _column_exists(conn, table: str, column: str) -> bool:
    return column in [c["name"] for c in inspect(conn).get_columns(table)]


def _add_column_if_missing(conn, table: str, column_name: str, column_def: str) -> bool:
    if _column_exists(conn, table, column_name):
        return False
    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column_name} {column_def}"))
    return True


def _seed_setting_if_missing(db, key: str, value: str) -> bool:
    existing = (
        db.query(Setting)
        .filter_by(organization_slug=DEFAULT_ORG_SLUG, key=key)
        .first()
    )
    if existing:
        return False
    db.add(Setting(
        organization_slug=DEFAULT_ORG_SLUG,
        key=key, value=value, value_type="string",
    ))
    return True


def migrate() -> None:
    print("V19: adding Missive outbound-draft columns to `quotes`...")
    init_db()

    added = 0
    with engine.begin() as conn:
        for name, defn in _COLUMN_DEFS:
            if _add_column_if_missing(conn, "quotes", name, defn):
                print(f"  + quotes.{name}")
                added += 1
            else:
                print(f"  · quotes.{name} already present")

        existing_idx = [i["name"] for i in inspect(conn).get_indexes("quotes")]
        for idx_name, col in _INDEXES:
            if idx_name in existing_idx:
                continue
            try:
                conn.execute(text(f"CREATE INDEX {idx_name} ON quotes ({col})"))
                print(f"  + index {idx_name}")
            except Exception as e:
                print(f"  ⚠ could not create {idx_name}: {e}")

    # Seed the auto-draft toggle (default ON for the default tenant).
    seeded = 0
    with db_session() as db:
        if _seed_setting_if_missing(db, "missive_auto_draft_enabled", "true"):
            seeded += 1
            print("  + setting just-print.missive_auto_draft_enabled = 'true'")
        else:
            print("  · setting missive_auto_draft_enabled already present")

    print()
    print(f"✓ {added} columns added, {seeded} settings seeded.")


if __name__ == "__main__":
    migrate()
