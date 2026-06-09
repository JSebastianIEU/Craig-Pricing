"""
V42 migration — rename NCR "Pads" → "Books".

Justin (June 2026 meeting): "NCR Pads" is the wrong product name. What
he sells under that key is a BOUND book (perforated + stitched at the
top, 50 sets/book). "Pads" refers to a different tear-off product he
doesn't offer through Craig. Renaming makes Craig accurate when it
talks to customers and matches the language Justin uses.

This migration does the DATA half of the rename — the code half is
done in the same v40.6 PR (pricing_engine, llm/craig_agent, extractor,
pdf_generator, main, data/small_format.json all renamed in lockstep).

Renames:
  - Product.key   'ncr_pads_a5' → 'ncr_books_a5'
  - Product.key   'ncr_pads_a4' → 'ncr_books_a4'
  - Product.name  'NCR Pads — A5' → 'NCR Books — A5' (en-dash + variants)
  - Product.name  'NCR Pads — A4' → 'NCR Books — A4'

Idempotent (REPLACE-only; running it twice is a no-op). Scoped to ALL
organization_slugs by design so multi-tenant catalogs that adopted the
seed get the rename too.

Existing `product_aliases` rows continue to work because they reference
Product.id (foreign key), not the key string. The `extractor.py` alias
table is in code and gets updated in the same PR.

Usage:
    python -m scripts.v42_rename_ncr_pads_to_books
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text

from db import engine


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------


def _is_postgres() -> bool:
    return engine.url.drivername.startswith("postgresql")


def migrate() -> None:
    print("V42: rename NCR Pads → NCR Books (per-product key + name)...")

    # Use REPLACE so the SQL is identical on Postgres + SQLite, AND so
    # running this twice is a no-op (REPLACE on a string that doesn't
    # contain the needle is a pass-through).
    #
    # The LIKE filter guards against touching rows that don't match,
    # making the UPDATE bounded even on huge catalogs.
    with engine.begin() as conn:
        result_keys = conn.execute(text(
            """
            UPDATE products
               SET key = REPLACE(key, 'ncr_pads_', 'ncr_books_')
             WHERE key LIKE 'ncr_pads_%'
            """
        ))
        result_names = conn.execute(text(
            """
            UPDATE products
               SET name = REPLACE(name, 'NCR Pads', 'NCR Books')
             WHERE name LIKE 'NCR Pads%'
            """
        ))
        print(f"  + renamed keys:  {result_keys.rowcount} row(s)")
        print(f"  + renamed names: {result_names.rowcount} row(s)")

    print()
    print("v42 migration complete.")


if __name__ == "__main__":
    migrate()
