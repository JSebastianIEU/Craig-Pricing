"""
V3 migration: introduce first-class Category rows + product image_url.

Idempotent. Run it once against an existing V2 DB; safe to re-run.

Steps:
  1. ALTER TABLE products ADD COLUMN image_url (if missing)
  2. Create `categories` table via Base.metadata.create_all
  3. Seed one category row for each distinct product category that doesn't
     already have one — humanized name, empty description.

Usage:
    python -m scripts.v3_categories_images
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import inspect, text

from db import engine, db_session
from db.models import Base, Category, Product, DEFAULT_ORG_SLUG


def column_exists(conn, table: str, column: str) -> bool:
    inspector = inspect(conn)
    cols = [c["name"] for c in inspector.get_columns(table)]
    return column in cols


def humanize(s: str) -> str:
    return s.replace("_", " ").title()


def migrate() -> None:
    print("Migrating to V3 (categories + images)...")
    print()

    # 1) Make sure all tables exist — creates `categories` if new
    print("Creating tables (idempotent)...")
    Base.metadata.create_all(engine)

    # 2) image_url on products
    print()
    print("Adding product.image_url if missing...")
    with engine.begin() as conn:
        if not column_exists(conn, "products", "image_url"):
            conn.execute(text("ALTER TABLE products ADD COLUMN image_url TEXT"))
            print("  + ADDED products.image_url")
        else:
            print("  · products.image_url already present")

    # 3) Seed category rows for each unique product category
    print()
    print("Seeding category rows...")
    with db_session() as db:
        orgs_categories: set[tuple[str, str]] = set()
        for (org_slug, category) in (
            db.query(Product.organization_slug, Product.category)
            .distinct()
            .all()
        ):
            orgs_categories.add((org_slug or DEFAULT_ORG_SLUG, category))

        existing = {
            (c.organization_slug, c.slug)
            for c in db.query(Category.organization_slug, Category.slug).all()
        }

        created = 0
        for org_slug, cat in sorted(orgs_categories):
            if (org_slug, cat) in existing:
                continue
            db.add(Category(
                organization_slug=org_slug,
                slug=cat,
                name=humanize(cat),
                description=None,
                sort_order=0,
            ))
            print(f"  + category {org_slug}/{cat}")
            created += 1

        if created == 0:
            print("  · all existing categories already seeded")
        else:
            print(f"  ↪ {created} category rows added")

    print()
    print("✓ V3 migration complete.")


if __name__ == "__main__":
    migrate()
