"""
V36 migration -- per-sq/m + per-sheet pricing strategies.

Justin's actual pricing formulas (from a meeting on 8 May 2026):

  Vinyl labels (per-sq/m):
    81 labels per square meter at 45 EUR/m^2.
    qty=500 -> 500/81 = 6.17 m^2 -> 6.17 x 45 = 277.65 EUR.

  PVC banners (per-sq/m): customer specifies width x height.
    width 1000mm x height 2000mm = 2 m^2 x 28 EUR = 56 EUR.

  Foamex/Dibond/Corri panels (per-sheet):
    sheet = 2400x1200mm, costs 150 EUR.
    panel = 250x500mm -> 18 panels per sheet.
    qty=20 -> ceil(20/18) = 2 sheets x 150 EUR = 300 EUR.

v34 flagged all 6 per-sq/m products as manual_review_required=True
because we didn't have the formulas yet. v36 adds the formulas and
flips the flag back off so Craig can ACTUALLY price these.

This migration:

  1. Adds 4 nullable Product columns:
       yield_per_sqm        (FLOAT)
       default_unit_size_mm (VARCHAR(20))
       sheet_size_mm        (VARCHAR(20))
       sheet_price          (FLOAT)
     Same Postgres-strict pattern as v34/v35 -- migrate_ddl_only runs
     before any older ORM migration that might SELECT * FROM products.

  2. Re-points the 6 per-sq/m products to pricing_strategy='per_sqm'
     and clears manual_review_required. unit_price stays as the
     EUR/m^2 rate (already correct in the catalog). yield_per_sqm
     left null for banners (area-based) and pre-filled with Justin's
     example value for vinyl_labels.

  3. Re-points 3 per-sheet products to pricing_strategy='per_sheet'
     with sheet_size_mm + sheet_price pre-filled where Justin gave
     known values. dibond/corri left manual_review for now (Justin
     to fill defaults via the Pricing UI when he gets to them).

Idempotent. Safe to re-run.

Usage:
    python -m scripts.v36_per_sqm_per_sheet_pricing
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import inspect, text

from db import db_session, engine
from db.models import DEFAULT_ORG_SLUG, Product, Setting


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


_COLUMN_DEFS = [
    ("products", "yield_per_sqm", "FLOAT NULL"),
    ("products", "default_unit_size_mm", "VARCHAR(20) NULL"),
    ("products", "sheet_size_mm", "VARCHAR(20) NULL"),
    ("products", "sheet_price", "FLOAT NULL"),
]


# ---------------------------------------------------------------------------
# Per-product re-pointing -- (product_key, new_strategy, fields_to_set)
# Each entry's fields_to_set is a dict of column -> value. None values
# are skipped (don't blow away existing data).
# ---------------------------------------------------------------------------

_PRODUCT_UPDATES: list[tuple[str, str, dict]] = [
    # Per-sq/m (banners, graphics) -- area-based, no fixed yield. The
    # engine asks the LLM for width_mm + height_mm and computes
    # total_m^2 directly. unit_price already holds the EUR/m^2 rate.
    ("pvc_banners",      "per_sqm", {"yield_per_sqm": None, "manual_review_required": False, "manual_review_reason": None}),
    ("mesh_banners",     "per_sqm", {"yield_per_sqm": None, "manual_review_required": False, "manual_review_reason": None}),
    ("window_graphics",  "per_sqm", {"yield_per_sqm": None, "manual_review_required": False, "manual_review_reason": None}),
    ("floor_graphics",   "per_sqm", {"yield_per_sqm": None, "manual_review_required": False, "manual_review_reason": None}),
    ("fabric_displays",  "per_sqm", {"yield_per_sqm": None, "manual_review_required": False, "manual_review_reason": None}),

    # Per-sq/m with fixed yield (vinyl labels). Justin gave 81 labels
    # per m^2 in his meeting example -- this is the "standard"
    # vinyl-label yield Just Print uses for catalog pricing. When the
    # customer doesn't specify a label size, we use this yield as the
    # fallback (qty / 81 = m^2). When the customer DOES specify size,
    # the engine derives an exact yield from the dimensions and uses
    # that instead. Justin can override via the dashboard.
    ("vinyl_labels", "per_sqm", {
        "yield_per_sqm": 81.0,
        # default_unit_size_mm intentionally null -- we want yield_per_sqm
        # to be the fallback path, not a derived yield from a guessed size.
        "manual_review_required": False,
        "manual_review_reason": None,
    }),

    # Per-sheet panels. 8x4 ft sheet = 2400x1200 mm. Justin's foamex
    # example was sheet_price=150 EUR. Dibond + corri left without
    # sheet_price -- Justin to set via dashboard.
    ("foamex_boards", "per_sheet", {
        "sheet_size_mm": "2400x1200",
        "sheet_price": 150.0,
        "manual_review_required": False,
        "manual_review_reason": None,
    }),
    ("dibond_boards", "per_sheet", {
        "sheet_size_mm": "3000x1500",
        # sheet_price intentionally null -- the engine will escalate
        # until Justin fills it from the Pricing UI.
    }),
    ("corri_boards", "per_sheet", {
        "sheet_size_mm": "2400x1200",
        # sheet_price intentionally null
    }),
]


# v36 business rules update -- replaces v34 Rule 8 manual-review wording.
# Now Craig MUST ask for dimensions for per-sq/m + per-sheet products,
# but can ACTUALLY quote them once he has dimensions.
BUSINESS_RULES_V36 = [
    "On the first turn, do not duplicate the widget greeting. Reply "
    "with substance: ask one specific question or give a price. "
    "Never repeat 'Craig here' or 'I handle pricing'.",

    "Once you have all required specs (product, quantity, finish, "
    "sides), DO NOT call the pricing tool yet. Your VERY NEXT message "
    "must be ONLY this question, nothing else:\n"
    "\n"
    "  'Got it. Quick question before I price it: do you have your "
    "  own print-ready artwork, or would you like our design service "
    "  (65 EUR ex VAT, 79.95 EUR inc -- one hour of design work)?'\n"
    "\n"
    "Wait for the answer. Only then call the pricing tool with the "
    "right needs_artwork value.\n"
    "\n"
    "CRITICAL: design service is 65 EUR ex VAT for ONE HOUR of design.\n"
    "Always frame it as 'one hour of design'.",

    "AFTER the customer answers the artwork question, call the "
    "pricing tool. Quote the inc-VAT total in one short sentence "
    "(e.g. \"That'll be 34.05 EUR for 100 single-sided matte business "
    "cards.\"), then ask 'Want me to put together the full quote "
    "for you?'. Do NOT emit [QUOTE_READY] yet -- funnel info comes "
    "first on web (form), or in the next email turn on Missive.",

    "v33 -- On the email channel, the STEP 4 binding-quote email "
    "(price + PDF) is now AUTO-SENT. There are no Missive drafts on "
    "the customer side anymore. The customer sees the PDF the moment "
    "specs+artwork+funnel are all in. Justin's intervention happens "
    "in the dashboard (Approve button), and that's when the payment "
    "link goes out -- also auto-sent into the same email thread.",

    "Escalations to Justin (escalate_to_justin tool) require contact "
    "info first. If you need to escalate but don't have name + "
    "email/phone, ask for them first, save_customer_info, then "
    "escalate. Escalation replies STILL go to Missive as drafts -- "
    "Justin needs to write the actual answer himself.",

    "A list of frequently asked questions is injected separately under "
    "## Frequently asked questions. When the customer asks one (or "
    "anything close), answer it INLINE in your own voice -- paraphrase, "
    "don't read verbatim. Do not escalate FAQs.",

    "Just Print operates in Ireland only. If a customer asks for "
    "delivery outside Ireland, politely tell them we only ship within "
    "Ireland and offer collection from our Ballymount shop. Don't "
    "proceed with the quote until they confirm an Irish address or "
    "pick collection.",

    # Rule 8 -- v36 dimension-based pricing for per-sq/m + per-sheet.
    "v36 -- dimension-based pricing for large-format products.\n"
    "\n"
    "Per-sq/m products (vinyl labels, PVC banners, mesh banners, "
    "window graphics, floor graphics, fabric displays):\n"
    "  - For BANNERS / GRAPHICS / FABRIC: ask the customer for the "
    "    overall width and height of the printed piece in mm "
    "    (e.g. 'a 1000mm x 2000mm banner'). Pass width_mm + height_mm "
    "    and quantity=1 (or quantity=N for multiple identical "
    "    banners) to quote_large_format. The engine computes "
    "    qty * (w*h)/1_000_000 m^2 * unit_price.\n"
    "  - For VINYL LABELS / DIE-CUT items: ask for the size of EACH "
    "    label in mm. Pass width_mm + height_mm + quantity. The "
    "    engine computes the area of one label, multiplies by qty "
    "    for total m^2, and bills at unit_price.\n"
    "  - If the customer can't say a size, fall back to the product's "
    "    default_unit_size_mm (configured per-product in the catalog) "
    "    OR yield_per_sqm. The engine handles this automatically.\n"
    "\n"
    "Per-sheet products (foamex, dibond, corri-boards):\n"
    "  - Ask for the size of EACH panel in mm. Pass width_mm + "
    "    height_mm + quantity. The engine computes how many panels "
    "    fit on a sheet (axis-aligned, with rotation) and multiplies "
    "    sheets_needed by sheet_price.\n"
    "  - If the customer can't say a panel size, escalate via "
    "    escalate_to_justin -- panels are too variable to default.\n"
    "\n"
    "NEVER quote without dimensions for these products. NEVER use "
    "'around', 'roughly', 'about', 'approximately'. If the engine "
    "returns manual_review:true (because dimensions weren't passed "
    "or config is missing), follow the v34 fallback: acknowledge to "
    "the customer + ask for dimensions, never invent a price.",
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
    if not _is_postgres():
        defn = (
            defn.replace("TIMESTAMP", "DATETIME")
                .replace("JSONB", "TEXT")
                .replace("DEFAULT FALSE", "DEFAULT 0")
                .replace("DEFAULT TRUE", "DEFAULT 1")
        )
    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {defn}"))
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


def migrate_ddl_only() -> None:
    """v36 DDL only -- runs early in startup so older ORM-using
    migrations don't trip on missing columns. Same pattern as v34/v35."""
    print("V36 DDL: adding per-sqm + per-sheet config columns...")
    with engine.begin() as conn:
        for table, name, defn in _COLUMN_DEFS:
            if _add_column_if_missing(conn, table, name, defn):
                print(f"  + {table}.{name}")
            else:
                print(f"  - {table}.{name} already present")


def migrate() -> None:
    print("V36: per-sq/m + per-sheet pricing strategies...")

    # 1. DDL (idempotent re-run if migrate_ddl_only already fired)
    migrate_ddl_only()

    # 2. Re-point products to new strategies + pre-fill known fields
    repointed = 0
    with engine.begin() as conn:
        for key, new_strategy, fields in _PRODUCT_UPDATES:
            row = conn.execute(
                text(
                    "SELECT id, pricing_strategy FROM products "
                    "WHERE organization_slug = :org AND key = :key"
                ),
                {"org": DEFAULT_ORG_SLUG, "key": key},
            ).fetchone()
            if not row:
                print(f"  - {key}: not found in catalog (skipping)")
                continue

            pid, current_strategy = row

            # Build SET clause from fields, omitting null updates that
            # would clobber existing data.
            updates: dict = {"pricing_strategy": new_strategy}
            for col, val in fields.items():
                # Always set False/None for manual_review_* (we WANT
                # to clear those). But skip None for other fields so
                # we don't blow away existing values.
                if col in ("manual_review_required", "manual_review_reason"):
                    updates[col] = val
                elif val is not None:
                    updates[col] = val

            # Idempotent: only re-write if the strategy is still the
            # legacy bulk_break OR if any v36 field is missing. Skip
            # if the row already looks v36-shaped.
            already_migrated = (
                current_strategy in ("per_sqm", "per_sheet")
            )
            if already_migrated:
                print(f"  - {key}: already on '{current_strategy}' (skipping)")
                continue

            set_parts = [f"{col} = :{col}" for col in updates]
            params = {**updates, "id": pid}
            conn.execute(
                text(f"UPDATE products SET {', '.join(set_parts)} WHERE id = :id"),
                params,
            )
            repointed += 1
            print(
                f"  + {key}: '{current_strategy}' -> '{new_strategy}'"
                + (f" (yield_per_sqm={fields.get('yield_per_sqm')})" if fields.get("yield_per_sqm") else "")
                + (f" (sheet_size={fields.get('sheet_size_mm')})" if fields.get("sheet_size_mm") else "")
            )

    print(f"  -> re-pointed {repointed} product(s) to v36 strategies")

    # 3. Force-reseed business_rules with v36 wording
    with db_session() as db:
        import json as _json
        rules_json = _json.dumps(BUSINESS_RULES_V36, ensure_ascii=False)
        r = _seed_setting(db, "business_rules", rules_json, force=True)
        print(f"  {r:>8}  setting business_rules (8 rules -- v36)")
        db.commit()

    print()
    print(f"v36 migration complete. {repointed} products re-pointed.")


if __name__ == "__main__":
    migrate()
