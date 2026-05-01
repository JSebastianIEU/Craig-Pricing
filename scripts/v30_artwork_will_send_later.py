"""
V30 migration — add `Conversation.artwork_will_send_later` Boolean.

Justin reported (May 2026 smoke): customer says "I don't have my
artwork finalised yet" / "I just need a price" — Craig keeps replying
with the upload prompt instead of giving the price. Loop with no exit.

Root cause: the sniffer was matching "i'll send" / "i'll provide" as
"customer has artwork ready", but those phrases actually mean "later".
The pricing-tool guard then unblocked, the quote was created with no
files, the upload-first gate replaced the verbal price with the
upload prompt, and the customer had no files to send → loop.

This migration adds a separate Boolean so we can distinguish three
intents:
  - customer_has_own_artwork=True  AND artwork_will_send_later=False
        → customer has artwork right now, push the upload card
  - customer_has_own_artwork=True  AND artwork_will_send_later=True
        → customer is committed but will send the files later;
          give the price, no upload pressure
  - customer_has_own_artwork=False                                 (any value)
        → customer wants the €65/hr design service

Idempotent. Safe to re-run.

Usage:
    python -m scripts.v30_artwork_will_send_later
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import inspect, text

from db import engine


_COLUMN_DEFS = [
    # SQLite vs Postgres tolerate slightly different DDL — we use the
    # most-portable form: NOT NULL with a default 0, no `BOOLEAN` keyword
    # (Postgres maps INTEGER 0/1 to bool via SQLAlchemy on read).
    ("conversations", "artwork_will_send_later", "BOOLEAN NOT NULL DEFAULT FALSE"),
]


def _column_exists(conn, table: str, column: str) -> bool:
    return column in [c["name"] for c in inspect(conn).get_columns(table)]


def _add_column_if_missing(conn, table: str, name: str, defn: str) -> bool:
    if _column_exists(conn, table, name):
        return False
    # SQLite doesn't accept `BOOLEAN NOT NULL DEFAULT FALSE` literally —
    # it'll happily store boolean-as-int but the FALSE keyword needs to
    # become 0. SQLAlchemy's create_engine drivername tells us which
    # dialect we're on.
    if engine.url.drivername.startswith("sqlite"):
        defn = defn.replace("FALSE", "0").replace("TRUE", "1")
    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {defn}"))
    return True


def migrate() -> None:
    print("V30: artwork_will_send_later flag on conversations...")
    added = 0
    with engine.begin() as conn:
        for table, name, defn in _COLUMN_DEFS:
            if _add_column_if_missing(conn, table, name, defn):
                print(f"  + {table}.{name}")
                added += 1
            else:
                print(f"  · {table}.{name} already present")
    if not added:
        print("  · no schema changes needed (already up to date)")


if __name__ == "__main__":
    migrate()
