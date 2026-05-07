"""
V33 migration — auto-send the binding quote, dashboard-only approval,
operator notifications, lifecycle UI.

Justin's ask (May 2026): customer-facing flow should be 100% automatic
up to the moment the customer commits (form submit on widget /
"yes confirm" on email). Justin's only intervention is the dashboard
Approve button. He gets an email notification the moment a quote
enters pending_approval so he doesn't have to refresh the dashboard.

This migration:
  1. Adds three Quote columns for the operator notification (Resend
     message id + sent timestamp + last error) and one column for
     `approved_at` (the existing `approved_by` had no timestamp).
  2. Force-seeds the new operator-notification settings:
        notifications_enabled         = "true"
        notification_sender_address   = "craig@notifications.strategos-ai.com"
        notification_sender_name      = "Craig (Just Print)"
        notification_to_address       = "info@just-print.ie"
        dashboard_base_url            = "<deployment URL>"
  3. Force-reseeds business_rules with the v33 wording (kills the
     v32 "STEP 4 PDF is a draft for Justin to send" language — it's
     now auto-sent).

Idempotent. Safe to re-run.

Usage:
    python -m scripts.v33_auto_send_and_notifications
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import inspect, text

from db import db_session, engine
from db.models import DEFAULT_ORG_SLUG, Setting


_COLUMN_DEFS = [
    ("quotes", "approved_at", "TIMESTAMP NULL"),
    ("quotes", "notification_sent_at", "TIMESTAMP NULL"),
    ("quotes", "notification_message_id", "VARCHAR(128) NULL"),
    ("quotes", "notification_last_error", "TEXT NULL"),
]


# v33 business rules — explicit "STEP 4 PDF is auto-sent, no draft".
# v32 had wording that said "the only one drafted for Justin's review"
# — kill that. The customer now sees the PDF the moment specs+artwork+
# funnel are all in. Justin's intervention moves to the dashboard.
BUSINESS_RULES_V33 = [
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
]


def _column_exists(conn, table: str, column: str) -> bool:
    return column in [c["name"] for c in inspect(conn).get_columns(table)]


def _add_column_if_missing(conn, table: str, name: str, defn: str) -> bool:
    if _column_exists(conn, table, name):
        return False
    if engine.url.drivername.startswith("sqlite"):
        # SQLite doesn't support TIMESTAMP keyword the same way — DATETIME is fine
        defn = defn.replace("TIMESTAMP", "DATETIME")
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


def migrate() -> None:
    print("V33: dashboard-only approval + operator notifications + lifecycle...")

    # ── 1. Schema ──────────────────────────────────────────────────────
    added = 0
    with engine.begin() as conn:
        for table, name, defn in _COLUMN_DEFS:
            if _add_column_if_missing(conn, table, name, defn):
                print(f"  + {table}.{name}")
                added += 1
            else:
                print(f"  · {table}.{name} already present")
    if not added:
        print("  · no schema changes needed (already up to date)")

    # ── 2. Settings seeds ─────────────────────────────────────────────
    with db_session() as db:
        # notifications_enabled defaults to true; force so existing orgs pick it up
        r = _seed_setting(db, "notifications_enabled", "true", force=True)
        print(f"  {r:>8}  notifications_enabled='true'")

        # Sender + recipient — force-seed so the runbook's expected
        # values land. Justin / operator can override via the settings
        # PATCH endpoint at any time.
        r = _seed_setting(
            db, "notification_sender_address",
            "craig@notifications.strategos-ai.com",
            force=False,  # don't overwrite if operator already set their own
        )
        print(f"  {r:>8}  notification_sender_address")
        r = _seed_setting(
            db, "notification_sender_name",
            "Craig (Just Print)",
            force=False,
        )
        print(f"  {r:>8}  notification_sender_name")
        r = _seed_setting(
            db, "notification_to_address",
            "info@just-print.ie",
            force=False,
        )
        print(f"  {r:>8}  notification_to_address='info@just-print.ie'")
        r = _seed_setting(
            db, "dashboard_base_url",
            "https://strategos-dashboard.vercel.app",
            force=False,
        )
        print(f"  {r:>8}  dashboard_base_url")

        # ── 3. Force-reseed business_rules with v33 wording ─────────
        rules_json = json.dumps(BUSINESS_RULES_V33, ensure_ascii=False)
        r = _seed_setting(db, "business_rules", rules_json, force=True)
        print(
            f"  {r:>8}  business_rules ({len(BUSINESS_RULES_V33)} rules — "
            f"force-reseeded for v33)"
        )


if __name__ == "__main__":
    migrate()
