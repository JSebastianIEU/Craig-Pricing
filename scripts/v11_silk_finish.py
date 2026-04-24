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

Usage:
    python -m scripts.v11_silk_finish
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db import db_session, init_db
from db.models import Product


# Only the 4 flyer SKUs. Brochures / compliment slips / letterheads / NCR are
# not affected per Justin's note — "for the leaflets & other stocks available
# on request" (i.e. other stocks = escalate, not add as catalog).
FLYER_KEYS = ["flyers_a6", "flyers_a5", "flyers_a4", "flyers_dl"]


def migrate() -> None:
    print("V11: adding 'silk' finish to flyers...")
    updated = 0
    with db_session() as db:
        products = (
            db.query(Product)
            .filter(Product.key.in_(FLYER_KEYS))
            .all()
        )
        for p in products:
            current = list(p.finishes or [])
            if "silk" in current:
                print(f"  \u00b7 {p.organization_slug}/{p.key} already has silk")
                continue
            p.finishes = current + ["silk"]
            updated += 1
            print(f"  + {p.organization_slug}/{p.key}: finishes \u2192 {p.finishes}")

    print()
    print(f"\u2713 {updated} products updated.")


if __name__ == "__main__":
    init_db()
    migrate()
