"""
Compute "is this integration green/yellow/red?" health summaries per tenant.

Read-only. Pulls from existing tables — no new audit table needed:
  - `Setting` rows for the *_enabled / api_key / dry_run flags
  - `Quote` rows for the printlogic_* and stripe_* timestamp / error columns
  - `Conversation` rows scoped to channel='missive' for Missive activity

The shape returned is consumed by:
  - `GET /admin/api/orgs/:slug/integrations/status` (admin endpoint)
  - The dashboard's IntegrationsHealthCard on the Overview tab

Design notes:
  - Pure function on (db, organization_slug). No side effects, no caching.
  - Health levels:
      "green"   — configured, enabled, recent successful activity
      "yellow"  — configured but not yet activated, OR safety mode
                  still on (printlogic_dry_run=true)
      "red"    — enabled but errors dominating recent activity
      "unknown" — not configured / disabled by tenant
  - 30-day window for activity counters; 7-day window for "recent success".
    Tunable via constants below.
"""

from __future__ import annotations

import datetime as _dt
from typing import Any

from sqlalchemy import and_, func
from sqlalchemy.orm import Session

from db.models import Conversation, Quote, Setting


ACTIVITY_WINDOW_DAYS = 30
RECENT_SUCCESS_DAYS = 7


# =============================================================================
# Helpers
# =============================================================================


def _truthy(val: str | None) -> bool:
    if not val:
        return False
    return val.strip().lower() in ("true", "1", "yes", "on")


def _setting(db: Session, organization_slug: str, key: str) -> str | None:
    """Read a Setting value, transparently decrypting secret-keyed rows."""
    from secrets_crypto import decrypt
    row = (
        db.query(Setting)
        .filter_by(organization_slug=organization_slug, key=key)
        .first()
    )
    if row is None:
        return None
    return decrypt(row.value)


def _now() -> _dt.datetime:
    """Indirection so tests can monkeypatch the clock."""
    return _dt.datetime.utcnow()


def _iso(dt: _dt.datetime | None) -> str | None:
    return dt.isoformat(timespec="seconds") if dt else None


# =============================================================================
# Per-integration scorers
# =============================================================================


def _printlogic_status(db: Session, organization_slug: str) -> dict:
    api_key = _setting(db, organization_slug, "printlogic_api_key") or ""
    dry_run = _truthy(_setting(db, organization_slug, "printlogic_dry_run"))
    configured = bool(api_key)
    # No explicit "printlogic_enabled" today — having an api_key + not in
    # dry_run is the de-facto "live" signal.
    enabled = configured and not dry_run

    cutoff = _now() - _dt.timedelta(days=ACTIVITY_WINDOW_DAYS)
    base = db.query(Quote).filter(
        Quote.organization_slug == organization_slug,
        Quote.created_at >= cutoff,
    )

    successes = base.filter(Quote.printlogic_pushed_at.isnot(None)).count()
    failures = base.filter(Quote.printlogic_last_error.isnot(None)).count()

    last_success_row = (
        db.query(Quote.printlogic_pushed_at)
        .filter(
            Quote.organization_slug == organization_slug,
            Quote.printlogic_pushed_at.isnot(None),
        )
        .order_by(Quote.printlogic_pushed_at.desc())
        .first()
    )
    last_success_at = last_success_row[0] if last_success_row else None

    last_error_row = (
        db.query(Quote.printlogic_last_error, Quote.created_at)
        .filter(
            Quote.organization_slug == organization_slug,
            Quote.printlogic_last_error.isnot(None),
        )
        .order_by(Quote.created_at.desc())
        .first()
    )
    last_error = last_error_row[0] if last_error_row else None
    last_error_at = last_error_row[1] if last_error_row else None

    # Health verdict
    notes = None
    if not configured:
        health = "unknown"
        notes = "Paste a PrintLogic API key in Connections → PrintLogic."
    elif dry_run:
        health = "yellow"
        notes = "Dry-run mode — pushes return synthetic DRY-xxxx ids without hitting PrintLogic. Flip after Stage 3 ceremony."
    else:
        recent_cutoff = _now() - _dt.timedelta(days=RECENT_SUCCESS_DAYS)
        had_recent_success = last_success_at and last_success_at >= recent_cutoff
        if successes == 0 and failures > 0:
            health = "red"
            notes = "All recent pushes failed. Check the api_key and run probe_printlogic."
        elif had_recent_success:
            health = "green"
        else:
            health = "yellow"
            notes = "No successful pushes in the last 7 days."

    return {
        "configured": configured,
        "enabled": enabled,
        "health": health,
        "last_success_at": _iso(last_success_at),
        "last_error": last_error,
        "last_error_at": _iso(last_error_at),
        "stats_30d": {"successes": successes, "failures": failures},
        "dry_run": dry_run,
        "notes": notes,
    }


def _stripe_status(db: Session, organization_slug: str) -> dict:
    api_key = _setting(db, organization_slug, "stripe_secret_key") or ""
    webhook_secret = _setting(db, organization_slug, "stripe_webhook_secret") or ""
    enabled = _truthy(_setting(db, organization_slug, "stripe_enabled"))
    configured = bool(api_key) and bool(webhook_secret)

    cutoff = _now() - _dt.timedelta(days=ACTIVITY_WINDOW_DAYS)
    base = db.query(Quote).filter(
        Quote.organization_slug == organization_slug,
        Quote.created_at >= cutoff,
    )

    links_created = base.filter(Quote.stripe_payment_link_id.isnot(None)).count()
    paid = base.filter(Quote.stripe_payment_status == "paid").count()
    failed = base.filter(Quote.stripe_last_error.isnot(None)).count()

    last_paid_row = (
        db.query(Quote.stripe_paid_at)
        .filter(
            Quote.organization_slug == organization_slug,
            Quote.stripe_paid_at.isnot(None),
        )
        .order_by(Quote.stripe_paid_at.desc())
        .first()
    )
    last_paid_at = last_paid_row[0] if last_paid_row else None

    last_error_row = (
        db.query(Quote.stripe_last_error, Quote.created_at)
        .filter(
            Quote.organization_slug == organization_slug,
            Quote.stripe_last_error.isnot(None),
        )
        .order_by(Quote.created_at.desc())
        .first()
    )
    last_error = last_error_row[0] if last_error_row else None
    last_error_at = last_error_row[1] if last_error_row else None

    notes = None
    if not enabled:
        health = "unknown"
        notes = "Disabled. Paste sk_*** + whsec_*** and flip stripe_enabled."
    elif not configured:
        health = "red"
        notes = "stripe_enabled=true but secret key or webhook secret missing."
    else:
        recent_cutoff = _now() - _dt.timedelta(days=RECENT_SUCCESS_DAYS)
        had_recent_paid = last_paid_at and last_paid_at >= recent_cutoff
        if links_created > 0 and paid == 0:
            health = "yellow"
            notes = "Links being created but no webhook events received. Check the webhook URL in Stripe's dashboard."
        elif had_recent_paid:
            health = "green"
        elif failed > 0 and paid == 0:
            health = "red"
            notes = f"{failed} failures and 0 successful payments in last 30d."
        else:
            health = "yellow"
            notes = "Configured but no paid quotes yet."

    return {
        "configured": configured,
        "enabled": enabled,
        "health": health,
        "last_success_at": _iso(last_paid_at),
        "last_error": last_error,
        "last_error_at": _iso(last_error_at),
        "stats_30d": {
            "successes": paid,
            "failures": failed,
            "links_created": links_created,
        },
        "notes": notes,
    }


def _missive_status(db: Session, organization_slug: str) -> dict:
    api_token = _setting(db, organization_slug, "missive_api_token") or ""
    enabled = _truthy(_setting(db, organization_slug, "missive_enabled"))
    from_addr = _setting(db, organization_slug, "missive_from_address") or ""
    configured = bool(api_token)

    cutoff = _now() - _dt.timedelta(days=ACTIVITY_WINDOW_DAYS)
    activity = (
        db.query(func.count(Conversation.id), func.max(Conversation.updated_at))
        .filter(
            Conversation.organization_slug == organization_slug,
            Conversation.channel == "missive",
            Conversation.created_at >= cutoff,
        )
        .first()
    )
    count_30d = activity[0] if activity else 0
    last_activity_at = activity[1] if activity else None

    notes = None
    if not enabled:
        health = "unknown"
        notes = "Missive integration disabled."
    elif not configured:
        health = "red"
        notes = "Enabled but missive_api_token is empty."
    else:
        recent_cutoff = _now() - _dt.timedelta(days=RECENT_SUCCESS_DAYS)
        recent = last_activity_at and last_activity_at >= recent_cutoff
        if recent:
            health = "green"
        elif count_30d > 0:
            health = "yellow"
            notes = "Configured but no Missive conversations in the last 7 days."
        else:
            health = "yellow"
            notes = "Configured but no activity yet — verify the webhook rule on Missive's side."

    # Surface the test-vs-real from_address gotcha as a hint
    if enabled and from_addr and "strategos-ai.com" in from_addr:
        notes = (notes or "") + " (Heads up: missive_from_address still points to a Strategos test inbox.)"

    return {
        "configured": configured,
        "enabled": enabled,
        "health": health,
        "last_success_at": _iso(last_activity_at),
        "last_error": None,  # We don't currently persist Missive errors per-conversation
        "last_error_at": None,
        "stats_30d": {"conversations": count_30d},
        "from_address": from_addr,
        "notes": notes,
    }


# =============================================================================
# Public entry point
# =============================================================================


def compute_integration_status(db: Session, organization_slug: str) -> dict[str, Any]:
    """
    Return a structured health summary for every integration on the tenant.

    Shape:
        {
          "missive":    { configured, enabled, health, last_success_at, ... },
          "printlogic": { ... },
          "stripe":     { ... },
          "computed_at": iso8601
        }

    Use the Overall the dashboard renders one card per integration.
    """
    return {
        "missive": _missive_status(db, organization_slug),
        "printlogic": _printlogic_status(db, organization_slug),
        "stripe": _stripe_status(db, organization_slug),
        "computed_at": _iso(_now()),
    }
