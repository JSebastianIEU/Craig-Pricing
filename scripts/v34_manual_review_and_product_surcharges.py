"""
V34 migration — manual-review escalation, per-product surcharges,
pricing verification table, friendlier Pricing UX.

Why this exists: JP-0086 was a real customer asking for "500 vinyl
labels". The product is configured `pricing_unit='per sq/m'` but its
`pricing_strategy='bulk_break'` treats `quantity` as a unitless
integer count. The engine multiplied 500 × €40 (bulk price) =
€20,000 ex VAT and stamped a quote no customer would ever pay
(€24,600 inc VAT).

Six products share the same per-sq/m vs bulk_break mismatch:
  vinyl_labels, pvc_banners, window_graphics, floor_graphics,
  mesh_banners, fabric_displays.

This migration:

  1. Adds new columns:
       Product.manual_review_required (bool)
       Product.manual_review_reason   (text)
       Product.internal_notes         (text — operator-only)
       Quote.manual_review_reason     (text)
       Quote.manual_quote_price_inc_vat / _ex_vat (float)
       Quote.manual_quote_notes       (text)
       Quote.manually_priced_at       (timestamp)
       Quote.manually_priced_by       (varchar)
       SurchargeRule.applies_to_product_keys (JSON)
     Plus the new pricing_verification_flags table.

  2. Force-flips manual_review_required=True for the six per-sq/m
     products + any products that match POA items (e.g. die-cut).

  3. Re-seeds the soft_touch surcharge with
     applies_to_product_keys=["business_cards"] (its original Phase D
     intent — the v22 migration scoped it to small_format because
     applies_to_category was the only available scope, but the engine
     never honored even that).

  4. Force-reseeds business_rules with v34 wording — adds Rule 8
     ("manual-review escalation") teaching Craig how to respond when
     a tool returns manual_review:true (acknowledge + ask for
     dimensions; never invent a price).

  5. Seeds operator settings:
       manual_review_notification_subject_prefix
       poa_keywords (copied from data/rules.json)

Idempotent. Safe to re-run.

Usage:
    python -m scripts.v34_manual_review_and_product_surcharges
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import inspect, text

from db import db_session, engine
from db.models import DEFAULT_ORG_SLUG, Product, Setting, SurchargeRule


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_COLUMN_DEFS = [
    # Product manual-review flag + per-product internal notes
    ("products", "manual_review_required", 'BOOLEAN NOT NULL DEFAULT 0'),
    ("products", "manual_review_reason", "TEXT NULL"),
    ("products", "internal_notes", "TEXT NULL"),

    # Quote manual-review fields
    ("quotes", "manual_review_reason", "TEXT NULL"),
    ("quotes", "manual_quote_price_inc_vat", "FLOAT NULL"),
    ("quotes", "manual_quote_price_ex_vat", "FLOAT NULL"),
    ("quotes", "manual_quote_notes", "TEXT NULL"),
    ("quotes", "manually_priced_at", "TIMESTAMP NULL"),
    ("quotes", "manually_priced_by", "VARCHAR(120) NULL"),

    # SurchargeRule — per-product scoping (JSON list of product keys)
    # JSONB on Postgres, TEXT on SQLite (SQLAlchemy's JSON type adapter
    # handles both via the ORM, but the raw ALTER needs the right
    # column type).
    ("surcharge_rules", "applies_to_product_keys", "JSONB NULL"),
]


# Per-sq/m products that must escalate to Justin instead of auto-quoting.
# These six were the audit's primary vulnerability — pricing_unit says
# "per sq/m" but pricing_strategy='bulk_break' treats qty as an integer
# count. The fix is to refuse to quote at runtime; Justin manually
# prices from the dashboard once he has dimensions.
_PER_SQM_PRODUCT_KEYS = [
    "vinyl_labels",
    "pvc_banners",
    "window_graphics",
    "floor_graphics",
    "mesh_banners",
    "fabric_displays",
]
_PER_SQM_REASON = "per-sq/m item — needs width/height to quote"


# Catalog products that match a POA item from data/rules.json. These
# are products where the catalog price exists but the product itself
# requires Justin's eyes (custom finishing, hardware, etc.). We only
# flip `manual_review_required` for products that match an exact key —
# narrative POA items like "rush job" stay in poa_keywords for the
# LLM/agent to detect, not the engine.
_POA_PRODUCT_KEYS_AND_REASONS = [
    # die-cut labels are typically vinyl_labels with extra finishing —
    # already covered by per-sq/m flip above. Listed here for clarity.
    # ("vinyl_labels", "POA — die-cut requires manual quote"),
]


# v34 business rules — appends Rule 8 "manual-review escalation" to v33's set.
BUSINESS_RULES_V34 = [
    "On the first turn, do not duplicate the widget greeting. Reply "
    "with substance: ask one specific question or give a price. "
    "Never repeat 'Craig here' or 'I handle pricing'.",

    "Once you have all required specs (product, quantity, finish, "
    "sides), DO NOT call the pricing tool yet. Your VERY NEXT message "
    "must be ONLY this question, nothing else:\n"
    "\n"
    "  'Got it. Quick question before I price it: do you have your "
    "  own print-ready artwork, or would you like our design service "
    "  (€65 ex VAT, €79.95 inc — one hour of design work)?'\n"
    "\n"
    "Wait for the answer. Only then call the pricing tool with the "
    "right needs_artwork value.\n"
    "\n"
    "CRITICAL: design service is €65 ex VAT for ONE HOUR of design.\n"
    "Always frame it as 'one hour of design'.",

    "AFTER the customer answers the artwork question, call the "
    "pricing tool. Quote the inc-VAT total in one short sentence "
    "(e.g. 'That'll be €34.05 for 100 single-sided matte business "
    "cards 👍'), then ask 'Want me to put together the full quote "
    "for you? 📋'. Do NOT emit [QUOTE_READY] yet — funnel info comes "
    "first on web (form), or in the next email turn on Missive.",

    "v33 — On the email channel, the STEP 4 binding-quote email "
    "(price + PDF) is now AUTO-SENT. There are no Missive drafts on "
    "the customer side anymore. The customer sees the PDF the moment "
    "specs+artwork+funnel are all in. Justin's intervention happens "
    "in the dashboard (Approve button), and that's when the payment "
    "link goes out — also auto-sent into the same email thread.",

    "Escalations to Justin (escalate_to_justin tool) require contact "
    "info first. If you need to escalate but don't have name + "
    "email/phone, ask for them first, save_customer_info, then "
    "escalate. Escalation replies STILL go to Missive as drafts — "
    "Justin needs to write the actual answer himself.",

    "A list of frequently asked questions is injected separately under "
    "## Frequently asked questions. When the customer asks one (or "
    "anything close), answer it INLINE in your own voice — paraphrase, "
    "don't read verbatim. Do not escalate FAQs.",

    "Just Print operates in Ireland only. If a customer asks for "
    "delivery outside Ireland, politely tell them we only ship within "
    "Ireland and offer collection from our Ballymount shop. Don't "
    "proceed with the quote until they confirm an Irish address or "
    "pick collection.",

    # Rule 8 — v34 manual-review escalation. CRITICAL for products
    # where the catalog price is unreliable (per-sq/m, POA, custom).
    "v34 — manual-review escalation. When a pricing tool returns "
    "`manual_review: true`, your reply must be ONE short message: "
    "acknowledge that Justin will check, and ASK FOR DIMENSIONS "
    "(width × height per unit in mm) if you don't already have them, "
    "OR for the missing detail named in the tool's `reason`. NEVER "
    "invent a price. NEVER say 'around', 'roughly', 'about', "
    "'approximately'. After you reply, call save_customer_info if "
    "you don't have name+email yet, then stop. Do NOT call any other "
    "quote tool on this turn.\n"
    "\n"
    "Example wording: 'Let me check that with Justin and get back to "
    "you 👍 Quick question first — what size is each label "
    "(width × height in mm)?'",
]


# Default poa_keywords copied from data/rules.json. The engine doesn't
# enforce these (the manual_review_required flag does the heavy lifting
# at the product level); they're persisted as a Setting so the LLM can
# read them when reasoning about whether to escalate borderline cases.
_DEFAULT_POA_KEYWORDS = [
    "z-fold",
    "die-cut labels",
    "die-cut",
    "installation",
    "custom sizes not listed",
    "frame hardware",
    "rush job",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _is_postgres() -> bool:
    return engine.url.drivername.startswith("postgresql")


def _column_exists(conn, table: str, column: str) -> bool:
    return column in [c["name"] for c in inspect(conn).get_columns(table)]


def _add_column_if_missing(conn, table: str, name: str, defn: str) -> bool:
    if _column_exists(conn, table, name):
        return False
    # SQLite type-token rewrites: TIMESTAMP -> DATETIME, JSONB -> TEXT,
    # FLOAT stays as REAL conceptually but FLOAT works in SQLite too.
    if not _is_postgres():
        defn = (
            defn.replace("TIMESTAMP", "DATETIME")
                .replace("JSONB", "TEXT")
        )
    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {defn}"))
    return True


def _table_exists(conn, table: str) -> bool:
    return table in inspect(conn).get_table_names()


def _create_pricing_verification_flags(conn) -> bool:
    """Create the new pricing_verification_flags table. Returns True
    if created. Idempotent."""
    if _table_exists(conn, "pricing_verification_flags"):
        return False

    if _is_postgres():
        ddl = """
            CREATE TABLE pricing_verification_flags (
                id            SERIAL PRIMARY KEY,
                organization_slug VARCHAR(80) NOT NULL,
                product_key   VARCHAR(80) NOT NULL,
                quantity      INTEGER NOT NULL,
                spec_key      VARCHAR(120) NOT NULL DEFAULT '',
                flagged_wrong BOOLEAN NOT NULL DEFAULT FALSE,
                comment       TEXT NULL,
                flagged_by    VARCHAR(120) NULL,
                created_at    TIMESTAMP NOT NULL DEFAULT now(),
                updated_at    TIMESTAMP NOT NULL DEFAULT now(),
                CONSTRAINT uq_pricing_verification_flag
                    UNIQUE (organization_slug, product_key, quantity, spec_key)
            );
            CREATE INDEX ix_pricing_verification_org_prod
                ON pricing_verification_flags (organization_slug, product_key);
        """
    else:
        ddl = """
            CREATE TABLE pricing_verification_flags (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                organization_slug VARCHAR(80) NOT NULL,
                product_key   VARCHAR(80) NOT NULL,
                quantity      INTEGER NOT NULL,
                spec_key      VARCHAR(120) NOT NULL DEFAULT '',
                flagged_wrong BOOLEAN NOT NULL DEFAULT 0,
                comment       TEXT NULL,
                flagged_by    VARCHAR(120) NULL,
                created_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                CONSTRAINT uq_pricing_verification_flag
                    UNIQUE (organization_slug, product_key, quantity, spec_key)
            );
            CREATE INDEX ix_pricing_verification_org_prod
                ON pricing_verification_flags (organization_slug, product_key);
        """
    # Multi-statement DDL — split because some drivers reject in one execute.
    for stmt in ddl.strip().split(";"):
        stmt = stmt.strip()
        if stmt:
            conn.execute(text(stmt))
    return True


def _seed_setting(db, key: str, value: str, *, force: bool = False) -> str:
    existing = (
        db.query(Setting)
        .filter_by(organization_slug=DEFAULT_ORG_SLUG, key=key)
        .first()
    )
    if existing:
        if not force:
            return "skipped"
        existing.value = value
        return "updated"
    db.add(Setting(
        organization_slug=DEFAULT_ORG_SLUG,
        key=key, value=value, value_type="string",
    ))
    return "added"


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------


def migrate() -> None:
    print("V34: manual-review escalation + per-product surcharges + verification...")

    # ── 1. Schema ──────────────────────────────────────────────────────
    added = 0
    with engine.begin() as conn:
        for table, name, defn in _COLUMN_DEFS:
            if _add_column_if_missing(conn, table, name, defn):
                print(f"  + {table}.{name}")
                added += 1
            else:
                print(f"  · {table}.{name} already present")

        if _create_pricing_verification_flags(conn):
            print("  + pricing_verification_flags table created")
        else:
            print("  · pricing_verification_flags already exists")

    # ── 2. Backfill: per-sq/m manual_review flags ──────────────────────
    if _is_postgres():
        _SQL_FLIP_BY_KEY = (
            "UPDATE products "
            "SET manual_review_required = :req, "
            "    manual_review_reason = :reason "
            "WHERE organization_slug = :org "
            "  AND key = :key "
            "  AND COALESCE(manual_review_required, FALSE) = FALSE"
        )
    else:
        _SQL_FLIP_BY_KEY = (
            "UPDATE products "
            "SET manual_review_required = :req, "
            "    manual_review_reason = :reason "
            "WHERE organization_slug = :org "
            "  AND key = :key "
            "  AND COALESCE(manual_review_required, 0) = 0"
        )

    flipped = 0
    with engine.begin() as conn:
        # Idempotent — only mutate where currently null/false.
        for key in _PER_SQM_PRODUCT_KEYS:
            result = conn.execute(
                text(_SQL_FLIP_BY_KEY),
                {
                    "req": True,
                    "reason": _PER_SQM_REASON,
                    "org": DEFAULT_ORG_SLUG,
                    "key": key,
                },
            )
            if result.rowcount:
                print(f"  + flipped manual_review_required for product '{key}'")
                flipped += 1

    if flipped:
        print(f"  -> flipped {flipped} per-sq/m product(s) to manual_review")
    else:
        print("  · no per-sq/m products needed flipping (already set)")

    # ── 3. POA-product flips (matched by key) ──────────────────────────
    for key, reason in _POA_PRODUCT_KEYS_AND_REASONS:
        with engine.begin() as conn:
            conn.execute(
                text(_SQL_FLIP_BY_KEY),
                {"req": True, "reason": reason, "org": DEFAULT_ORG_SLUG, "key": key},
            )

    # ── 4. Re-seed soft_touch surcharge with per-product scope ──────────
    with db_session() as db:
        soft_touch = (
            db.query(SurchargeRule)
            .filter_by(organization_slug=DEFAULT_ORG_SLUG, name="soft_touch")
            .first()
        )
        if soft_touch is not None:
            current = soft_touch.applies_to_product_keys
            # Idempotent — only set if currently null/empty.
            if not current:
                soft_touch.applies_to_product_keys = ["business_cards"]
                soft_touch.applies_to_category = None
                print("  + soft_touch surcharge scoped to ['business_cards']")
            else:
                print(
                    f"  · soft_touch surcharge already has product-keys scope: {current}"
                )
        else:
            print("  · soft_touch surcharge not found (skipping re-scope)")

        # ── 5. Operator settings ───────────────────────────────────────
        r1 = _seed_setting(
            db,
            "manual_review_notification_subject_prefix",
            "[Just Print — needs your eyes]",
            force=False,
        )
        print(f"  {r1:>8}  setting manual_review_notification_subject_prefix")

        r2 = _seed_setting(
            db, "poa_keywords",
            json.dumps(_DEFAULT_POA_KEYWORDS, ensure_ascii=False),
            force=False,
        )
        print(f"  {r2:>8}  setting poa_keywords")

        # ── 6. Force-reseed business_rules with v34 wording ────────────
        rules_json = json.dumps(BUSINESS_RULES_V34, ensure_ascii=False)
        r3 = _seed_setting(db, "business_rules", rules_json, force=True)
        print(f"  {r3:>8}  setting business_rules ({len(BUSINESS_RULES_V34)} rules — v34)")

        db.commit()

    print()
    print(f"v34 migration complete. {added} columns added, {flipped} per-sq/m products flipped.")


if __name__ == "__main__":
    migrate()
