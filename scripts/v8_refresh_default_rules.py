"""
V8 migration: upgrade tenants whose `business_rules` still match the
previous V6 default set to the latest V6 default set.

Rationale: V6's `seed()` is idempotent and never touches rows that already
have content — which is the right call for tenants who've customized their
rules. But it means we have no way to roll out an improved default set to
tenants who never edited. This script fills that gap: it compares the
stored rules against the known historical V6 default strings and, if they
match exactly, swaps them for the current DEFAULT_RULES.

If a tenant has edited even one rule, we skip — treating their setup as
intentional.

Idempotent on its own. Safe to run on every boot.

Usage:
    python -m scripts.v8_refresh_default_rules
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db import db_session, init_db
from db.models import Setting
from scripts.v6_default_business_rules import DEFAULT_RULES


# The exact rule set seeded by the original V6 (pre-upgrade). Listed here
# verbatim so we can detect unedited installs and replace them without
# touching anything a user has customized.
_V6_ORIGINAL_RULES: list[str] = [
    (
        "The chat widget always shows the customer a greeting the moment they "
        "open it. On the customer's FIRST message, do NOT open your reply with "
        "another greeting, hello, or self-introduction. Skip straight to "
        "answering or asking the next clarifying question."
    ),
    (
        "Contact info is required before issuing a PDF quote. Flow for a "
        "standard quote: (1) give the verbal price inline; (2) ask 'Want me "
        "to put together the full quote for you?'; (3) if they say yes, ask "
        "for their name + email (or WhatsApp number); (4) validate the email "
        "or phone and confirm it back; (5) call save_customer_info; (6) "
        "ONLY AFTER the tool call succeeds, reply with exactly: "
        "\"Here's your quote! 📋 [QUOTE_READY]\". No contact info = no "
        "[QUOTE_READY] marker."
    ),
    (
        "Same contact-first rule applies to escalations. Before calling "
        "escalate_to_justin, collect name + email/phone, validate, confirm, "
        "call save_customer_info, and THEN call escalate_to_justin. Never "
        "escalate an anonymous conversation — Justin has no one to follow "
        "up with."
    ),
]


def migrate() -> None:
    print("V8: upgrading unedited V6-default business_rules to latest...")
    upgraded = 0
    skipped = 0
    with db_session() as db:
        rows = db.query(Setting).filter(Setting.key == "business_rules").all()
        for s in rows:
            try:
                parsed = json.loads(s.value or "[]")
            except (ValueError, TypeError):
                parsed = None
            if parsed == _V6_ORIGINAL_RULES:
                s.value = json.dumps(DEFAULT_RULES)
                s.value_type = "json"
                upgraded += 1
                print(f"  \u2191 {s.organization_slug}: upgraded to latest defaults")
            else:
                skipped += 1
                print(f"  \u00b7 {s.organization_slug}: custom rules, leaving alone")

    print()
    print(f"\u2713 {upgraded} tenants upgraded, {skipped} preserved.")


if __name__ == "__main__":
    init_db()
    migrate()
