"""
V11 migration: add 'silk' to the finishes list of flyers.

Justin's note from the Apr 23 test conversations:
  "Notes for Roi & Sebastian - can we just offer 170gsm silk for the leaflets
   & other stocks available on request"

Conv 30 also showed a silent bug where Craig quoted 500 A5 silk as *matte*
because silk wasn't in the catalog's valid finishes list. Adding silk here
makes Craig quote silk correctly instead of substituting.

Silk is same price as matte per Justin's "same price across finishes" policy
for flyers — no pricing changes, just the finishes array.

Idempotent: products already listing silk are left alone.

v34 fix — uses raw SQL with explicit columns to avoid the ORM loading
manual_review_required (and other v34 columns) that don't exist yet at
this point in the migration chain. Same pattern as v25/v26.

Usage:
    python -m scripts.v11_silk_finish
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text

from db import engine, init_db


# Only the 4 flyer SKUs. Brochures / compliment slips / letterheads / NCR are
# not affected per Justin's note — "for the leaflets & other stocks available
# on request" (i.e. other stocks = escalate, not add as catalog).
FLYER_KEYS = ["flyers_a6", "flyers_a5", "flyers_a4", "flyers_dl"]


def migrate() -> None:
    print("V11: adding 'silk' finish to flyers...")
    updated = 0
    with engine.begin() as conn:
        rows: list = []
        for k in FLYER_KEYS:
            r = conn.execute(
                text(
                    "SELECT id, organization_slug, key, finishes "
                    "FROM products WHERE key = :k"
                ),
                {"k": k},
            ).fetchall()
            rows.extend(r)

        for pid, org_slug, key, finishes_raw in rows:
            # finishes is JSON; pg returns list, sqlite returns str
            if isinstance(finishes_raw, str):
                try:
                    current = json.loads(finishes_raw) or []
                except json.JSONDecodeError:
                    current = []
            elif isinstance(finishes_raw, list):
                current = list(finishes_raw)
            else:
                current = []

            if "silk" in current:
                print(f"  - {org_slug}/{key} already has silk")
                continue

            new_finishes = current + ["silk"]
            conn.execute(
                text("UPDATE products SET finishes = :f WHERE id = :id"),
                {"f": json.dumps(new_finishes, ensure_ascii=False), "id": pid},
            )
            updated += 1
            print(f"  + {org_slug}/{key}: finishes -> {new_finishes}")

    print()
    print(f"  {updated} products updated.")


if __name__ == "__main__":
    init_db()
    migrate()
