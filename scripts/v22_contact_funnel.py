"""
V22 migration — Phase E: extended customer-funnel + FAQ knowledge base.

After Justin's post-launch meeting, Roi sent a spec asking Craig to
collect richer customer info before quoting (company-vs-individual,
returning-customer email lookup, delivery-vs-collect, etc.) plus a set
of 12 FAQs Craig should answer naturally without escalating.

This migration:
  1. Adds 5 nullable columns to `conversations`:
     - is_company, is_returning_customer, past_customer_email,
       delivery_method, delivery_address
  2. Seeds setting `craig_faqs_json` with the 12 FAQs (literal copy
     from Roi's spec — already customer-ready in tone).
  3. Seeds setting `shop_address` with a placeholder until Justin
     provides his address (used for the "collect" answer).
  4. Replaces the contact-collection rule (entry #3) in
     `business_rules` with the new 5-step funnel:
        a) confirm specs → verbal price (free, no gate)
        b) ask if they want full quote
        c) name + email/whatsapp
        d) company or individual
        e) returning customer? if yes, find_past_quotes_by_email
        f) own artwork or +€65 design service?
        g) delivery vs collect (+ address if delivery)
        h) save_customer_info → [QUOTE_READY]
  5. Appends a new business rule (#5) telling Craig the FAQs are
     injected separately and to answer them inline without escalating.

Idempotent. Re-running adds nothing for columns / settings already
present, BUT will RE-SEED business_rules + faqs + shop_address EVERY
RUN if a `force_reseed=True` flag is passed (default False — protects
hand-edits Justin made via the dashboard).

Usage:
    python -m scripts.v22_contact_funnel
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
    ("is_company",             "BOOLEAN NULL"),
    ("is_returning_customer",  "BOOLEAN NULL"),
    ("past_customer_email",    "VARCHAR(200) NULL"),
    ("delivery_method",        "VARCHAR(20) NULL"),
    # JSON column — SQLite stores as TEXT, Postgres as JSON. The
    # SQLAlchemy `JSON` type abstracts this; the DDL string just needs
    # to be a permissive type. Postgres accepts TEXT; SQLite ditto.
    ("delivery_address",       "TEXT NULL"),
]


# 12 FAQs — literal from Roi's spec, ready for the LLM to paraphrase.
CRAIG_FAQS: list[dict[str, str]] = [
    {
        "q": "What products do you have?",
        "a": (
            "Print: leaflets, brochures, business cards, stationery, posters. "
            "Signage: banners, panels, vehicle graphics, exhibition stands. "
            "Tell us what it's for and we'll point you to the right product."
        ),
    },
    {
        "q": "What file formats do you accept?",
        "a": "Print-ready PDF preferred. JPG, PNG, AI, INDD also accepted.",
    },
    {
        "q": "Do you ship?",
        "a": (
            "Yes. Free shipping over €100. Under €100, shipping is calculated "
            "and added to the quote."
        ),
    },
    {
        "q": "Can I collect from your shop?",
        "a": (
            "Yes, just say so when you order. Pickup address: "
            "{{shop_address}}."
        ),
    },
    {
        "q": "How fast do I get a quote?",
        "a": (
            "If your order is a standard spec, your quote is immediate, right "
            "here in the chat. If it's something custom, we try to answer the "
            "same day. For urgent jobs, tell us how soon you need it and we'll "
            "come back fast."
        ),
    },
    {
        "q": "How long does printing take?",
        "a": (
            "Standard print turnaround is 3 to 5 working days from artwork "
            "approval. Does that work for your deadline? If you need it faster, "
            "tell us when and we'll see what's possible."
        ),
    },
    {
        "q": "Will I see a proof before you print?",
        "a": (
            "Yes. We always send a digital proof for your approval before "
            "going to press."
        ),
    },
    {
        "q": "How do I pay?",
        "a": (
            "Once you approve the quote, we send a credit card payment link. "
            "Production starts once payment confirms."
        ),
    },
    {
        "q": "Can I do a re-order from a job I did before?",
        "a": (
            "Yes. Give us your order number or the email you used and we'll "
            "pull up the spec. Confirm quantity and we go straight to a quote."
        ),
    },
    {
        "q": "What if my file isn't print-ready?",
        "a": (
            "Send what you have. If there's an issue (low resolution, wrong "
            "size, missing bleed), we'll flag it before going to print. If you "
            "need design help, that's our €65 design service (€79.95 inc VAT)."
        ),
    },
    {
        "q": "Can I cancel or change my order?",
        "a": (
            "Until you approve the proof, yes, no problem. Once it's gone to "
            "press, we can't pull it back."
        ),
    },
    {
        "q": "Can I store my designs for future orders?",
        "a": (
            "Yes. We keep your approved files on your account so you can "
            "re-order in one message."
        ),
    },
]


# Replacement business_rules array. Keeps existing #1, #2, #4 logic and
# rewrites #3 (contact collection) per Roi's 5-step spec; adds #5 (FAQs).
BUSINESS_RULES_V22 = [
    # Rule 1 — first-turn greeting
    "On the first turn, do not duplicate the widget greeting. The widget "
    "already opened with 'Hey — Craig here' (or your tenant's configured "
    "greeting). Reply with substance: ask one specific question or give "
    "a price. Never repeat 'Craig here' or 'I handle pricing'.",

    # Rule 2 — verbal price is free, inline, immediate
    "When you have all required specs (product, quantity, finish where "
    "applicable, sides), call the pricing tool IMMEDIATELY and quote the "
    "inc-VAT total in the next message. Do NOT gate the verbal price "
    "behind contact collection — the customer is just shopping at this "
    "stage and the price is non-binding until Justin approves.",

    # Rule 3 — NEW 5-step contact-collection funnel
    "After giving the verbal price, ask 'Want me to put together the full "
    "quote for you? 📋'. If they say yes, walk them through these "
    "questions ONE AT A TIME (don't dump them all at once — that's "
    "interrogation, not conversation). Confirm each answer back briefly "
    "before moving to the next:\n"
    "  (a) Name + best contact (email or WhatsApp number).\n"
    "      Validate: emails must look real (no gmial typos, no "
    "      yopmail/tempmail disposables); phones at least 8 digits.\n"
    "  (b) 'Are you ordering for a company or as an individual? "
    "      (helps with invoicing)'\n"
    "  (c) 'Have you ordered with us before? If yes, what email did you "
    "      use last time so I can link this to your account?' If yes "
    "      AND they give an email, call find_past_quotes_by_email so "
    "      you can reference past orders.\n"
    "  (d) 'Do you have print-ready artwork, or would you like us to "
    "      design it for you? Our standard design service is €65 ex "
    "      VAT (€79.95 inc VAT).' If they want design, on the NEXT "
    "      pricing tool call pass needs_artwork=true, "
    "      artwork_hours=1.0 — that's the standard flat €65 design "
    "      service. If they have artwork, omit both.\n"
    "  (e) 'Delivery to an address, or would you collect from our "
    "      shop?' If delivery → collect address (line 1 + city + "
    "      eircode minimum). If collect → tell them the shop address "
    "      from the shop_address setting and offer hours if asked.\n"
    "Once you have ALL of (a)–(e), call save_customer_info ONCE with "
    "every field you collected (name, email, phone, is_company, "
    "is_returning_customer, past_customer_email, delivery_method, "
    "delivery_address). Then end your reply with [QUOTE_READY] on its "
    "own line — the server attaches the PDF and renders the card.",

    # Rule 4 — escalations also gated
    "Escalations to Justin (escalate_to_justin tool) require contact "
    "info first — same gate as the PDF flow. If the request is custom "
    "and you need to escalate but don't have name + email/phone yet, "
    "ask for them first (without asking the company/design/delivery "
    "questions — those only apply to standard quotes), then escalate.",

    # Rule 5 — FAQ knowledge
    "A list of frequently asked questions is injected separately under "
    "## Frequently asked questions. When the customer asks one of those "
    "(or anything close), answer it INLINE in your own voice — "
    "paraphrase, don't read the canned answer verbatim. Do not escalate "
    "an FAQ to Justin. If the customer asks something not on the FAQ list "
    "and not in the catalog, then escalate as normal.",
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


def migrate(force_reseed: bool = False) -> None:
    print("V22: Phase E — contact-funnel + FAQs...")
    init_db()

    # ── 1. Schema columns ────────────────────────────────────────────
    added = 0
    with engine.begin() as conn:
        for name, defn in _COLUMN_DEFS:
            if _add_column_if_missing(conn, "conversations", name, defn):
                print(f"  + conversations.{name}")
                added += 1
            else:
                print(f"  · conversations.{name} already present")

    # ── 2-4. Setting seeds ───────────────────────────────────────────
    with db_session() as db:
        # FAQs — JSON-encoded list
        faqs_json = json.dumps(CRAIG_FAQS, ensure_ascii=False)
        r = _seed_setting(db, "craig_faqs_json", faqs_json, force=force_reseed)
        print(f"  {r:>8}  setting craig_faqs_json ({len(CRAIG_FAQS)} entries)")

        # Shop address placeholder
        r = _seed_setting(
            db, "shop_address",
            "TBD — pending Justin (used for the 'collect from shop' answer)",
            force=force_reseed,
        )
        print(f"  {r:>8}  setting shop_address (placeholder)")

        # Replace business_rules wholesale (this one IS force-reseeded
        # because it's prompt logic, not customer-tweakable copy).
        # Justin can still hand-edit the JSON in the Settings tab if needed.
        rules_json = json.dumps(BUSINESS_RULES_V22, ensure_ascii=False)
        r = _seed_setting(db, "business_rules", rules_json, force=True)
        print(f"  {r:>8}  setting business_rules ({len(BUSINESS_RULES_V22)} rules — force-reseeded)")

        db.commit()

    print()
    print(f"✓ {added} columns added.")


if __name__ == "__main__":
    migrate()
