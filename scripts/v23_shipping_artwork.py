"""
V23 migration — Phase F: shipping + artwork upload + interactive form.

Three coordinated changes triggered by Justin's post-meeting feedback:

  (a) Shipping policy: €15 inc VAT flat fee for "Just Print Delivery",
      free over €100 inc VAT goods total. Two new Quote columns +
      tenant settings.
  (b) Artwork upload: customer uploads print-ready file via chat
      widget; URL persists on Quote, attaches to Missive draft, lands
      on PrintLogic order in custom_data. Three new Quote columns.
  (c) Real shop address for "Collection" delivery method (replaces the
      placeholder seeded by v22): "Ballymount Cross Business Park, 7,
      Ballymount, Dublin, D24 E5NH, Ireland". Setting `shop_address` is
      force-reseeded.

Also force-reseeds business_rules for the new Phase F flow:
  - Craig MUST ask the artwork question explicitly BEFORE pricing
  - When customer has artwork, Craig emits [ARTWORK_UPLOAD] marker so
    widget renders the upload button
  - When the funnel needs to be collected, Craig emits [CUSTOMER_FORM]
    marker so widget renders the structured form (no more free-text
    Q&A — too unpredictable per smoke tests)

Idempotent. Re-runs preserve existing column values (so a quote that
already has shipping computed keeps it). business_rules + shop_address
are force-reseeded each run because they're prompt logic, not customer
copy.

Usage:
    python -m scripts.v23_shipping_artwork
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import inspect, text

from db import db_session, engine, init_db
from db.models import DEFAULT_ORG_SLUG, Setting


_COLUMN_DEFS = [
    # (table, name, sql)
    ("quotes", "shipping_cost_ex_vat",  "FLOAT NOT NULL DEFAULT 0"),
    ("quotes", "shipping_cost_inc_vat", "FLOAT NOT NULL DEFAULT 0"),
    ("quotes", "artwork_file_url",      "TEXT NULL"),
    ("quotes", "artwork_file_name",     "VARCHAR(255) NULL"),
    ("quotes", "artwork_file_size",     "INTEGER NULL"),
]


# Phase F business_rules — force-reseeded each run (it's prompt logic).
# Replaces the v22 set wholesale. Key changes from v22:
#   - Rule #3 now asks artwork BEFORE pricing
#   - Rule #6 added: emit [CUSTOMER_FORM] for structured funnel collection
#   - Rule #7 added: emit [ARTWORK_UPLOAD] when customer has own artwork
BUSINESS_RULES_V23 = [
    # Rule 1 — first-turn greeting
    "On the first turn, do not duplicate the widget greeting. The widget "
    "already opened with 'Hey — Craig here' (or your tenant's configured "
    "greeting). Reply with substance: ask one specific question or give "
    "a price. Never repeat 'Craig here' or 'I handle pricing'.",

    # Rule 2 — verbal price is free, inline, immediate
    "Verbal prices are non-binding and free to give. As soon as you have "
    "the required specs (product, quantity, finish where applicable, sides) "
    "AND know whether the customer needs design service, call the pricing "
    "tool and quote the inc-VAT total in plain text. Don't gate the verbal "
    "price behind contact collection.",

    # Rule 3 — REWRITTEN: ask artwork BEFORE pricing
    "Before calling any pricing tool, you MUST know whether the customer "
    "has print-ready artwork or needs our design service. If they haven't "
    "told you, ask in the same message you confirm the specs back, e.g.: "
    "'Just to confirm — 500 business cards, single-sided, gloss? And do "
    "you have print-ready artwork, or would you like our design service "
    "(€65 ex VAT, €79.95 inc)?' Once they answer:\n"
    "  - If they HAVE artwork: pass needs_artwork=false to the pricing "
    "    tool. After giving the verbal price, end your reply with the "
    "    marker [ARTWORK_UPLOAD] on its own line — the widget will show "
    "    them an upload button. They MUST upload before the quote is "
    "    finalized (server enforces this gate).\n"
    "  - If they NEED design: pass needs_artwork=true, artwork_hours=1.0 "
    "    to the pricing tool — that's the standard flat €65 ex VAT design "
    "    service, applied to the quote total. Don't emit [ARTWORK_UPLOAD].",

    # Rule 4 — full-quote ask + form trigger
    "After giving the verbal price, ask 'Want me to put together the full "
    "quote for you? 📋'. If the customer says yes, do NOT ask the funnel "
    "questions (company/individual, returning, delivery) one by one in "
    "free text. Instead emit [CUSTOMER_FORM] on its own line. The widget "
    "will render an interactive form with validation that collects "
    "everything in one go. After the form submits, the server runs "
    "save_customer_info on your behalf — your job is just to emit the "
    "marker.",

    # Rule 5 — post-form acknowledgement
    "When the user submits the form (you'll see a system message saying "
    "the form was submitted with the captured fields), reply briefly "
    "acknowledging — e.g. 'Got everything 👍 here's your quote 📋' — and "
    "end with [QUOTE_READY] on its own line. The PDF card will render. "
    "Do NOT re-quote or re-ask anything that was on the form.",

    # Rule 6 — escalations also gated
    "Escalations to Justin (escalate_to_justin tool) require contact info "
    "first — same gate as the PDF flow. If the request is custom and you "
    "need to escalate but don't have name + email/phone yet, ask for them "
    "first (no need for the company/design/delivery questions — those only "
    "apply to standard quotes), then escalate.",

    # Rule 7 — FAQ knowledge
    "A list of frequently asked questions is injected separately under "
    "## Frequently asked questions. When the customer asks one of those "
    "(or anything close), answer it INLINE in your own voice — paraphrase, "
    "don't read the canned answer verbatim. Do not escalate an FAQ to "
    "Justin. If the customer asks something not on the FAQ list and not in "
    "the catalog, then escalate as normal.",

    # Rule 8 — Ireland-only
    "Just Print operates in Ireland only. If a customer asks for delivery "
    "outside Ireland (UK, EU, US, anywhere else), politely tell them we "
    "currently only ship within Ireland and offer collection from our "
    "shop as an alternative. Don't proceed with the quote until they "
    "confirm an Irish address or pick collection.",
]


def _column_exists(conn, table: str, column: str) -> bool:
    return column in [c["name"] for c in inspect(conn).get_columns(table)]


def _add_column_if_missing(conn, table: str, column_name: str, column_def: str) -> bool:
    if _column_exists(conn, table, column_name):
        return False
    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column_name} {column_def}"))
    return True


def _seed_setting(db, key: str, value: str, *, force: bool = False) -> str:
    """Return 'added' / 'skipped' / 'updated'."""
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


def migrate() -> None:
    print("V23: Phase F — shipping + artwork + form-mode...")
    init_db()

    # ── 1. Schema columns ────────────────────────────────────────────
    added = 0
    with engine.begin() as conn:
        for table, name, defn in _COLUMN_DEFS:
            if _add_column_if_missing(conn, table, name, defn):
                print(f"  + {table}.{name}")
                added += 1
            else:
                print(f"  · {table}.{name} already present")

    # ── 2. Settings ──────────────────────────────────────────────────
    with db_session() as db:
        # Real shop address — replaces v22 placeholder, force-reseeded
        # because the placeholder is wrong copy that the LLM injects.
        r = _seed_setting(
            db, "shop_address",
            "Ballymount Cross Business Park, 7, Ballymount, Dublin, D24 E5NH, Ireland",
            force=True,
        )
        print(f"  {r:>8}  setting shop_address (real address — force-reseeded)")

        # Shipping policy
        r = _seed_setting(db, "shipping_fee_inc_vat", "15.00", force=False)
        print(f"  {r:>8}  setting shipping_fee_inc_vat = 15.00")
        r = _seed_setting(db, "free_shipping_threshold_inc_vat", "100.00", force=False)
        print(f"  {r:>8}  setting free_shipping_threshold_inc_vat = 100.00")

        # business_rules force-reseeded with the Phase F set
        rules_json = json.dumps(BUSINESS_RULES_V23, ensure_ascii=False)
        r = _seed_setting(db, "business_rules", rules_json, force=True)
        print(f"  {r:>8}  setting business_rules ({len(BUSINESS_RULES_V23)} rules — force-reseeded)")

        db.commit()

    print()
    print(f"✓ {added} columns added.")


if __name__ == "__main__":
    migrate()
