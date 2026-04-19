"""
V9 seed: insert Missive-integration settings rows for every tenant.

Five keys per tenant, idempotent — rows that already exist are left alone.

    missive_enabled          -> "false"
    missive_api_token        -> ""  (user pastes it via the dashboard)
    missive_webhook_secret   -> secrets.token_urlsafe(32) (auto-generated)
    missive_from_address     -> sensible tenant default
    missive_from_name        -> "Craig @ <Org Name>"

The dashboard's MissiveTab writes / reads these via the generic
`PATCH /admin/api/orgs/{org_slug}/settings/{key}` endpoint, which already
upserts. This script just guarantees the rows exist with sane starting
values the first time a tenant is provisioned — so the dashboard always
has a webhook secret to show, even before the user touches anything.

Usage:
    python -m scripts.v9_missive_settings_seed
"""

from __future__ import annotations

import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db import db_session, init_db
from db.models import Setting


# Per-tenant seed defaults. Fall back to generic values for tenants we
# don't know about yet. Add an entry here when provisioning a new client.
_TENANT_DEFAULTS: dict[str, dict[str, str]] = {
    "just-print": {
        "missive_from_address": "sebastian@strategos-ai.com",
        "missive_from_name": "Craig @ Just Print",
    },
}


_GENERIC_DEFAULTS = {
    "missive_from_address": "",
    "missive_from_name": "Craig",
}


# (key, default_factory_or_value, value_type, description)
# `default` is either a literal string or a callable returning one.
SEED_ROWS = [
    ("missive_enabled", "false", "string",
     "Kill switch for the Missive channel. 'true' / 'false'."),
    ("missive_api_token", "", "string",
     "Bearer token used to POST drafts to Missive. Generated from Missive \u2192 Integrations \u2192 API."),
    ("missive_webhook_secret", lambda: secrets.token_urlsafe(32), "string",
     "Shared HMAC-SHA256 secret. Set on the Missive rule's Webhook action; "
     "Craig verifies every incoming webhook against it."),
    ("missive_from_address", None, "string",
     "Address the draft reply is attributed to. Defaults to the watched inbox."),
    ("missive_from_name", None, "string",
     "Display name on the draft reply."),
]


def _resolve_default(key: str, fallback: object, org_slug: str) -> str:
    """Tenant-specific default beats generic fallback beats the row's own."""
    tenant = _TENANT_DEFAULTS.get(org_slug, {})
    if key in tenant:
        return tenant[key]
    if key in _GENERIC_DEFAULTS:
        return _GENERIC_DEFAULTS[key]
    return fallback() if callable(fallback) else (fallback or "")


def seed() -> None:
    print("V9: seeding Missive settings rows per tenant...")
    inserted = 0
    with db_session() as db:
        # Enumerate tenants via any existing per-tenant row — system_prompt is
        # guaranteed by the V4 seed.
        prompts = db.query(Setting).filter(Setting.key == "system_prompt").all()
        tenant_slugs = sorted({s.organization_slug for s in prompts})

        for slug in tenant_slugs:
            for key, default, value_type, description in SEED_ROWS:
                existing = (
                    db.query(Setting)
                    .filter_by(organization_slug=slug, key=key)
                    .first()
                )
                if existing:
                    continue
                value = _resolve_default(key, default, slug)
                db.add(Setting(
                    organization_slug=slug,
                    key=key,
                    value=value,
                    value_type=value_type,
                    description=description,
                ))
                inserted += 1
                print(f"  + {slug}/{key}")

    print()
    print(f"\u2713 {inserted} Missive settings inserted.")


if __name__ == "__main__":
    init_db()
    seed()
