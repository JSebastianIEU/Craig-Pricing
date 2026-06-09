"""
V41 migration — per-product minimum order value + max-qty auto-quote
ceiling.

Justin's ask (post v40.4 meeting): every print shop has a "minimum
order value" — for him, €45 on vinyl labels and €25 on the rest of
large_format. Below that, customer pays the minimum regardless of
the engine's tiny computed total. Today the engine just bills the
qty × price even if it comes out as €10 (tiny order).

Similarly, every product has a sensible auto-quote ceiling — above N
units the job is too big for Craig to auto-price and should go to
Justin manually. Leaflets/letterheads above the ceiling escalate.

This migration adds the CAPABILITY only — two nullable Product
columns. Engine and dashboard wiring lives in the same v40.5 PR.
No data seeded: the per-product floor / ceiling are Justin's
business decisions, set in the dashboard once the engine is live.

Columns:
  - `min_order_value_eur`     (Float, nullable). If set and the
    engine's `final_price_ex_vat` after surcharges + client
    multiplier falls below this, the engine FLOORS the total to
    this value and surfaces a "Minimum order €X" note in
    `surcharges_applied` so the LLM can mention it to the customer.
  - `max_qty_for_auto_quote`  (Integer, nullable). If set and the
    LLM tool's `quantity` exceeds this, the engine returns
    `EscalationResult(manual_review=True)` BEFORE pricing math
    runs — Craig auto-creates a `needs_revision` quote and asks
    Justin to manually price.

Idempotent. Safe to re-run. No-op for products that never get values
set (the default for every existing product).

Usage:
    python -m scripts.v41_min_order_and_ceiling
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import inspect, text

from db import engine


# ---------------------------------------------------------------------------
# Schema — two new Product columns
# ---------------------------------------------------------------------------

_COLUMN_DEFS = [
    ("products", "min_order_value_eur", "FLOAT NULL"),
    ("products", "max_qty_for_auto_quote", "INTEGER NULL"),
]


def _is_postgres() -> bool:
    return engine.url.drivername.startswith("postgresql")


def _column_exists(conn, table: str, column: str) -> bool:
    return column in [c["name"] for c in inspect(conn).get_columns(table)]


def _add_column_if_missing(conn, table: str, name: str, defn: str) -> bool:
    if _column_exists(conn, table, name):
        return False
    if not _is_postgres():
        defn = (
            defn.replace("BOOLEAN", "INTEGER")
                .replace("DEFAULT FALSE", "DEFAULT 0")
                .replace("DEFAULT TRUE", "DEFAULT 1")
        )
    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {defn}"))
    return True


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------


def migrate_ddl_only() -> None:
    """V41 DDL only — adds the two Product columns. Runs early in
    startup so the ORM's SELECT * doesn't trip on missing columns."""
    print("V41 DDL: adding min_order_value_eur + max_qty_for_auto_quote...")
    with engine.begin() as conn:
        for table, name, defn in _COLUMN_DEFS:
            if _add_column_if_missing(conn, table, name, defn):
                print(f"  + {table}.{name}")
            else:
                print(f"  - {table}.{name} already present")


def migrate() -> None:
    print(
        "V41: minimum order value + max-qty auto-quote ceiling — "
        "capability only..."
    )
    migrate_ddl_only()
    print()
    print(
        "v41 migration complete (columns added; set per-product values in "
        "dashboard)."
    )


if __name__ == "__main__":
    migrate()
