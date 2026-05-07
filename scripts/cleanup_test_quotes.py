"""
One-off cleanup — purge test quotes / conversations from production.

Scope (confirmed by JS, May 7 2026):

  Delete quotes whose conversation customer_email is one of:
    - sebastian@strategos-ai.com
    - jpenad.ieu2023@student.ie.edu
    - juansebastianpenadonneys@gmail.com
    - partygames2000@gmail.com

  Delete every quote with id < 38 (everything before JP-0038 is test data).

  PRESERVE these quote ids no matter what:
    - 85  (foamex boards — real client)
    - 86  (vinyl labels  — real client)

When a conversation has no surviving quotes after deletion, drop the
conversation too. Conversations with at least one preserved quote stay.

DRY RUN by default. Pass `--apply` to actually delete.

Usage:
    # Preview
    python -m scripts.cleanup_test_quotes

    # Actually delete
    python -m scripts.cleanup_test_quotes --apply
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import or_

from db import db_session
from db.models import Conversation, Quote


# ---------------------------------------------------------------------------
# Config — explicit, easy to audit
# ---------------------------------------------------------------------------

TEST_EMAILS = {
    "sebastian@strategos-ai.com",
    "jpenad.ieu2023@student.ie.edu",
    "juansebastianpenadonneys@gmail.com",
    "partygames2000@gmail.com",
}

# Cutoff: every quote BELOW this id is test data (per JS).
ID_CUTOFF = 38

# Hard preserve — never delete these even if they match the email/id rules.
PRESERVE_QUOTE_IDS = {85, 86}


def _norm_email(e: str | None) -> str:
    return (e or "").strip().lower()


def main(apply: bool = False) -> None:
    print("=" * 60)
    print(f"Test-quote cleanup ({'APPLY' if apply else 'DRY RUN'})")
    print("=" * 60)
    print(f"  test_emails       : {sorted(TEST_EMAILS)}")
    print(f"  id_cutoff         : id < {ID_CUTOFF}")
    print(f"  preserve_quote_ids: {sorted(PRESERVE_QUOTE_IDS)}")
    print()

    with db_session() as db:
        # ── 1. Identify quotes to delete ────────────────────────────
        all_quotes = db.query(Quote).all()
        all_convs = {c.id: c for c in db.query(Conversation).all()}

        to_delete: list[Quote] = []
        keep_reasons: dict[int, str] = {}

        for q in all_quotes:
            if q.id in PRESERVE_QUOTE_IDS:
                keep_reasons[q.id] = "preserve-list"
                continue

            conv = all_convs.get(q.conversation_id) if q.conversation_id else None
            email = _norm_email(conv.customer_email if conv else None)

            reason: str | None = None
            if q.id < ID_CUTOFF:
                reason = f"id<{ID_CUTOFF}"
            elif email in TEST_EMAILS:
                reason = f"email={email}"

            if reason:
                to_delete.append(q)
                print(
                    f"  DEL  JP-{q.id:04d}  conv={q.conversation_id}  "
                    f"product={q.product_key}  "
                    f"email={email or '(none)'}  reason={reason}"
                )
            else:
                keep_reasons[q.id] = "kept"

        print()
        print(f"  → {len(to_delete)} quotes flagged for deletion")
        print(f"  → {len(keep_reasons)} quotes kept "
              f"(of which {len([k for k,v in keep_reasons.items() if v == 'preserve-list'])} explicitly preserved)")
        print()

        # ── 2. Identify conversations to delete (only ones whose every
        #      surviving quote is gone) ───────────────────────────────
        delete_quote_ids = {q.id for q in to_delete}

        survivors_by_conv: dict[int, list[int]] = {}
        for q in all_quotes:
            if q.id in delete_quote_ids:
                continue
            survivors_by_conv.setdefault(q.conversation_id or 0, []).append(q.id)

        conv_ids_with_dead_quotes = {
            q.conversation_id for q in to_delete if q.conversation_id
        }
        convs_to_delete: list[Conversation] = []
        for cid in conv_ids_with_dead_quotes:
            survivors = survivors_by_conv.get(cid, [])
            if not survivors:
                conv = all_convs.get(cid)
                if conv:
                    convs_to_delete.append(conv)
                    print(
                        f"  DEL  conv {cid}  email={_norm_email(conv.customer_email)}  "
                        f"channel={conv.channel}  (no surviving quotes)"
                    )
            else:
                conv = all_convs.get(cid)
                print(
                    f"  KEEP conv {cid}  email={_norm_email(conv.customer_email) if conv else '?'}  "
                    f"survivors=JP-{','.join(f'{i:04d}' for i in survivors)}"
                )

        print()
        print(f"  → {len(convs_to_delete)} conversations flagged for deletion")
        print()

        if not apply:
            print("DRY RUN — nothing changed. Re-run with --apply to execute.")
            return

        # ── 3. Apply ────────────────────────────────────────────────
        # Delete quotes first (some might belong to convs we're keeping
        # because they have other survivors). Then delete the convs that
        # are fully orphaned. Conversation has cascade=all, delete-orphan
        # but we explicit-delete to keep this readable + auditable.
        for q in to_delete:
            db.delete(q)
        db.flush()
        for conv in convs_to_delete:
            db.delete(conv)
        db.commit()

        print(f"✓ Deleted {len(to_delete)} quotes and {len(convs_to_delete)} conversations.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--apply", action="store_true",
        help="Actually delete. Without this flag, runs in dry-run mode.",
    )
    args = parser.parse_args()
    main(apply=args.apply)
