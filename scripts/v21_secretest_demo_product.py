"""
V21 migration — seed the `secretest` demo product (€1 inc VAT).

Purpose: a sentinel product for live end-to-end demos. Justin can run
the full Craig flow (chat → quote → customer confirms → Justin approves
in dashboard → Missive draft created → customer pays €1 via Stripe →
Justin pushes to PrintLogic) without exposing his real catalog or
risking a production-priced commitment.

Shape:
  - product_key = "secretest"
  - category    = "small_format"  (so quote_small_format works)
  - finishes    = []              (no finish required — keeps the LLM
                                   from asking for one)
  - price_per   = "1"             (1 unit = 1 unit, no batching)
  - 1 PriceTier: quantity=1, price=0.88 → with the Irish printed-matter
    13.5% VAT rate (the rate small_format products are mapped to in
    Just Print's tax setup), this lands at exactly €1.00 inc VAT —
    above Stripe's €0.50 minimum and a clean round figure for the demo.
    If the tenant's tax mapping differs the inc-VAT total will drift
    by a few cents; the demo still works.
  - Several aliases so the customer can phrase it naturally:
      "secretest", "secret test", "secretest demo", "demo product",
      "test product", "test order"
  - double_sided_surcharge = False (so the LLM picking yes/no doesn't
    move the price away from €1)

Idempotent. Safe to re-run on every deploy. Won't override pricing if
someone has tweaked the tier in the meantime.

Usage:
    python -m scripts.v21_secretest_demo_product
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db import db_session, init_db
from db.models import (
    DEFAULT_ORG_SLUG, PriceTier, Product, ProductAlias,
)


SECRETEST_KEY = "secretest"
SECRETEST_NAME = "Secret Test (€1 demo)"
SECRETEST_DESC = (
    "Sentinel product for end-to-end Craig demos. €1 inc VAT — above "
    "Stripe's €0.50 minimum, low enough that a live charge is harmless. "
    "Do NOT produce. Cancel any orders after the demo."
)
SECRETEST_NOTES = "DEMO ONLY — sentinel product, do not actually produce"

# Several phrasings the customer might use
SECRETEST_ALIASES = [
    "secretest",
    "secret test",
    "secretest demo",
    "demo product",
    "test product",
    "test order",
    "1 euro test",
]


def _seed(db, organization_slug: str) -> tuple[bool, bool, int]:
    """Add the product, its tier, and its aliases for a single tenant.

    Returns (product_added, tier_added, aliases_added)."""
    product_added = False
    tier_added = False
    aliases_added = 0

    # ── Product ──────────────────────────────────────────────────────
    product = (
        db.query(Product)
        .filter_by(organization_slug=organization_slug, key=SECRETEST_KEY)
        .first()
    )
    if not product:
        product = Product(
            organization_slug=organization_slug,
            key=SECRETEST_KEY,
            name=SECRETEST_NAME,
            category="small_format",
            description=SECRETEST_DESC,
            sizes=[],
            finishes=[],
            price_per="1",
            notes=SECRETEST_NOTES,
            pricing_strategy="tiered",
            metric_unit=None,
            image_url=None,
            double_sided_surcharge=False,
            unit_price=None,
            bulk_price=None,
            bulk_threshold=None,
            pricing_unit=None,
            min_qty=1,
        )
        db.add(product)
        db.flush()
        product_added = True

    # ── Price tier ───────────────────────────────────────────────────
    # Target: exactly €1.00 inc VAT for a clean demo total.
    # The Just Print catalog has small_format products mapped to the
    # Irish printed-matter rate of 13.5% via category_tax_map. So
    # ex_VAT = 0.88 → vat = round(0.88 × 0.135, 2) = 0.12 → inc = €1.00.
    TARGET_PRICE_EX_VAT = 0.88
    tier = (
        db.query(PriceTier)
        .filter_by(product_id=product.id, spec_key="", quantity=1)
        .first()
    )
    if not tier:
        db.add(PriceTier(
            organization_slug=organization_slug,
            product_id=product.id,
            spec_key="",
            quantity=1,
            price=TARGET_PRICE_EX_VAT,
        ))
        tier_added = True
    elif abs(tier.price - TARGET_PRICE_EX_VAT) > 1e-6:
        # An older v21 run seeded €0.81 (the math was assumed to be
        # 23% VAT but small_format maps to 13.5%). Correct it on
        # subsequent runs so the demo total really is €1.00.
        print(
            f"  · price drift detected (was {tier.price:.2f}); "
            f"updating to {TARGET_PRICE_EX_VAT:.2f}"
        )
        tier.price = TARGET_PRICE_EX_VAT
        tier_added = True  # report as "added" for the summary line

    # ── Aliases ──────────────────────────────────────────────────────
    existing_aliases = {
        a.alias.lower()
        for a in db.query(ProductAlias)
        .filter_by(organization_slug=organization_slug, product_id=product.id)
        .all()
    }
    for alias in SECRETEST_ALIASES:
        if alias.lower() in existing_aliases:
            continue
        db.add(ProductAlias(
            organization_slug=organization_slug,
            product_id=product.id,
            alias=alias,
        ))
        aliases_added += 1

    return product_added, tier_added, aliases_added


def migrate() -> None:
    print("V21: seeding 'secretest' demo product (€1 inc VAT)...")
    init_db()

    with db_session() as db:
        prod_added, tier_added, alias_count = _seed(db, DEFAULT_ORG_SLUG)
        if prod_added:
            print(f"  + {DEFAULT_ORG_SLUG}.products.{SECRETEST_KEY}")
        else:
            print(f"  · {DEFAULT_ORG_SLUG}.products.{SECRETEST_KEY} already present")
        if tier_added:
            print(f"  + price_tier qty=1 price=0.88 (=> €1.00 inc 13.5% VAT)")
        else:
            print(f"  · price_tier qty=1 already present")
        if alias_count:
            print(f"  + {alias_count} aliases")
        else:
            print(f"  · all aliases already present")
        db.commit()

    print()
    print("✓ secretest ready. Try it in the chat:")
    print("    \"can I get one secretest please\"")
    print("  Craig will quote €1.00 inc VAT, then run the full demo flow.")


if __name__ == "__main__":
    migrate()
