"""
V37 migration -- engagement-approval gate for inbound Missive emails.

Why this exists: pre-v37 the Missive webhook used a binary classifier
(is_quote_inquiry true/false). Anything True drafted Craig and the
Missive draft auto-sent. Justin had to apologise to a customer because
Craig replied to an email that wasn't a quote request.

v37 adds a confidence score to the classifier and a third tier:

  >= engagement_confidence_threshold (default 0.85)  -> Craig responds
  >= LOW_CONFIDENCE_FLOOR (0.2) but < threshold     -> pause + notify
                                                       Justin to approve
  < LOW_CONFIDENCE_FLOOR                             -> silent drop

This migration:

  1. Adds 1 nullable Conversation column:
       engagement_classification (JSONB on Postgres / TEXT on SQLite)
     Stores the classifier verdict + audit fields (notification_sent_at,
     approved_at, rejected_at, etc.) so the dashboard + Justin's email
     have everything without a separate table.

  2. Seeds the per-tenant Setting `engagement_confidence_threshold`
     with the default value 0.85. Idempotent -- doesn't clobber
     operator overrides.

Idempotent. Safe to re-run.

Usage:
    python -m scripts.v37_engagement_approval
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import inspect, text

from db import db_session, engine
from db.models import DEFAULT_ORG_SLUG, Setting


# ---------------------------------------------------------------------------
# Schema -- one new nullable JSON column on conversations
# ---------------------------------------------------------------------------


_COLUMN_DEFS = [
    ("conversations", "engagement_classification", "JSONB NULL"),
]


# ---------------------------------------------------------------------------
# Helpers (mirror v36 pattern)
# ---------------------------------------------------------------------------


def _is_postgres() -> bool:
    return engine.url.drivername.startswith("postgresql")


def _column_exists(conn, table: str, column: str) -> bool:
    return column in [c["name"] for c in inspect(conn).get_columns(table)]


def _add_column_if_missing(conn, table: str, name: str, defn: str) -> bool:
    if _column_exists(conn, table, name):
        return False
    if not _is_postgres():
        # On SQLite the JSON type is stored as TEXT.
        defn = (
            defn.replace("JSONB", "TEXT")
                .replace("TIMESTAMP", "DATETIME")
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
        key=key, value=value, value_type="float",
    ))
    return "added"


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------


def migrate_ddl_only() -> None:
    """v37 DDL only -- runs early in startup so older ORM-using
    migrations don't trip on the missing column. Same pattern as v34/v35/v36."""
    print("V37 DDL: adding engagement_classification column...")
    with engine.begin() as conn:
        for table, name, defn in _COLUMN_DEFS:
            if _add_column_if_missing(conn, table, name, defn):
                print(f"  + {table}.{name}")
            else:
                print(f"  - {table}.{name} already present")


def migrate() -> None:
    print("V37: engagement-approval gate...")

    # 1. DDL (idempotent re-run if migrate_ddl_only already fired)
    migrate_ddl_only()

    # 2. Seed the per-tenant confidence threshold (default 0.85).
    # Operators can override per-tenant from the dashboard Settings tab.
    with db_session() as db:
        r = _seed_setting(db, "engagement_confidence_threshold", "0.85")
        print(f"  {r:>8}  setting engagement_confidence_threshold = 0.85")
        db.commit()

    # 3. v36 cleanup: clear manual_review_required + manual_review_reason
    # on the 6 per-sqm + 3 per-sheet products. v36's migration had an
    # edge: if the product's pricing_strategy was already 'per_sqm' /
    # 'per_sheet' (because of a partial earlier run, or because it was
    # seeded that way by a fresh deploy), the idempotency guard
    # SKIPPED the whole UPDATE — including the line that should have
    # cleared the v34 manual_review flag. Result: production has
    # vinyl_labels with pricing_strategy='per_sqm' AND
    # manual_review_required=True, so quote_large_format() short-
    # circuits to manual_review on EVERY request and never reaches
    # _quote_per_sqm. This step force-clears the flag for products
    # that have a v36 strategy. Idempotent — safe to re-run.
    _PER_SQM_OR_SHEET_PRODUCT_KEYS = (
        "vinyl_labels", "pvc_banners", "mesh_banners",
        "window_graphics", "floor_graphics", "fabric_displays",
        "foamex_boards", "dibond_boards", "corri_boards",
    )
    cleared = 0
    with engine.begin() as conn:
        for key in _PER_SQM_OR_SHEET_PRODUCT_KEYS:
            row = conn.execute(
                text(
                    "SELECT id, pricing_strategy, manual_review_required "
                    "FROM products WHERE key = :key"
                ),
                {"key": key},
            ).fetchone()
            if not row:
                continue
            pid, strategy, mr_required = row
            if strategy not in ("per_sqm", "per_sheet"):
                continue
            if not mr_required:
                continue
            conn.execute(
                text(
                    "UPDATE products SET "
                    "manual_review_required = :mrr, "
                    "manual_review_reason = :mrn "
                    "WHERE id = :id"
                ),
                {"mrr": False, "mrn": None, "id": pid},
            )
            cleared += 1
            print(f"  + {key}: manual_review flag cleared (strategy={strategy})")
    print(f"  -> cleared manual_review on {cleared} per-sqm/per-sheet product(s)")

    print()
    print("v37 migration complete.")


if __name__ == "__main__":
    migrate()
