"""
V12 migration — add PrintLogic integration columns to `quotes`.

Phase A needs us to track, per Quote, whether it has been pushed to
PrintLogic and (if so) under what id. Five new nullable columns:

  - printlogic_order_id        VARCHAR(64) — real id or synthetic 'DRY-xxxxxxxx'
  - printlogic_customer_id     VARCHAR(64)
  - printlogic_pushed_at       TIMESTAMP
  - printlogic_last_error      TEXT
  - printlogic_push_attempts   INTEGER NOT NULL DEFAULT 0

Idempotent — re-running is a no-op for columns that already exist.
Matches the "column_exists + add_column_if_missing" pattern from
scripts/v2_multitenancy_pricing.py so we work on both SQLite (local dev)
and Postgres (prod on Cloud SQL).

Usage:
    python -m scripts.v12_printlogic_cols
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import inspect, text

from db import engine, init_db


_COLUMN_DEFS = [
    ("printlogic_order_id",      "VARCHAR(64) NULL"),
    ("printlogic_customer_id",   "VARCHAR(64) NULL"),
    ("printlogic_pushed_at",     "TIMESTAMP NULL"),
    ("printlogic_last_error",    "TEXT NULL"),
    ("printlogic_push_attempts", "INTEGER NOT NULL DEFAULT 0"),
]


def _column_exists(conn, table: str, column: str) -> bool:
    return column in [c["name"] for c in inspect(conn).get_columns(table)]


def _add_column_if_missing(conn, table: str, column_name: str, column_def: str) -> bool:
    if _column_exists(conn, table, column_name):
        return False
    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column_name} {column_def}"))
    return True


def migrate() -> None:
    print("V12: adding PrintLogic columns to `quotes`...")
    # init_db() creates any missing tables with the CURRENT model definition
    # (which already includes these columns), so for fresh DBs the columns
    # are already there. For existing DBs we ALTER TABLE.
    init_db()

    added = 0
    with engine.begin() as conn:
        for name, defn in _COLUMN_DEFS:
            if _add_column_if_missing(conn, "quotes", name, defn):
                print(f"  + quotes.{name}")
                added += 1
            else:
                print(f"  \u00b7 quotes.{name} already present")

        # Index on printlogic_order_id for fast idempotency checks + joins
        # (only where not null). The name matches the one declared on the
        # model, so SQLAlchemy's create_all won't fight us.
        try:
            idx_name = "ix_quotes_printlogic_order_id"
            existing_idx = [i["name"] for i in inspect(conn).get_indexes("quotes")]
            if idx_name not in existing_idx:
                conn.execute(text(f"CREATE INDEX {idx_name} ON quotes (printlogic_order_id)"))
                print(f"  + index {idx_name}")
        except Exception as e:  # non-fatal — SQLAlchemy's create_all usually handles it
            print(f"  \u26a0 could not create index (probably exists): {e}")

    print()
    print(f"\u2713 {added} columns added.")


if __name__ == "__main__":
    migrate()
