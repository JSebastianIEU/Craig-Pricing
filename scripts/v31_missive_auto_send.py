"""
V31 migration — seed `missive_auto_send_enabled=true`.

Justin's ask (May 2026): the email back-and-forth before the quote
PDF (asking what finish, what qty, collection vs delivery, etc.) is
mechanical and Craig should send those replies straight through.
Drafts are only useful for the moment money becomes binding — the
email with the quote PDF attached.

This migration seeds the per-org setting that controls the new
auto-send behaviour. Default is "true" (auto-send is ON for clarifying
replies; the quote PDF still drafts). Set to "false" via the dashboard
to roll back to the pre-v32 "everything is a draft" behaviour.

Idempotent. Safe to re-run. Doesn't overwrite an explicit existing
value — if Justin (or another operator) sets it to "false" on purpose,
re-running this migration won't flip it back to "true".

Usage:
    python -m scripts.v31_missive_auto_send
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db import db_session
from db.models import Setting


_SEED_KEY = "missive_auto_send_enabled"
_SEED_VALUE = "true"


def migrate() -> None:
    print("V31: seed missive_auto_send_enabled=true (auto-send on, except PDF)...")

    with db_session() as db:
        # Find every distinct organization_slug we have settings for.
        # We only seed orgs that already have at least one setting row
        # (so we don't accidentally create a phantom org tag).
        existing_orgs = (
            db.query(Setting.organization_slug)
            .distinct()
            .all()
        )
        seeded = 0
        skipped = 0
        for (org_slug,) in existing_orgs:
            row = (
                db.query(Setting)
                .filter_by(organization_slug=org_slug, key=_SEED_KEY)
                .first()
            )
            if row is not None:
                skipped += 1
                continue
            db.add(Setting(
                organization_slug=org_slug,
                key=_SEED_KEY,
                value=_SEED_VALUE,
                value_type="string",
            ))
            seeded += 1
        if seeded:
            print(f"  + seeded {_SEED_KEY}={_SEED_VALUE!r} on {seeded} org(s)")
        if skipped:
            print(f"  · skipped {skipped} org(s) (already had an explicit value)")
        if not (seeded or skipped):
            print("  · no orgs found — nothing to do")


if __name__ == "__main__":
    migrate()
