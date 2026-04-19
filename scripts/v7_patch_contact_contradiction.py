"""
V7 migration: remove the "ONLY collect contact info for escalations" block
from stored `system_prompt` rows.

Context: the V4-seeded base personality told Craig to NEVER ask for contact
details on standard quotes. That directly contradicts the V6 business rule
that says contact info is REQUIRED before issuing a PDF. LLMs follow the
most emphatic/earliest directive, and removing the contradiction is more
reliable than trying to out-shout it.

This script surgically deletes the conflicting block — anything else the
user has edited in the prompt is preserved. Idempotent: if the block is
already gone (user edited it, or a previous run stripped it), nothing
happens.

Usage:
    python -m scripts.v7_patch_contact_contradiction
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db import db_session, init_db
from db.models import Setting


# The exact marker for the start of the conflicting section in V4's seed.
_START_MARKER = "## When to collect contact details"

# The next section after the contact-details block — stop removal here so
# we don't eat unrelated content the user may have added below.
_END_MARKERS = (
    "## Tone examples",
    "## Helpful images",
    "## Catalog + business rules",
    "## Products and their",  # legacy, v5 should have already removed
)


def _strip_contact_block(prompt: str) -> str | None:
    """Return the prompt with the contact-details block removed, or None if
    the marker isn't present (nothing to do)."""
    start = prompt.find(_START_MARKER)
    if start == -1:
        return None
    # Find the earliest subsequent section header so we stop there.
    end_candidates = [prompt.find(m, start + len(_START_MARKER)) for m in _END_MARKERS]
    end_candidates = [p for p in end_candidates if p != -1]
    if end_candidates:
        end = min(end_candidates)
        return (prompt[:start].rstrip() + "\n\n" + prompt[end:]).strip() + "\n"
    # No subsequent section — just truncate from the marker onward.
    return prompt[:start].rstrip() + "\n"


def migrate() -> None:
    print("V7: removing contradictory contact-info block from stored prompts...")
    patched = 0
    with db_session() as db:
        rows = db.query(Setting).filter(Setting.key == "system_prompt").all()
        for s in rows:
            if not s.value:
                continue
            new_value = _strip_contact_block(s.value)
            if new_value is None or new_value == s.value:
                print(f"  \u00b7 {s.organization_slug}: nothing to patch")
                continue
            s.value = new_value
            patched += 1
            print(f"  \u2702 {s.organization_slug}: stripped contact-details block")

    print()
    print(f"\u2713 {patched} prompts patched.")


if __name__ == "__main__":
    init_db()
    migrate()
