"""
Migration script: JSON pricing files -> SQLite database.

Run this once to bootstrap the DB from Justin's spreadsheets.
Idempotent — drops and recreates all pricing data on every run.
(Does NOT touch conversations or quotes.)

Usage:
    python scripts/migrate_json_to_db.py
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db import db_session, init_db
from db.models import (
    Product, PriceTier, ProductAlias, SurchargeRule, Setting,
)

DATA_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data",
)


def _load_json(filename: str) -> dict:
    with open(os.path.join(DATA_DIR, filename), "r") as f:
        return json.load(f)


def migrate():
    print("Creating tables...")
    init_db()

    with db_session() as db:
        # Wipe pricing data (keep conversations/quotes)
        print("Clearing existing pricing data...")
        db.query(PriceTier).delete()
        db.query(ProductAlias).delete()
        db.query(Product).delete()
        db.query(SurchargeRule).delete()
        db.query(Setting).delete()
        db.commit()

        # ---------- SMALL FORMAT ----------
        print("Loading small format products...")
        small_format = _load_json("small_format.json")
        for key, data in small_format.items():
            product = Product(
                key=key,
                name=data["name"],
                category="small_format",
                description=data.get("description", ""),
                sizes=data.get("sizes", []),
                finishes=data.get("finishes", []),
                price_per=data.get("price_per", ""),
                notes=data.get("notes", ""),
                double_sided_surcharge=data.get("double_sided_surcharge", True),
            )
            db.add(product)
            db.flush()  # need product.id before adding tiers

            # Price tiers
            for qty_str, price in data["prices"].items():
                db.add(PriceTier(
                    product_id=product.id,
                    spec_key="",
                    quantity=int(qty_str),
                    price=float(price),
                ))
        print(f"  → {len(small_format)} small format products")

        # ---------- LARGE FORMAT ----------
        print("Loading large format products...")
        large_format = _load_json("large_format.json")
        for key, data in large_format.items():
            product = Product(
                key=key,
                name=data["name"],
                category="large_format",
                description=data.get("description", ""),
                sizes=data.get("sizes", []),
                finishes=[],
                price_per=data.get("pricing_unit", ""),
                notes=data.get("notes", ""),
                unit_price=float(data["unit_price"]),
                bulk_price=float(data["bulk_price"]),
                bulk_threshold=int(data["bulk_threshold"]),
                pricing_unit=data.get("pricing_unit", ""),
                min_qty=int(data.get("min_qty", 1)),
            )
            db.add(product)
        print(f"  → {len(large_format)} large format products")

        # ---------- BOOKLETS ----------
        print("Loading booklets...")
        booklets = _load_json("booklets.json")
        booklet_count = 0
        tier_count = 0
        for fmt in booklets:  # 'a5' | 'a4'
            for binding in booklets[fmt]:  # 'saddle_stitch' | 'perfect_bound'
                # Create one product per format+binding combo
                product_key = f"booklet_{fmt}_{binding}"
                product = Product(
                    key=product_key,
                    name=f"Booklet {fmt.upper()} — {binding.replace('_', ' ').title()}",
                    category="booklet",
                    description=f"{fmt.upper()} {binding.replace('_', ' ')} booklet",
                    sizes=[fmt.upper()],
                    finishes=list({
                        cover for pages_data in booklets[fmt][binding].values()
                        for cover in pages_data.keys()
                    }),
                    price_per="per job",
                    notes="Saddle stitch up to 48pp. Perfect bound from 24pp.",
                )
                db.add(product)
                db.flush()
                booklet_count += 1

                # Price tiers: spec_key = "pages|cover_type"
                for pages_str, covers in booklets[fmt][binding].items():
                    for cover_type, qty_prices in covers.items():
                        for qty_str, price in qty_prices.items():
                            db.add(PriceTier(
                                product_id=product.id,
                                spec_key=f"{pages_str}pp|{cover_type}",
                                quantity=int(qty_str),
                                price=float(price),
                            ))
                            tier_count += 1
        print(f"  → {booklet_count} booklet product-variants, {tier_count} price tiers")

        # ---------- ALIASES ----------
        print("Loading product aliases...")
        from extractor import PRODUCT_ALIASES  # reuse curated aliases

        alias_count = 0
        for product_key, aliases in PRODUCT_ALIASES.items():
            product = db.query(Product).filter_by(key=product_key).first()
            if product is None:
                continue
            for alias in aliases:
                db.add(ProductAlias(product_id=product.id, alias=alias.lower()))
                alias_count += 1
        print(f"  → {alias_count} aliases")

        # ---------- SURCHARGE RULES ----------
        # Two shapes supported for backwards compat:
        #   - scalar: "double_sided": 0.2 → kind=multiplier, amount=0.2
        #   - rich:   "soft_touch": {"kind":"additive","amount":15.0,"applies_to_category":"small_format"}
        print("Loading surcharge rules...")
        rules = _load_json("rules.json")
        for name, spec in rules["surcharges"].items():
            if isinstance(spec, (int, float)):
                kind = "multiplier"
                amount = float(spec)
                applies_to_category = None
                description = f"{name.replace('_', ' ').title()} surcharge: +{int(amount * 100)}%"
            else:
                kind = (spec.get("kind") or "multiplier").strip().lower()
                amount = float(spec.get("amount") or spec.get("multiplier") or 0.0)
                applies_to_category = spec.get("applies_to_category")
                description = spec.get("description") or (
                    f"{name.replace('_', ' ').title()} surcharge: "
                    + (f"+€{amount:.2f} flat" if kind == "additive" else f"+{int(amount * 100)}%")
                )
            db.add(SurchargeRule(
                name=name,
                multiplier=amount,
                kind=kind,
                applies_to_category=applies_to_category,
                description=description,
            ))
        print(f"  → {len(rules['surcharges'])} surcharge rules")

        # ---------- SETTINGS ----------
        print("Loading settings...")
        settings = [
            ("artwork_rate_eur", str(rules["artwork_rate_eur"]), "float",
             "Artwork/design hourly rate, EUR ex VAT"),
            ("vat_rate", str(rules["vat_rate"]), "float",
             "Irish VAT rate (0.23 = 23%)"),
            ("standard_turnaround", rules["standard_turnaround"], "string",
             "Standard turnaround time quoted to customers"),
            ("poa_items", json.dumps(rules["poa_items"]), "json",
             "Items Craig must escalate (z-fold, die-cut, etc.)"),
        ]
        for key, value, value_type, description in settings:
            db.add(Setting(
                key=key, value=value, value_type=value_type, description=description,
            ))
        print(f"  → {len(settings)} settings")

    print("\nMigration complete.")

    # Verification
    with db_session() as db:
        print("\nVerification:")
        print(f"  Products:         {db.query(Product).count()}")
        print(f"  Price tiers:      {db.query(PriceTier).count()}")
        print(f"  Aliases:          {db.query(ProductAlias).count()}")
        print(f"  Surcharge rules:  {db.query(SurchargeRule).count()}")
        print(f"  Settings:         {db.query(Setting).count()}")


if __name__ == "__main__":
    migrate()
