"""
V15 migration — add Stripe payment columns to `quotes`.

Phase B brings Stripe payment links. Six new nullable columns mirror the
PrintLogic pattern from V12:

  - stripe_payment_link_id         VARCHAR(128) — Stripe's `plink_...` id
  - stripe_payment_link_url        TEXT         — public checkout URL we send to customer
  - stripe_checkout_session_id     VARCHAR(128) — populated by webhook when session completes
  - stripe_payment_status          VARCHAR(32)  — unpaid / paid / refunded / failed
  - stripe_paid_at                 TIMESTAMP
  - stripe_last_error              TEXT

Idempotent. Safe on SQLite (dev) and Postgres (prod). Re-running adds nothing
for columns that already exist.

Usage:
    python -m scripts.v15_stripe_cols
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import inspect, text

from db import engine, init_db


_COLUMN_DEFS = [
    ("stripe_payment_link_id",     "VARCHAR(128) NULL"),
    ("stripe_payment_link_url",    "TEXT NULL"),
    ("stripe_checkout_session_id", "VARCHAR(128) NULL"),
    ("stripe_payment_status",      "VARCHAR(32) NULL"),
    ("stripe_paid_at",             "TIMESTAMP NULL"),
    ("stripe_last_error",          "TEXT NULL"),
]

_INDEXES = [
    ("ix_quotes_stripe_payment_link_id",     "stripe_payment_link_id"),
    ("ix_quotes_stripe_checkout_session_id", "stripe_checkout_session_id"),
]


def _column_exists(conn, table: str, column: str) -> bool:
    return column in [c["name"] for c in inspect(conn).get_columns(table)]


def _add_column_if_missing(conn, table: str, column_name: str, column_def: str) -> bool:
    if _column_exists(conn, table, column_name):
        return False
    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column_name} {column_def}"))
    return True


def migrate() -> None:
    print("V15: adding Stripe columns to `quotes`...")
    init_db()

    added = 0
    with engine.begin() as conn:
        for name, defn in _COLUMN_DEFS:
            if _add_column_if_missing(conn, "quotes", name, defn):
                print(f"  + quotes.{name}")
                added += 1
            else:
                print(f"  \u00b7 quotes.{name} already present")

        existing_idx = [i["name"] for i in inspect(conn).get_indexes("quotes")]
        for idx_name, col in _INDEXES:
            if idx_name in existing_idx:
                continue
            try:
                conn.execute(text(f"CREATE INDEX {idx_name} ON quotes ({col})"))
                print(f"  + index {idx_name}")
            except Exception as e:
                print(f"  \u26a0 could not create {idx_name}: {e}")

    print()
    print(f"\u2713 {added} columns added.")


if __name__ == "__main__":
    migrate()
