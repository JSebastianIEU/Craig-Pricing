"""
V6 seed: populate `business_rules` with sensible defaults for any tenant
whose rules list is still empty.

These rules patch two Craig-wide behavioral gaps that the personality
prompt alone hasn't reliably prevented:

  1. Craig opens his first reply with a duplicate greeting (the widget
     already showed one client-side).
  2. Craig emits [QUOTE_READY] before collecting the customer's contact
     info, so the PDF goes out with no one attached to follow up with.

Business rules are appended to the system prompt at runtime, so they
behave as overrides over the base personality.

Idempotent — if `business_rules` has ANY content, this script leaves it
alone. Users who have already added their own rules via the Settings UI
are never overwritten.

Usage:
    python -m scripts.v6_default_business_rules
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db import db_session, init_db
from db.models import Setting


DEFAULT_RULES: list[str] = [
    (
        "The chat widget always shows the customer a greeting the moment they "
        "open it. On the customer's FIRST message, do NOT open your reply with "
        "another greeting, hello, or self-introduction. Skip straight to "
        "answering or asking the next clarifying question."
    ),
    (
        "The VERBAL PRICE is always free — you can quote any standard product "
        "from the catalog without asking for contact info. Just say the "
        "inc-VAT total inline (e.g. \"That'll be €46.74 for 500 business "
        "cards 👍\") as soon as you have product + quantity + specs."
    ),
    (
        "The PDF QUOTE and any ORDER REQUEST require contact info — this is "
        "the gate. Flow: (1) state the verbal price; (2) ask \"Want me to "
        "put together the full quote for you? 📋\"; (3) if they say yes, "
        "ask for their name + email (or WhatsApp number); (4) validate the "
        "email or phone and confirm it back; (5) call save_customer_info; "
        "(6) ONLY AFTER the tool call succeeds, reply with exactly: "
        "\"Here's your quote! 📋 [QUOTE_READY]\". The server will strip "
        "[QUOTE_READY] if contact info is missing, so emitting it without "
        "having called save_customer_info will fail silently."
    ),
    (
        "Same contact-first rule applies to escalations. Before calling "
        "escalate_to_justin, collect name + email/phone, validate, confirm, "
        "call save_customer_info, and THEN call escalate_to_justin. Never "
        "escalate an anonymous conversation — Justin has no one to follow "
        "up with."
    ),
]


def seed() -> None:
    print("V6: seeding default business_rules where empty...")
    updated = 0
    with db_session() as db:
        rows = db.query(Setting).filter(Setting.key == "business_rules").all()
        for s in rows:
            current: list = []
            if s.value:
                try:
                    parsed = json.loads(s.value)
                    if isinstance(parsed, list):
                        current = parsed
                except (ValueError, TypeError):
                    current = []
            if current:
                print(f"  \u00b7 {s.organization_slug}: already has {len(current)} rule(s) \u2014 leaving alone")
                continue
            s.value = json.dumps(DEFAULT_RULES)
            s.value_type = "json"
            updated += 1
            print(f"  + {s.organization_slug}: seeded {len(DEFAULT_RULES)} default rules")

    print()
    print(f"\u2713 {updated} tenants updated.")


if __name__ == "__main__":
    init_db()
    seed()
