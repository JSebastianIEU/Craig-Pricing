"""
V40 migration — marketing attribution column on conversations.

Adds `Conversation.attribution` (JSONB on Postgres / TEXT on SQLite),
a nullable JSON blob holding the UTM params + ad click IDs the widget
captures from the landing-page URL:

  {
    "first_touch": { utm_source, ..., gclid, fbclid, ... },  # write-once
    "last_touch":  { ...same keys... },                       # latest
    "merged_from": ["<external_id stitched by identity>"]
  }

DDL only — no seeding. The merge logic lives in attribution.py and runs
on every /chat + customer-info call. JSON column so new ad platforms
never need an ALTER TABLE.

Idempotent. Safe to re-run.

Usage:
    python -m scripts.v40_attribution
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import inspect, text

from db import engine


# ---------------------------------------------------------------------------
# Schema — one new nullable JSON column on conversations
# ---------------------------------------------------------------------------

_COLUMN_DEFS = [
    ("conversations", "attribution", "JSONB NULL"),
]


def _is_postgres() -> bool:
    return engine.url.drivername.startswith("postgresql")


def _column_exists(conn, table: str, column: str) -> bool:
    return column in [c["name"] for c in inspect(conn).get_columns(table)]


def _add_column_if_missing(conn, table: str, name: str, defn: str) -> bool:
    if _column_exists(conn, table, name):
        return False
    if not _is_postgres():
        # On SQLite the JSON type is stored as TEXT.
        defn = (
            defn.replace("JSONB", "TEXT")
                .replace("TIMESTAMP", "DATETIME")
                .replace("DEFAULT FALSE", "DEFAULT 0")
                .replace("DEFAULT TRUE", "DEFAULT 1")
        )
    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {defn}"))
    return True


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------


def migrate_ddl_only() -> None:
    """V40 DDL only — adds Conversation.attribution. Runs early in
    startup so the ORM's SELECT * doesn't trip on the missing column."""
    print("V40 DDL: adding conversations.attribution...")
    with engine.begin() as conn:
        for table, name, defn in _COLUMN_DEFS:
            if _add_column_if_missing(conn, table, name, defn):
                print(f"  + {table}.{name}")
            else:
                print(f"  - {table}.{name} already present")


def migrate() -> None:
    print("V40: marketing attribution column...")
    migrate_ddl_only()
    print()
    print("v40 migration complete.")


if __name__ == "__main__":
    migrate()
