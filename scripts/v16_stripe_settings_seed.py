"""
V16 seed — per-tenant Stripe settings.

Phase B: Stripe payment links for confirmed quotes. Five tenant-scoped
settings, all empty/disabled out of the box. Justin (or Roi on his behalf)
pastes the real keys in the dashboard Connections tab — the code is
dormant until `stripe_enabled` flips to `"true"`.

Seed rows:
  - stripe_enabled         = "false"  (master kill switch)
  - stripe_secret_key      = ""       (sk_live_... or sk_test_...)
  - stripe_webhook_secret  = ""       (whsec_... from the endpoint config)
  - stripe_currency        = "eur"    (Just Print is Ireland)
  - stripe_success_url     = ""       (optional; redirect after paid)

Defaulting `stripe_enabled=false` is the Phase B safety invariant. Even if
the code deploys before Justin gives us the keys, the flow returns
`{ok:false, error:"disabled"}` and `confirm_order` silently skips link
creation — customer sees exactly the same reply they see today. No
customer-facing regression possible until we explicitly turn it on.

Usage:
    python -m scripts.v16_stripe_settings_seed
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db import db_session, init_db
from db.models import Setting


SEED_ROWS: list[tuple[str, str, str, str]] = [
    # (key, value, value_type, description)
    (
        "stripe_enabled",
        "false",
        "string",
        "Master switch for Stripe payment links. 'true' enables link creation "
        "on confirm_order; 'false' skips the integration entirely. Default false "
        "so the code is dormant until keys are pasted and a dry run is done.",
    ),
    (
        "stripe_secret_key",
        "",
        "string",
        "Tenant's Stripe secret key (sk_live_... in prod, sk_test_... for testing). "
        "Comes from the tenant's OWN Stripe dashboard — we never share keys "
        "across tenants. Never commit.",
    ),
    (
        "stripe_webhook_secret",
        "",
        "string",
        "Signing secret (whsec_...) for the webhook endpoint the tenant "
        "configures at https://dashboard.stripe.com/webhooks. Required — we "
        "reject any webhook whose HMAC doesn't match.",
    ),
    (
        "stripe_currency",
        "eur",
        "string",
        "ISO 4217 currency code used when creating payment links. Default 'eur' "
        "for Just Print (Ireland). Change per tenant as needed.",
    ),
    (
        "stripe_success_url",
        "",
        "string",
        "Optional. URL Stripe redirects to after successful payment. Leave "
        "empty to use Stripe's hosted confirmation page.",
    ),
]


def seed() -> None:
    print("V16: seeding Stripe settings per tenant...")
    init_db()
    inserted = 0
    with db_session() as db:
        prompts = db.query(Setting).filter(Setting.key == "system_prompt").all()
        tenant_slugs = sorted({s.organization_slug for s in prompts})
        if not tenant_slugs:
            print("  \u00b7 no tenants found; nothing to seed.")
            return

        for slug in tenant_slugs:
            for key, value, value_type, description in SEED_ROWS:
                existing = (
                    db.query(Setting)
                    .filter_by(organization_slug=slug, key=key)
                    .first()
                )
                if existing:
                    print(f"  \u00b7 {slug}/{key} already present")
                    continue
                db.add(Setting(
                    organization_slug=slug,
                    key=key,
                    value=value,
                    value_type=value_type,
                    description=description,
                ))
                inserted += 1
                print(f"  + {slug}/{key}  (value={value!r})")

    print()
    print(f"\u2713 {inserted} settings inserted.")


if __name__ == "__main__":
    seed()
