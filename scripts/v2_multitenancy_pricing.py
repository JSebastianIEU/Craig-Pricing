"""
Migration: bring an existing single-tenant Craig DB up to the V2 multi-tenant +
configurable-pricing schema.

Idempotent — safe to run multiple times.

Adds:
  - organization_slug column to: products, price_tiers, product_aliases,
    surcharge_rules, settings, conversations, quotes
  - pricing_strategy + metric_unit columns to products
  - kind + applies_to_category columns to surcharge_rules
  - tax_rates + category_tax_map tables (created via Base.metadata.create_all)

Backfills:
  - All existing rows → organization_slug = 'just-print'
  - All existing products → pricing_strategy inferred from category
  - All existing surcharges → kind = 'multiplier'
  - Seed Just Print's tax rates: standard=23%, reduced=13.5%
  - Seed category-tax map: printed-matter categories → reduced; others → standard

Usage:
    python -m scripts.v2_multitenancy_pricing
"""

from __future__ import annotations

import sys
from pathlib import Path

# Make sure the repo root is importable when run as a script
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import inspect, text

from db import engine, db_session, init_db
from db.models import (
    Base, Product, SurchargeRule, TaxRate, CategoryTaxMap, DEFAULT_ORG_SLUG,
)


def column_exists(conn, table: str, column: str) -> bool:
    inspector = inspect(conn)
    cols = [c["name"] for c in inspector.get_columns(table)]
    return column in cols


def add_column_if_missing(conn, table: str, column_def: str, column_name: str) -> None:
    if not column_exists(conn, table, column_name):
        conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column_def}"))
        print(f"  + ALTER TABLE {table} ADD COLUMN {column_name}")
    else:
        print(f"  · {table}.{column_name} already present")


def backfill_org(conn, table: str, default: str = DEFAULT_ORG_SLUG) -> None:
    result = conn.execute(
        text(f"UPDATE {table} SET organization_slug = :v WHERE organization_slug IS NULL OR organization_slug = ''"),
        {"v": default},
    )
    if result.rowcount:
        print(f"  ↪ backfilled {result.rowcount} rows in {table}")


# Categories that get the reduced (13.5%) printed-matter VAT rate in Ireland.
# Matches the original hardcoded set in pricing_engine.py.
_PRINTED_MATTER_CATEGORIES = {"small_format", "booklet"}


def infer_pricing_strategy(category: str) -> str:
    """Infer the legacy pricing strategy from category for backfill."""
    if category == "small_format":
        return "tiered"
    if category == "booklet":
        return "tiered"  # spec_key encodes booklet specs
    if category == "large_format":
        return "bulk_break"
    return "tiered"


def migrate() -> None:
    print("Migrating Craig DB to V2 schema...")
    print()

    # 1) Make sure all tables exist — creates new ones (tax_rates, category_tax_map)
    print("Creating tables (idempotent)...")
    init_db()
    Base.metadata.create_all(engine)

    # 2) ALTER existing tables to add new columns
    print()
    print("Adding new columns where missing...")
    with engine.begin() as conn:
        # organization_slug everywhere
        for tbl in [
            "products", "price_tiers", "product_aliases", "surcharge_rules",
            "settings", "conversations", "quotes",
        ]:
            add_column_if_missing(
                conn, tbl,
                f"organization_slug VARCHAR(80) DEFAULT '{DEFAULT_ORG_SLUG}'",
                "organization_slug",
            )

        # Product extra columns
        add_column_if_missing(conn, "products", "pricing_strategy VARCHAR(30) DEFAULT 'tiered'", "pricing_strategy")
        add_column_if_missing(conn, "products", "metric_unit VARCHAR(30)", "metric_unit")

        # SurchargeRule extra columns
        add_column_if_missing(conn, "surcharge_rules", "kind VARCHAR(20) DEFAULT 'multiplier'", "kind")
        add_column_if_missing(conn, "surcharge_rules", "applies_to_category VARCHAR(80)", "applies_to_category")

    # 3) Backfill organization_slug + new columns
    print()
    print("Backfilling rows...")
    with engine.begin() as conn:
        for tbl in [
            "products", "price_tiers", "product_aliases", "surcharge_rules",
            "settings", "conversations", "quotes",
        ]:
            backfill_org(conn, tbl)

    # 4) Backfill product.pricing_strategy from category
    with db_session() as db:
        products = db.query(Product).all()
        for p in products:
            if not p.pricing_strategy or p.pricing_strategy == "tiered":
                inferred = infer_pricing_strategy(p.category)
                if p.pricing_strategy != inferred:
                    p.pricing_strategy = inferred
        # Backfill surcharge.kind
        for s in db.query(SurchargeRule).all():
            if not s.kind:
                s.kind = "multiplier"
        print(f"  ↪ pricing_strategy + surcharge.kind backfilled")

    # 5) Seed tax rates and category map for Just Print
    print()
    print("Seeding tax rates for Just Print...")
    with db_session() as db:
        existing = {t.name for t in db.query(TaxRate).filter_by(organization_slug=DEFAULT_ORG_SLUG).all()}

        wanted: list[tuple[str, float, str, bool]] = [
            ("standard", 0.23, "Standard Irish VAT — applies to signage, services, large format.", True),
            ("reduced", 0.135, "Reduced VAT for printed matter (flyers, cards, booklets).", False),
            ("zero", 0.00, "Zero-rated items.", False),
        ]
        for name, rate, desc, is_default in wanted:
            if name in existing:
                continue
            db.add(TaxRate(
                organization_slug=DEFAULT_ORG_SLUG,
                name=name, rate=rate,
                description=desc,
                is_default=is_default,
            ))
            print(f"  + tax_rate {name} = {rate}")
        db.flush()

        # Map categories to tax rates
        rates = {t.name: t for t in db.query(TaxRate).filter_by(organization_slug=DEFAULT_ORG_SLUG).all()}
        existing_maps = {
            m.category for m in db.query(CategoryTaxMap).filter_by(organization_slug=DEFAULT_ORG_SLUG).all()
        }

        # Each existing category in `products` gets mapped
        category_rows = db.query(Product.category).filter_by(organization_slug=DEFAULT_ORG_SLUG).distinct().all()
        for (cat,) in category_rows:
            if cat in existing_maps:
                continue
            target_rate = rates["reduced"] if cat in _PRINTED_MATTER_CATEGORIES else rates["standard"]
            db.add(CategoryTaxMap(
                organization_slug=DEFAULT_ORG_SLUG,
                category=cat,
                tax_rate_id=target_rate.id,
            ))
            print(f"  + category_tax_map {cat} → {target_rate.name}")

    print()
    print("✓ Migration complete.")


if __name__ == "__main__":
    migrate()
