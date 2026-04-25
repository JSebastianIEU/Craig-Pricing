"""
V18 migration — clean up legacy Stripe paste-key settings.

When we migrated to Stripe Connect (OAuth), the per-tenant
`stripe_secret_key` and `stripe_webhook_secret` Settings became
obsolete. This script deletes those rows for every tenant.

Idempotent — re-running is a no-op for tenants whose rows have already
been deleted. Logs the count so the deploy log makes the change
visible.

In production today (verified via the V17 audit logs), Just Print's
legacy Stripe rows were both empty strings — so deleting them is
literally lossless. Future tenants never had these rows because v16
was updated in the same release that introduced Connect.

Usage:
    python -m scripts.v18_stripe_connect_migration
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db import db_session, init_db
from db.models import Setting


# Keys removed by this migration. Update this list ONLY in lockstep with
# the corresponding removal from settings_security.SECRET_KEYS and v16.
LEGACY_KEYS: tuple[str, ...] = (
    "stripe_secret_key",
    "stripe_webhook_secret",
)


def migrate() -> None:
    print("V18: removing legacy Stripe paste-flow settings...")
    init_db()

    deleted = 0
    with db_session() as db:
        rows = (
            db.query(Setting)
            .filter(Setting.key.in_(list(LEGACY_KEYS)))
            .all()
        )
        if not rows:
            print("  · no legacy rows present; nothing to do.")
            return

        for r in rows:
            had_value = bool(r.value)
            print(
                f"  - deleting {r.organization_slug}/{r.key}"
                f" (had_value={had_value})"
            )
            db.delete(r)
            deleted += 1

    print()
    print(f"✓ {deleted} legacy Stripe rows removed.")


if __name__ == "__main__":
    migrate()
