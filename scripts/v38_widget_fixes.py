"""
V38 migration — widget bug-fix pass.

Driven by the audit of the 33 most recent web-widget conversations
which showed a 42% abandon rate. Six concrete bugs (engine math,
flow ordering, missing catalog, language detection) — see
plan file for details.

This migration is the DB-side companion to the code changes:

  1. Adds `Product.requires_dimensions` (Boolean, default False)
     and `Product.sanity_max_unit_price` (Float, nullable).
  2. Flips `vinyl_labels.requires_dimensions = True` so the engine
     escalates instead of falling back to yield-only math when the
     LLM forgot to pass per-unit dimensions (Bug 1).
  3. Seeds the `posters` product (manual_review_required=True) with
     A4-A0 sizes + aliases (Bug 5/6).
  4. Force-reseeds the `business_rules` Setting with v38 wording —
     price first, ask artwork second (Bug 3); language-mirroring (Bug 4);
     tightened dimension requirement (Bug 1 prompt side).

Idempotent. Safe to re-run.

Usage:
    python -m scripts.v38_widget_fixes
"""

from __future__ import annotations

import json as _json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import inspect, text

from db import db_session, engine
from db.models import (
    DEFAULT_ORG_SLUG,
    Product,
    ProductAlias,
    Setting,
)


# ---------------------------------------------------------------------------
# Schema — two new Product columns
# ---------------------------------------------------------------------------

_COLUMN_DEFS = [
    ("products", "requires_dimensions", "BOOLEAN NOT NULL DEFAULT FALSE"),
    ("products", "sanity_max_unit_price", "FLOAT NULL"),
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
# Business rules v38 — price-first, artwork-second, language-mirror
# ---------------------------------------------------------------------------

BUSINESS_RULES_V38 = [
    # Rule 0 — LANGUAGE MIRRORING. Must be first so it overrides everything.
    "v38 — LANGUAGE MIRRORING. Detect the customer's language from "
    "their first message and reply in the SAME language. Spanish "
    "(quiero, necesito, cuanto), French (bonjour, je veux, combien), "
    "Portuguese (quero, preciso), German, Italian — match it. If "
    "ambiguous or English, default to English. Lock in the language "
    "at turn 1 and keep using it for the whole conversation. All "
    "other rules (tone, formatting, no markdown, golden rules) apply "
    "identically in whatever language you're using.",

    "On the first turn, do not duplicate the widget greeting. Reply "
    "with substance: ask one specific question or give a price. "
    "Never repeat 'Craig here' or 'I handle pricing'.",

    # Rule 2 — v38 REWRITE. Audit showed 42% abandon rate because
    # customers wanted to see the price BEFORE committing to send
    # artwork files. New flow: price FIRST, then ask artwork.
    "v38 — PRICE FIRST, ARTWORK SECOND. The customer MUST see a "
    "price BEFORE you ask the artwork question. NEVER ask the "
    "artwork question on the first turn when you have enough specs "
    "to price.\n"
    "\n"
    "When you have enough info to price, CALL THE PRICING TOOL "
    "IMMEDIATELY with needs_artwork=false. Specifically:\n"
    "  * PVC/mesh/fabric banners — if customer gave dimensions "
    "    (e.g. '1m x 2m', '1000x2000mm', '2 sq m'), that is "
    "    ENOUGH. Call quote_large_format with width_mm + height_mm "
    "    (or area_sqm) and quantity=1. Banners have no finish/sides "
    "    question. DO NOT confirm specs, DO NOT ask artwork yet.\n"
    "  * Foamex/dibond/corri panels — if customer gave panel "
    "    dimensions, call quote_large_format with width_mm + "
    "    height_mm + quantity. DO NOT ask anything else first.\n"
    "  * Vinyl labels — if customer gave per-label width+height AND "
    "    quantity, call quote_large_format. If dims are missing, "
    "    ASK for them (NEVER ask artwork instead of dims).\n"
    "  * Business cards / flyers — if customer gave quantity + "
    "    sides + finish, call quote_small_format.\n"
    "  * Booklets — if customer gave format, binding, pages, cover, "
    "    quantity — call quote_booklet.\n"
    "\n"
    "AFTER the tool returns a price, reply with BOTH the price AND "
    "the artwork question in the same message:\n"
    "\n"
    "  \"That'll be EUR X.XX inc VAT for [short product summary] "
    "[emoji]. Quick one before I wrap the full quote: do you have "
    "your own print-ready artwork, or would you like our design "
    "service (EUR 65 ex VAT for one hour of design work)?\"\n"
    "\n"
    "If the customer later picks design service, re-quote with "
    "needs_artwork=true to add the design line item. Design service "
    "is EUR 65 ex VAT for ONE HOUR of design work; always frame it "
    "as 'one hour of design'.",

    # Rule 3 — confirm specs ONLY when ambiguous. v38.1 — eliminated
    # the unconditional spec-confirm step. For most cases the LLM
    # should just call the tool. Only confirm when there's
    # ACTUAL ambiguity (customer said 'a few', vague qty, etc.).
    "Spec confirmation is OPTIONAL — skip it unless there is real "
    "ambiguity. Examples of when to confirm:\n"
    "  * Customer said 'a few hundred' or 'around 500' — ask for "
    "    the exact number.\n"
    "  * Customer gave one number that could be quantity OR a size "
    "    — clarify.\n"
    "  * Customer mentioned two different products — clarify which.\n"
    "\n"
    "When specs are CLEAR (e.g. 'PVC banner 1m x 2m', '500 business "
    "cards single-sided matte', '1000 vinyl labels 40x10mm'), DO "
    "NOT confirm — just call the pricing tool directly per Rule 2.",

    "v33 — On the email channel, the STEP 4 binding-quote email "
    "(price + PDF) is now AUTO-SENT. There are no Missive drafts on "
    "the customer side anymore. The customer sees the PDF the moment "
    "specs+artwork+funnel are all in.",

    "Escalations to Justin (escalate_to_justin tool) require contact "
    "info first. If you need to escalate but don't have name + "
    "email/phone, ask for them first, save_customer_info, then "
    "escalate.",

    "A list of frequently asked questions is injected separately under "
    "## Frequently asked questions. When the customer asks one (or "
    "anything close), answer it INLINE in your own voice — paraphrase, "
    "don't read verbatim. Do not escalate FAQs.",

    "Just Print operates in Ireland only. If a customer asks for "
    "delivery outside Ireland, politely tell them we only ship within "
    "Ireland and offer collection from our Ballymount shop.",

    # Rule 8 — v38 TIGHTENED. Old rule said "fall back to yield" if no
    # dims. Audit (Ian's case) showed that fallback gives wrong prices
    # for small labels (€277 for 500 labels of 40x10mm).
    "v38 — DIMENSION-BASED PRICING for large-format products.\n"
    "\n"
    "Per-sq/m products (vinyl labels, PVC banners, mesh banners, "
    "window graphics, floor graphics, fabric displays):\n"
    "  - For VINYL LABELS / DIE-CUT items: ALWAYS ask for the size "
    "    of EACH label in mm before pricing. Pass width_mm + "
    "    height_mm + quantity to quote_large_format. NEVER call the "
    "    tool without dims for vinyl labels — the engine will REFUSE "
    "    the call and escalate (requires_dimensions=True is enforced "
    "    server-side as of v38).\n"
    "  - For BANNERS / GRAPHICS / FABRIC: ask for the overall width "
    "    and height of the printed piece in mm. Pass width_mm + "
    "    height_mm + quantity (usually 1 for a single banner).\n"
    "  - If the customer can't give dims for vinyl labels, ask "
    "    explicitly: \"What size is each label, roughly? Width x "
    "    height in mm.\" Don't proceed until they answer.\n"
    "\n"
    "Per-sheet products (foamex, dibond, corri-boards):\n"
    "  - Ask for the size of EACH panel in mm. Pass width_mm + "
    "    height_mm + quantity. Engine computes sheets needed.\n"
    "  - If the customer can't say a panel size, escalate via "
    "    escalate_to_justin.\n"
    "\n"
    "POSTERS (v38 new):\n"
    "  - When the customer asks for posters, A0/A1/A2/A3/A4 prints "
    "    etc., recognise the request, ask for size + quantity, then "
    "    escalate via escalate_to_justin. Posters are manual-review "
    "    in v38 (pricing varies by size + paper) — never invent a "
    "    price. Tell the customer Justin will follow up with the "
    "    exact quote.\n"
    "\n"
    "NEVER use 'around', 'roughly', 'about', 'approximately'. If the "
    "engine returns manual_review:true, acknowledge to the customer "
    "+ ask for any missing info; never invent a price.",
]


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------


def migrate_ddl_only() -> None:
    """v38 DDL only — adds new Product columns. Runs early in
    startup so older ORM migrations don't trip on the missing columns."""
    print("V38 DDL: adding requires_dimensions + sanity_max_unit_price...")
    with engine.begin() as conn:
        for table, name, defn in _COLUMN_DEFS:
            if _add_column_if_missing(conn, table, name, defn):
                print(f"  + {table}.{name}")
            else:
                print(f"  - {table}.{name} already present")


def _flip_vinyl_labels_requires_dimensions(db) -> None:
    """Flip the new requires_dimensions flag on for products where the
    yield-only fallback is unsafe (sizes vary too much). Currently only
    vinyl_labels — banners are area-based so the fallback is fine for
    them."""
    products_to_flip = ["vinyl_labels"]
    for org_slug in {DEFAULT_ORG_SLUG, "just-print"}:
        for key in products_to_flip:
            p = db.query(Product).filter_by(
                organization_slug=org_slug, key=key,
            ).first()
            if p is None:
                continue
            if getattr(p, "requires_dimensions", False):
                print(
                    f"  - {key} (org={org_slug}): requires_dimensions "
                    f"already True"
                )
                continue
            p.requires_dimensions = True
            print(
                f"  + {key} (org={org_slug}): requires_dimensions = True"
            )


def _seed_posters_product(db) -> None:
    """Add `posters` to the catalog as a manual_review product. Justin
    can fill in per-size pricing later via the dashboard; for now Craig
    at least recognises the product instead of getting confused."""
    poster_aliases = [
        "poster", "posters", "wall poster", "wall posters",
        "event poster", "event posters",
        "a0 print", "a0 prints", "a0 poster", "a0 posters",
        "a1 print", "a1 prints", "a1 poster", "a1 posters",
        "a2 print", "a2 prints", "a2 poster", "a2 posters",
        "a3 poster", "a3 posters",
        "paper poster", "paper posters",
        "large print", "large prints",
    ]
    for org_slug in {DEFAULT_ORG_SLUG, "just-print"}:
        existing = db.query(Product).filter_by(
            organization_slug=org_slug, key="posters",
        ).first()
        if existing:
            print(f"  - posters (org={org_slug}): already exists")
            # Still ensure manual_review_required is set in case someone
            # manually flipped it off.
            if not existing.manual_review_required:
                existing.manual_review_required = True
                existing.manual_review_reason = (
                    "Poster pricing varies by size, paper weight, "
                    "finish — needs Justin's quote"
                )
                print(f"     (re-set manual_review_required=True)")
        else:
            p = Product(
                organization_slug=org_slug,
                key="posters",
                name="Posters",
                category="large_format",
                description=(
                    "Flat paper posters — full colour digital print on "
                    "170gsm or 200gsm silk paper, sizes A4 through A0."
                ),
                pricing_strategy="manual_review",
                pricing_unit="per poster",
                min_qty=1,
                notes=(
                    "Pricing varies by size and paper weight — Justin "
                    "will quote manually."
                ),
                manual_review_required=True,
                manual_review_reason=(
                    "Poster pricing varies by size, paper weight, "
                    "finish — needs Justin's quote"
                ),
            )
            db.add(p)
            db.flush()
            print(f"  + posters (org={org_slug}): seeded as manual_review")

            # Seed aliases
            for a in poster_aliases:
                db.add(ProductAlias(
                    organization_slug=org_slug,
                    product_id=p.id,
                    alias=a.lower(),
                ))
            print(f"     + {len(poster_aliases)} aliases seeded")


def _reseed_business_rules(db) -> None:
    rules_json = _json.dumps(BUSINESS_RULES_V38, ensure_ascii=False)
    for org_slug in {DEFAULT_ORG_SLUG, "just-print"}:
        row = db.query(Setting).filter_by(
            organization_slug=org_slug, key="business_rules",
        ).first()
        if row is None:
            db.add(Setting(
                organization_slug=org_slug,
                key="business_rules",
                value=rules_json,
                value_type="json",
            ))
            print(f"  + business_rules (org={org_slug}): seeded ({len(BUSINESS_RULES_V38)} rules)")
        else:
            row.value = rules_json
            row.value_type = "json"
            print(
                f"  + business_rules (org={org_slug}): force-reseeded "
                f"({len(BUSINESS_RULES_V38)} rules — v38)"
            )


def migrate() -> None:
    print("V38: widget bug-fix pass (requires_dimensions + posters + "
          "price-first rules)...")
    migrate_ddl_only()
    with db_session() as db:
        _flip_vinyl_labels_requires_dimensions(db)
        _seed_posters_product(db)
        _reseed_business_rules(db)
        db.commit()
    print()
    print("v38 migration complete.")


if __name__ == "__main__":
    migrate()
