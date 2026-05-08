"""
V35 migration — test-chat sandbox + customer issue reporting + admin alerts.

JS asked (May 2026) for three related capabilities so testing Craig is
safer + ops feedback is more transparent:

  1. Test chat in the dashboard — a sandboxed conversation mode that
     skips the funnel (no artwork question, no contact form, no
     delivery prompt). Conversations marked is_test=True don't show
     up in the regular Conversations module so JS / Justin can play
     with the bot without polluting real customer data.

  2. "Report an issue" link in the widget footer — when the customer
     clicks it, a modal lets them describe what went wrong. We persist
     it as an IssueReport row and email the admin (sebastian@strategos-ai.com
     by default). The customer sees a friendly canned reply.

  3. Admin alerts on:
       - issue reports (above)
       - Justin flagging a price wrong in Pricing Verification
       - Justin commenting on a price row in Pricing Verification

This migration:
  - Adds Conversation.is_test + Quote.is_test (boolean flags)
  - Creates issue_reports table
  - Seeds settings:
      admin_alert_email = sebastian@strategos-ai.com
      admin_alert_subject_prefix = [Strategos]

Idempotent. Safe to re-run.

Usage:
    python -m scripts.v35_test_chat_and_issue_reports
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import inspect, text

from db import db_session, engine
from db.models import DEFAULT_ORG_SLUG, Setting


_COLUMN_DEFS = [
    # Test-mode flags. Same DEFAULT FALSE pattern as v34 — Postgres
    # rejects integer literals on boolean columns, so the DDL helper
    # rewrites for SQLite.
    ("conversations", "is_test", "BOOLEAN NOT NULL DEFAULT FALSE"),
    ("quotes", "is_test", "BOOLEAN NOT NULL DEFAULT FALSE"),
]


def _is_postgres() -> bool:
    return engine.url.drivername.startswith("postgresql")


def _column_exists(conn, table: str, column: str) -> bool:
    return column in [c["name"] for c in inspect(conn).get_columns(table)]


def _table_exists(conn, table: str) -> bool:
    return table in inspect(conn).get_table_names()


def _add_column_if_missing(conn, table: str, name: str, defn: str) -> bool:
    if _column_exists(conn, table, name):
        return False
    if _is_postgres():
        pass  # leave FALSE/TRUE/JSONB/TIMESTAMP as-is
    else:
        defn = (
            defn.replace("TIMESTAMP", "DATETIME")
                .replace("JSONB", "TEXT")
                .replace("DEFAULT FALSE", "DEFAULT 0")
                .replace("DEFAULT TRUE", "DEFAULT 1")
        )
    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {defn}"))
    return True


def _create_issue_reports(conn) -> bool:
    """Create the new issue_reports table. Returns True if created."""
    if _table_exists(conn, "issue_reports"):
        return False

    if _is_postgres():
        ddl = """
            CREATE TABLE issue_reports (
                id                       SERIAL PRIMARY KEY,
                organization_slug        VARCHAR(80) NOT NULL,
                conversation_id          INTEGER REFERENCES conversations(id),
                customer_email           VARCHAR(200) NULL,
                customer_name            VARCHAR(200) NULL,
                channel                  VARCHAR(30) NULL,
                message                  TEXT NOT NULL,
                status                   VARCHAR(30) NOT NULL DEFAULT 'open',
                reviewed_by              VARCHAR(120) NULL,
                reviewed_at              TIMESTAMP NULL,
                resolution_notes         TEXT NULL,
                notification_sent_at     TIMESTAMP NULL,
                notification_message_id  VARCHAR(128) NULL,
                notification_last_error  TEXT NULL,
                created_at               TIMESTAMP NOT NULL DEFAULT now(),
                updated_at               TIMESTAMP NOT NULL DEFAULT now()
            );
            CREATE INDEX ix_issue_reports_org_status
                ON issue_reports (organization_slug, status);
            CREATE INDEX ix_issue_reports_org_created
                ON issue_reports (organization_slug, created_at);
            CREATE INDEX ix_issue_reports_conversation_id
                ON issue_reports (conversation_id);
        """
    else:
        ddl = """
            CREATE TABLE issue_reports (
                id                       INTEGER PRIMARY KEY AUTOINCREMENT,
                organization_slug        VARCHAR(80) NOT NULL,
                conversation_id          INTEGER REFERENCES conversations(id),
                customer_email           VARCHAR(200) NULL,
                customer_name            VARCHAR(200) NULL,
                channel                  VARCHAR(30) NULL,
                message                  TEXT NOT NULL,
                status                   VARCHAR(30) NOT NULL DEFAULT 'open',
                reviewed_by              VARCHAR(120) NULL,
                reviewed_at              DATETIME NULL,
                resolution_notes         TEXT NULL,
                notification_sent_at     DATETIME NULL,
                notification_message_id  VARCHAR(128) NULL,
                notification_last_error  TEXT NULL,
                created_at               DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at               DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX ix_issue_reports_org_status
                ON issue_reports (organization_slug, status);
            CREATE INDEX ix_issue_reports_org_created
                ON issue_reports (organization_slug, created_at);
            CREATE INDEX ix_issue_reports_conversation_id
                ON issue_reports (conversation_id);
        """
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


def migrate_ddl_only() -> None:
    """v35 DDL only — extracted so it can run BEFORE other ORM-using
    migrations on subsequent deploys, same pattern as v34_ddl_only.

    Idempotent — column-existence + table-existence checks make repeats
    a no-op."""
    print("V35 DDL: column + issue_reports table creation...")
    with engine.begin() as conn:
        for table, name, defn in _COLUMN_DEFS:
            if _add_column_if_missing(conn, table, name, defn):
                print(f"  + {table}.{name}")
            else:
                print(f"  - {table}.{name} already present")
        if _create_issue_reports(conn):
            print("  + issue_reports table created")
        else:
            print("  - issue_reports already exists")


def migrate() -> None:
    print("V35: test-chat sandbox + customer issue reports + admin alerts...")

    # ── 1. Schema (idempotent — DDL helpers skip when columns exist) ──
    migrate_ddl_only()

    # ── 2. Settings ──────────────────────────────────────────────────
    with db_session() as db:
        r1 = _seed_setting(
            db, "admin_alert_email", "sebastian@strategos-ai.com", force=False,
        )
        print(f"  {r1:>8}  setting admin_alert_email")

        r2 = _seed_setting(
            db, "admin_alert_subject_prefix", "[Strategos]", force=False,
        )
        print(f"  {r2:>8}  setting admin_alert_subject_prefix")

        db.commit()

    print()
    print("v35 migration complete.")


if __name__ == "__main__":
    migrate()
