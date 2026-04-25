"""
V17 migration — encrypt every existing secret-keyed Setting in place.

When `secrets_crypto` was introduced, existing rows in production had
plaintext values. New writes go through encrypt() but the historic rows
are still readable in the DB. This script walks every secret-keyed row
across every tenant and re-writes them as ciphertext.

Idempotent: rows that are already encrypted (`enc::v1::` prefix) are
skipped. Empty values are skipped (no point encrypting empty strings).

Pre-requisite: `CRAIG_SECRETS_KEY` env var must be set BEFORE running
this. Otherwise the script generates a new in-process key, encrypts
everything with it, and the next process can't decrypt — catastrophic.
There's a guard below that refuses to run without the env var on
production-like environments.

Usage:
    # On Cloud Run shell (or wherever CRAIG_SECRETS_KEY is set):
    python -m scripts.v17_encrypt_existing_secrets

    # Force-run for local dev (uses the in-process key — only safe on
    # throwaway DBs):
    CRAIG_FORCE_LOCAL_KEY=1 python -m scripts.v17_encrypt_existing_secrets
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db import db_session, init_db
from db.models import Setting
from secrets_crypto import encrypt, is_encrypted
from settings_security import SECRET_KEYS


def migrate() -> None:
    """
    Encrypt every plaintext secret-keyed Setting row. Soft-skips on
    Cloud Run boot when CRAIG_SECRETS_KEY is unset — the deploy keeps
    going, secrets stay plaintext, and the warning surfaces in logs
    until an operator provisions the key. This is by design: blocking
    boot on a missing key would mean any momentary Secret Manager outage
    crashes the entire service.
    """
    print("V17: encrypting existing secret-keyed Setting rows...")

    # Decide whether we have a stable key to encrypt with. Without one,
    # an in-process key would encrypt rows that NO future process can
    # decrypt — catastrophic data loss. Skip with a loud warning instead.
    has_env_key = bool(os.environ.get("CRAIG_SECRETS_KEY", "").strip())
    force_local = os.environ.get("CRAIG_FORCE_LOCAL_KEY", "").strip()
    if not has_env_key and not force_local:
        print(
            "  ⚠ CRAIG_SECRETS_KEY not set — SKIPPING. Secret-keyed rows remain "
            "in plaintext until the env var is provisioned (then re-run this "
            "script manually OR redeploy)."
        )
        print(
            "    To run anyway against a throwaway DB, set CRAIG_FORCE_LOCAL_KEY=1."
        )
        return

    init_db()
    encrypted_count = 0
    skipped_count = 0
    empty_count = 0

    with db_session() as db:
        rows = (
            db.query(Setting)
            .filter(Setting.key.in_(list(SECRET_KEYS)))
            .all()
        )
        if not rows:
            print("  · no secret-keyed rows found; nothing to do.")
            return

        for r in rows:
            if not r.value:
                empty_count += 1
                continue
            if is_encrypted(r.value):
                skipped_count += 1
                continue
            r.value = encrypt(r.value)
            encrypted_count += 1
            print(f"  + {r.organization_slug}/{r.key} encrypted (was {len(r.value)} bytes plain)")

    print()
    print(f"✓ encrypted={encrypted_count} already_encrypted={skipped_count} empty={empty_count}")


if __name__ == "__main__":
    migrate()
