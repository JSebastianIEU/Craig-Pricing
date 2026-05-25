"""
V39 migration — minimum billable area for per-sq/m products.

Justin's ask: vinyl labels (and similar per-m² products) under 1 m²
should be billed as a full square metre — there's a real physical
minimum cut / sheet usage, so a 0.2 m² order shouldn't price at a
fifth of a square metre.

This migration adds the CAPABILITY only — `Product.min_billable_sqm`
(Float, nullable). It deliberately does NOT seed any product value:
`min_billable_sqm` changes prices, so which products get a floor (and
what value) is Justin's per-product business decision, made in the
dashboard — same as unit_price / bulk_price. Seeding a value here
would also run against the CI test DB and break the existing per-sq/m
math tests (e.g. the Ian-Byrne sub-1m² vinyl regression).

The engine side (pricing_engine._quote_per_sqm) clamps total_m2 up to
this floor before bulk-price selection. No-op for products without a
floor (the default for every existing product).

Idempotent. Safe to re-run.

Usage:
    python -m scripts.v39_min_billable_area
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import inspect, text

from db import engine


# ---------------------------------------------------------------------------
# Schema — one new Product column
# ---------------------------------------------------------------------------

_COLUMN_DEFS = [
    ("products", "min_billable_sqm", "FLOAT NULL"),
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
    """V39 DDL only — adds Product.min_billable_sqm. Runs early in
    startup so the ORM's SELECT * doesn't trip on the missing column."""
    print("V39 DDL: adding min_billable_sqm...")
    with engine.begin() as conn:
        for table, name, defn in _COLUMN_DEFS:
            if _add_column_if_missing(conn, table, name, defn):
                print(f"  + {table}.{name}")
            else:
                print(f"  - {table}.{name} already present")


def migrate() -> None:
    print("V39: minimum billable area (per-sq/m floor) — capability only...")
    migrate_ddl_only()
    print()
    print("v39 migration complete (column added; set values in dashboard).")


if __name__ == "__main__":
    migrate()
