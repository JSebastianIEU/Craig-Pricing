"""
Migration script: JSON pricing files -> SQLite/Postgres database.

Originally a one-shot bootstrap for the `just-print` tenant. Now accepts
`--org-slug <slug>` so the same JSON catalogs can seed any tenant —
useful for the `demo` tenant + future client onboardings.

Idempotent **per tenant**: drops and recreates pricing data ONLY for the
named slug. Other tenants' data is untouched. Conversations and quotes
are never touched.

Usage:
    # First-time bootstrap of just-print (legacy default)
    python scripts/migrate_json_to_db.py

    # Provision the demo tenant with the same catalog
    python scripts/migrate_json_to_db.py --org-slug demo

    # Or, programmatically:
    from scripts.migrate_json_to_db import migrate
    migrate(organization_slug="demo")
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db import db_session, init_db
from db.models import (
    DEFAULT_ORG_SLUG,
    Product, PriceTier, ProductAlias, SurchargeRule, Setting,
)

DATA_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data",
)


def _load_json(filename: str) -> dict:
    with open(os.path.join(DATA_DIR, filename), "r", encoding="utf-8") as f:
        return json.load(f)


def _wipe_tenant(db, slug: str) -> None:
    """Delete pricing data ONLY for this tenant. Surgical — won't touch
    other tenants' rows."""
    # PriceTier has FK to Product, so collect product_ids first then delete tiers
    product_ids = [
        pid for (pid,) in db.query(Product.id)
        .filter(Product.organization_slug == slug).all()
    ]
    if product_ids:
        db.query(PriceTier).filter(PriceTier.product_id.in_(product_ids)).delete(
            synchronize_session=False
        )
        db.query(ProductAlias).filter(ProductAlias.product_id.in_(product_ids)).delete(
            synchronize_session=False
        )
    db.query(Product).filter(Product.organization_slug == slug).delete(
        synchronize_session=False
    )
    db.query(SurchargeRule).filter(SurchargeRule.organization_slug == slug).delete(
        synchronize_session=False
    )
    # Pricing-only settings — leave system_prompt, missive_*, stripe_*, etc. alone.
    pricing_keys = ("artwork_rate_eur", "vat_rate", "standard_turnaround", "poa_items")
    db.query(Setting).filter(
        Setting.organization_slug == slug,
        Setting.key.in_(pricing_keys),
    ).delete(synchronize_session=False)
    db.commit()


def migrate(organization_slug: str = DEFAULT_ORG_SLUG) -> None:
    """Bootstrap or refresh the pricing catalog for `organization_slug`.
    Other tenants are not touched."""
    print(f"Creating tables...")
    init_db()

    print(f"Migrating pricing for tenant: {organization_slug!r}")

    with db_session() as db:
        print(f"Clearing existing pricing data for {organization_slug!r}...")
        _wipe_tenant(db, organization_slug)

        # ---------- SMALL FORMAT ----------
        print("Loading small format products...")
        small_format = _load_json("small_format.json")
        for key, data in small_format.items():
            product = Product(
                organization_slug=organization_slug,
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

            for qty_str, price in data["prices"].items():
                db.add(PriceTier(
                    product_id=product.id,
                    spec_key="",
                    quantity=int(qty_str),
                    price=float(price),
                ))
        print(f"  -> {len(small_format)} small format products")

        # ---------- LARGE FORMAT ----------
        print("Loading large format products...")
        large_format = _load_json("large_format.json")
        for key, data in large_format.items():
            product = Product(
                organization_slug=organization_slug,
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
        print(f"  -> {len(large_format)} large format products")

        # ---------- BOOKLETS ----------
        print("Loading booklets...")
        booklets = _load_json("booklets.json")
        booklet_count = 0
        tier_count = 0
        for fmt in booklets:
            for binding in booklets[fmt]:
                product_key = f"booklet_{fmt}_{binding}"
                product = Product(
                    organization_slug=organization_slug,
                    key=product_key,
                    name=f"Booklet {fmt.upper()} - {binding.replace('_', ' ').title()}",
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
        print(f"  -> {booklet_count} booklet variants, {tier_count} tiers")

        # ---------- ALIASES ----------
        print("Loading product aliases...")
        from extractor import PRODUCT_ALIASES

        alias_count = 0
        for product_key, aliases in PRODUCT_ALIASES.items():
            product = (
                db.query(Product)
                .filter_by(organization_slug=organization_slug, key=product_key)
                .first()
            )
            if product is None:
                continue
            for alias in aliases:
                db.add(ProductAlias(product_id=product.id, alias=alias.lower()))
                alias_count += 1
        print(f"  -> {alias_count} aliases")

        # ---------- SURCHARGE RULES ----------
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
                    + (f"+EUR {amount:.2f} flat" if kind == "additive"
                       else f"+{int(amount * 100)}%")
                )
            db.add(SurchargeRule(
                organization_slug=organization_slug,
                name=name,
                multiplier=amount,
                kind=kind,
                applies_to_category=applies_to_category,
                description=description,
            ))
        print(f"  -> {len(rules['surcharges'])} surcharge rules")

        # ---------- SETTINGS (pricing-only) ----------
        print("Loading pricing settings...")
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
                organization_slug=organization_slug,
                key=key, value=value, value_type=value_type, description=description,
            ))
        print(f"  -> {len(settings)} pricing settings")

    print("\nMigration complete.")

    with db_session() as db:
        print(f"\nVerification for {organization_slug!r}:")
        print(f"  Products:         {db.query(Product).filter_by(organization_slug=organization_slug).count()}")
        print(f"  Price tiers:      {db.query(PriceTier).join(Product).filter(Product.organization_slug == organization_slug).count()}")
        print(f"  Surcharge rules:  {db.query(SurchargeRule).filter_by(organization_slug=organization_slug).count()}")
        print(f"  Pricing settings: {db.query(Setting).filter(Setting.organization_slug == organization_slug, Setting.key.in_(['artwork_rate_eur', 'vat_rate', 'standard_turnaround', 'poa_items'])).count()}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Seed pricing catalog for a tenant")
    parser.add_argument(
        "--org-slug",
        default=DEFAULT_ORG_SLUG,
        help=f"Organization slug to seed (default: {DEFAULT_ORG_SLUG!r})",
    )
    args = parser.parse_args()
    migrate(organization_slug=args.org_slug)
