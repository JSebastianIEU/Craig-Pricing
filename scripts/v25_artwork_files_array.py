"""
V25 migration — Phase G: multi-file artwork + flow tightening.

Justin's smoke of v24 surfaced these issues. v25 fixes them:

  1. Artwork upload was single-file only. Now `quotes.artwork_files`
     is a JSON array — each customer can upload multiple files (front
     + back PDFs, design + reference, etc.). Cap 10 files (Missive's
     attachment limit). Backfills the existing singular columns into
     the array on first run.

  2. business_rules force-reseeded with stricter Rule #2 — the
     artwork question MUST be its own message. DeepSeek was bundling
     it with spec confirmation OR with "want full quote?" so
     customers were seeing all three at once and skipping the
     artwork answer.

Idempotent. Safe to re-run.

Usage:
    python -m scripts.v25_artwork_files_array
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import inspect, text

from db import db_session, engine, init_db
from db.models import DEFAULT_ORG_SLUG, Quote, Setting


_COLUMN_DEFS = [
    ("quotes", "artwork_files", "TEXT NULL"),
]


# Phase G business_rules — force-reseeded. The big change is Rule #2
# (artwork isolation) — uncompromisingly clear that the artwork
# question is its OWN message, with no spec recap and no "full quote"
# prompt fused in.
BUSINESS_RULES_V25 = [
    # Rule 1 — first-turn greeting
    "On the first turn, do not duplicate the widget greeting. Reply "
    "with substance: ask one specific question or give a price. "
    "Never repeat 'Craig here' or 'I handle pricing'.",

    # Rule 2 — ARTWORK QUESTION IS ITS OWN MESSAGE
    "Once you have all required specs (product, quantity, finish, "
    "sides), DO NOT call the pricing tool, DO NOT confirm specs in "
    "this message, DO NOT mention 'full quote'. Your VERY NEXT "
    "message must be ONLY this question, nothing else:\n"
    "\n"
    "  'Got it. Quick question before I price it: do you have your "
    "  own print-ready artwork, or would you like our design service "
    "  (€65 ex VAT, €79.95 inc — flat per-order fee)?'\n"
    "\n"
    "After you ask, WAIT for the customer's answer. Only on the NEXT "
    "turn do you call the pricing tool with the right needs_artwork "
    "value:\n"
    "  - artwork=true  -> needs_artwork=false (no design line item)\n"
    "  - design=true   -> needs_artwork=true, artwork_hours=1.0\n"
    "\n"
    "CRITICAL: the design service is FLAT per-order, NOT per hour. "
    "NEVER say 'per hour', '/hr', 'hourly'.\n"
    "\n"
    "Bundling the artwork question with anything else (spec recap, "
    "'want full quote?', the price itself) is a contractual breach. "
    "The widget renders an upload button for the customer right after "
    "this question — bundling other content makes that button render "
    "in the wrong place.",

    # Rule 3 — verbal price + offer full quote
    "AFTER the customer answers the artwork question (and you've "
    "captured it), call the pricing tool. Quote the inc-VAT total in "
    "plain text. If the customer said they HAVE artwork, end the "
    "message with [ARTWORK_UPLOAD] on its own line so the widget "
    "renders the upload button. Then ask 'Want me to put together "
    "the full quote? 📋'. The verbal price is free and non-binding.",

    # Rule 4 — full-quote ask + form trigger
    "When the customer says yes to 'want full quote?', emit "
    "[CUSTOMER_FORM] on its own line. The widget renders the "
    "structured form (name, email, company/individual, returning?, "
    "delivery method, address). Do NOT ask these questions in free "
    "text — the form has validation.",

    # Rule 5 — post-form acknowledgement (rarely fires — server short-
    # circuits, but kept as fallback)
    "If you receive a system message saying the customer submitted "
    "the form, reply briefly — 'All set 👍' — and emit [QUOTE_READY] "
    "on its own line. Do NOT re-quote — the customer already saw it.",

    # Rule 6 — escalations gated on contact
    "Escalations to Justin (escalate_to_justin tool) require contact "
    "info first. If you need to escalate but don't have name + "
    "email/phone, ask for them first, save_customer_info, then "
    "escalate.",

    # Rule 7 — FAQ knowledge inline
    "A list of frequently asked questions is injected separately under "
    "## Frequently asked questions. When the customer asks one (or "
    "anything close), answer it INLINE in your own voice — paraphrase, "
    "don't read verbatim. Do not escalate FAQs.",

    # Rule 8 — Ireland-only
    "Just Print operates in Ireland only. If a customer asks for "
    "delivery outside Ireland, politely tell them we only ship within "
    "Ireland and offer collection from our Ballymount shop. Don't "
    "proceed with the quote until they confirm an Irish address or "
    "pick collection.",
]


def _column_exists(conn, table: str, column: str) -> bool:
    return column in [c["name"] for c in inspect(conn).get_columns(table)]


def _add_column_if_missing(conn, table: str, column_name: str, column_def: str) -> bool:
    if _column_exists(conn, table, column_name):
        return False
    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column_name} {column_def}"))
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


def migrate() -> None:
    print("V25: Phase G — multi-file artwork + flow tightening...")
    init_db()

    # ── 1. Schema ────────────────────────────────────────────────────
    added = 0
    with engine.begin() as conn:
        for table, name, defn in _COLUMN_DEFS:
            if _add_column_if_missing(conn, table, name, defn):
                print(f"  + {table}.{name}")
                added += 1
            else:
                print(f"  · {table}.{name} already present")

    # ── 2. Backfill: turn singular cols into a 1-element array on
    #               existing quotes that have an artwork_file_url but
    #               no artwork_files JSON yet.
    backfilled = 0
    with db_session() as db:
        rows = (
            db.query(Quote)
            .filter(Quote.artwork_file_url.isnot(None))
            .filter((Quote.artwork_files.is_(None)) | (Quote.artwork_files == ""))
            .all()
        )
        for q in rows:
            q.artwork_files = [{
                "url": q.artwork_file_url,
                "filename": q.artwork_file_name or "artwork",
                "size": q.artwork_file_size or 0,
                "content_type": "application/octet-stream",
                "uploaded_at": None,
            }]
            backfilled += 1
        if backfilled:
            print(f"  + backfilled {backfilled} quote(s) into artwork_files array")

        # ── 3. Force-reseed business_rules ──────────────────────────
        rules_json = json.dumps(BUSINESS_RULES_V25, ensure_ascii=False)
        r = _seed_setting(db, "business_rules", rules_json, force=True)
        print(f"  {r:>8}  setting business_rules ({len(BUSINESS_RULES_V25)} rules — force-reseeded)")

        db.commit()

    print()
    print(f"✓ {added} columns added, {backfilled} quotes backfilled.")


if __name__ == "__main__":
    migrate()
