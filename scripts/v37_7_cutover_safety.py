"""
V37.7 migration -- cutover-safety auto-OFF + internal-team allowlist seed.

Two things, both idempotent + tenant-scoped:

  1. Auto-OFF on missive_from_address change. The first time after this
     migration is deployed, we record the current `missive_from_address`
     into a sentinel Setting `missive_from_address_last_known`. On
     subsequent boots, if the operator updates `missive_from_address`
     to something different from the last-known value AND
     `missive_enabled` is currently `true`, we flip it to `false`.

     Why: cutover from a test mailbox (sebastian@strategos-ai.com) to
     production (info@just-print.ie) MUST start with Craig dormant.
     Forgetting to manually toggle OFF before pointing at the new
     mailbox would send Craig replies into Justin's real customer
     inbox the moment the first email arrives. This migration enforces
     the safe default automatically.

  2. Seed `internal_team_domains` Setting if missing. Default value:
     `["just-print.ie"]`. Prevents Craig from auto-replying to internal
     team emails (Eva, Ian, etc. on @just-print.ie sending each other
     mail that lands in the Missive-watched inbox).

Idempotent. Safe to re-run.

Usage:
    python -m scripts.v37_7_cutover_safety
"""

from __future__ import annotations

import json as _json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db import db_session
from db.models import DEFAULT_ORG_SLUG, Setting


def _get(db, key: str, organization_slug: str = DEFAULT_ORG_SLUG):
    return (
        db.query(Setting)
        .filter_by(organization_slug=organization_slug, key=key)
        .first()
    )


def _upsert(
    db, key: str, value: str, value_type: str = "string",
    organization_slug: str = DEFAULT_ORG_SLUG,
) -> str:
    """Returns 'added' / 'updated' / 'skipped' for log clarity."""
    row = _get(db, key, organization_slug)
    if row is None:
        db.add(Setting(
            organization_slug=organization_slug,
            key=key, value=value, value_type=value_type,
        ))
        return "added"
    if row.value == value:
        return "skipped"
    row.value = value
    row.value_type = value_type
    return "updated"


def migrate_for_tenant(db, organization_slug: str) -> None:
    """v37.7 — applied per-tenant. Currently only DEFAULT_ORG_SLUG
    (just-print) has Missive configured, but we keep the loop tenant-
    aware for when more clients land."""

    # 1. internal_team_domains seed (Phase C1 prerequisite).
    domains_row = _get(db, "internal_team_domains", organization_slug)
    if domains_row is None:
        # Derive default from the org slug if it looks like a domain.
        # Fallback: empty list, operator adds via dashboard.
        default_domains: list[str] = []
        if organization_slug == "just-print":
            default_domains = ["just-print.ie"]
        action = _upsert(
            db, "internal_team_domains",
            _json.dumps(default_domains),
            value_type="json",
            organization_slug=organization_slug,
        )
        print(
            f"  {action:>8}  setting internal_team_domains "
            f"= {default_domains!r} (org={organization_slug})"
        )
    else:
        print(
            f"   skipped  setting internal_team_domains already configured "
            f"(org={organization_slug})"
        )

    # 2. internal_team_addresses placeholder (empty list — operator
    # populates from dashboard if any team member uses a personal email).
    if _get(db, "internal_team_addresses", organization_slug) is None:
        action = _upsert(
            db, "internal_team_addresses",
            _json.dumps([]),
            value_type="json",
            organization_slug=organization_slug,
        )
        print(
            f"  {action:>8}  setting internal_team_addresses = [] "
            f"(org={organization_slug})"
        )

    # 3. Cutover safety: auto-OFF when missive_from_address changes.
    from_addr_row = _get(db, "missive_from_address", organization_slug)
    current_from = (from_addr_row.value if from_addr_row else "") or ""
    last_known_row = _get(
        db, "missive_from_address_last_known", organization_slug,
    )
    last_known = (last_known_row.value if last_known_row else "") or ""

    if not current_from:
        # No from-address set yet. Nothing to compare against.
        # Record the sentinel as empty so the first time the operator
        # configures it, we don't auto-OFF on the very first config.
        # (Auto-OFF should only fire when CHANGING from one configured
        # address to another — not on initial setup.)
        if last_known_row is None:
            _upsert(
                db, "missive_from_address_last_known", "",
                value_type="string",
                organization_slug=organization_slug,
            )
            print(
                f"   seeded  missive_from_address_last_known='' "
                f"(org={organization_slug}; awaiting first config)"
            )
        return

    if not last_known:
        # First time recording. Snapshot current value. No auto-OFF
        # (this is the initial setup, not a cutover).
        _upsert(
            db, "missive_from_address_last_known", current_from,
            value_type="string",
            organization_slug=organization_slug,
        )
        print(
            f"   seeded  missive_from_address_last_known={current_from!r} "
            f"(org={organization_slug}; first snapshot, no auto-OFF)"
        )
        return

    if current_from == last_known:
        print(
            f"   stable  missive_from_address unchanged "
            f"(={current_from!r}, org={organization_slug})"
        )
        return

    # Cutover detected: from_address changed.
    enabled_row = _get(db, "missive_enabled", organization_slug)
    enabled = (enabled_row.value if enabled_row else "false").lower() == "true"
    print(
        f"   change  missive_from_address: {last_known!r} -> {current_from!r} "
        f"(org={organization_slug})"
    )
    if enabled:
        _upsert(
            db, "missive_enabled", "false", value_type="string",
            organization_slug=organization_slug,
        )
        print(
            f"   PAUSED  missive_enabled flipped to false "
            f"(cutover safety; operator must manually re-enable)"
        )
    else:
        print(f"   note    missive_enabled was already false; no-op")

    # Update the snapshot so we don't keep firing.
    _upsert(
        db, "missive_from_address_last_known", current_from,
        value_type="string",
        organization_slug=organization_slug,
    )
    print(
        f"  updated  missive_from_address_last_known={current_from!r}"
    )


def migrate() -> None:
    """Apply v37.7 to all configured tenants."""
    print("V37.7: cutover safety + internal-team allowlist seed...")
    with db_session() as db:
        # Discover tenants from existing missive_from_address or
        # missive_enabled rows. Falls back to DEFAULT_ORG_SLUG so a
        # fresh install gets the seed.
        tenants = {
            row.organization_slug
            for row in db.query(Setting).filter(
                Setting.key.in_((
                    "missive_from_address",
                    "missive_enabled",
                    "missive_api_token",
                ))
            ).all()
        }
        tenants.add(DEFAULT_ORG_SLUG)
        for org_slug in sorted(tenants):
            print(f"  -- tenant: {org_slug} --")
            migrate_for_tenant(db, org_slug)
        db.commit()
    print()
    print("v37.7 migration complete.")


if __name__ == "__main__":
    migrate()
