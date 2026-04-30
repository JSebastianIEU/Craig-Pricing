"""
V20 migration — add `client_confirmed_at` to `quotes`.

Phase D restructures the approval flow so Justin's dashboard "Approve"
action is the sole trigger for outbound integrations (Stripe Payment
Link + Missive draft). The LLM's `confirm_order` tool is now passive —
it records the customer-side acceptance signal here so Justin can see
in his queue which quotes are "ready for me to act on" vs "lead just
quoted, no commitment yet".

  - client_confirmed_at  TIMESTAMP NULL — when confirm_order fired

Idempotent. Safe on SQLite (dev) and Postgres (prod).

Usage:
    python -m scripts.v20_client_confirmed_at
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import inspect, text

from db import engine, init_db


_COLUMN_DEFS = [
    ("client_confirmed_at", "TIMESTAMP NULL"),
]


def _column_exists(conn, table: str, column: str) -> bool:
    return column in [c["name"] for c in inspect(conn).get_columns(table)]


def _add_column_if_missing(conn, table: str, column_name: str, column_def: str) -> bool:
    if _column_exists(conn, table, column_name):
        return False
    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column_name} {column_def}"))
    return True


def migrate() -> None:
    print("V20: adding client_confirmed_at to `quotes`...")
    init_db()

    added = 0
    with engine.begin() as conn:
        for name, defn in _COLUMN_DEFS:
            if _add_column_if_missing(conn, "quotes", name, defn):
                print(f"  + quotes.{name}")
                added += 1
            else:
                print(f"  · quotes.{name} already present")

    print()
    print(f"✓ {added} columns added.")


if __name__ == "__main__":
    migrate()
