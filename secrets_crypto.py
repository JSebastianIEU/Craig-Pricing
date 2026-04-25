"""
Application-level encryption for secret Setting values.

Why this exists: secrets like `stripe_secret_key`, `printlogic_api_key`,
etc. live in the `settings` table in Cloud SQL Postgres. Plain rows mean
that anyone who gets their hands on a DB dump (stolen backup, leaked
snapshot, future SQL-injection) reads every tenant's keys in clear.

Solution: Fernet (AES-128-CBC + HMAC-SHA256) encrypt the value before
INSERT/UPDATE, decrypt on read. The Fernet key itself never lives in the
DB — it's mounted into Cloud Run as the `CRAIG_SECRETS_KEY` env var,
sourced from Google Secret Manager. So a stolen DB dump alone is useless.

## Threat model

  | Attack                         | Pre-fix         | Post-fix         |
  | DB dump leaked                 | secrets exposed | encrypted blobs  |
  | Cloud Run env var leaked       | n/a             | secrets exposed  |
  | Both DB AND env leaked         | secrets exposed | secrets exposed  |
  | App-level RCE on Cloud Run     | secrets exposed | secrets exposed  |

i.e. we defend specifically against DB-only compromise, which is the most
common operational risk (snapshots, backups, replicas, dev-vs-prod mixups).
We do NOT defend against a full Cloud Run compromise — that's accepted.

## Key rotation

Fernet's `MultiFernet` lets us rotate by adding a new key first in the
list (used for new writes) while keeping the old key (used to decrypt
old rows). To rotate:
  1. Generate new key, set `CRAIG_SECRETS_KEY=new,old` (comma-separated)
  2. Deploy → new writes use the new key
  3. Run a one-off migration that re-encrypts every row
  4. Drop the old key from the env var

For now (single-tenant) we use a single key. The comma-split is forward-
compatible.

## Backwards compatibility

Existing rows in production may have plaintext values written before this
module existed. `decrypt()` first attempts Fernet decode; on failure it
returns the input unchanged, treating it as a not-yet-encrypted legacy
row. The migration script (`v17_encrypt_existing_secrets.py`) re-writes
those rows with proper ciphertext.

## Local dev

If `CRAIG_SECRETS_KEY` is unset (typical for `pytest` and local widget
testing), we lazily generate an in-process key. **Data written this way
cannot be read by any other process.** That's intentional — local dev
DBs are throwaway. Tests work against this in-process key with no setup.
"""

from __future__ import annotations

import base64
import hashlib
import os
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken, MultiFernet


_FERNET: Optional[MultiFernet] = None
_FERNET_LOCAL_KEY: Optional[bytes] = None  # for tests / local dev


def _key_from_env() -> Optional[MultiFernet]:
    """
    Build a MultiFernet from the comma-separated `CRAIG_SECRETS_KEY` env var.
    Returns None if the env var is missing or empty (caller falls back to
    a process-local key).

    Each comma-separated value can be either:
      - A 32-byte url-safe base64 Fernet key (proper format)
      - Any other string — we'll hash it to derive a key (convenience for
        dev; NOT for prod)
    """
    raw = os.environ.get("CRAIG_SECRETS_KEY", "").strip()
    if not raw:
        return None

    keys: list[Fernet] = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            # Try as a real Fernet key first
            keys.append(Fernet(chunk.encode()))
        except (ValueError, TypeError):
            # Fallback: derive a key from arbitrary string. The same input
            # always yields the same key so values stay readable across
            # restarts. Production should always use a real Fernet key.
            derived = base64.urlsafe_b64encode(hashlib.sha256(chunk.encode()).digest())
            keys.append(Fernet(derived))
    if not keys:
        return None
    return MultiFernet(keys)


def _local_key() -> MultiFernet:
    """Lazy in-process key for tests / local dev. Persists for the
    lifetime of the process only — by design."""
    global _FERNET_LOCAL_KEY
    if _FERNET_LOCAL_KEY is None:
        _FERNET_LOCAL_KEY = Fernet.generate_key()
    return MultiFernet([Fernet(_FERNET_LOCAL_KEY)])


def _get_fernet() -> MultiFernet:
    """Return the active MultiFernet instance, building it on first use."""
    global _FERNET
    if _FERNET is None:
        _FERNET = _key_from_env() or _local_key()
    return _FERNET


# Marker prefix — encrypted ciphertext starts with this so we can tell
# encrypted-vs-plaintext apart without trying to decrypt every value. The
# prefix lives BEFORE the Fernet token (which itself starts with `gAAAAA`),
# so a Fernet token alone wouldn't be confused with raw user input.
_PREFIX = "enc::v1::"


def encrypt(plaintext: str) -> str:
    """Encrypt a string value. Returns a wire-safe ASCII string with
    `enc::v1::` prefix."""
    if not plaintext:
        # Don't encrypt empty values — keeps "not yet set" readable in DB
        return plaintext
    token = _get_fernet().encrypt(plaintext.encode("utf-8"))
    return _PREFIX + token.decode("ascii")


def decrypt(value: str) -> str:
    """
    Decrypt a value if it's encrypted, return as-is otherwise.

    Backwards compat: rows that pre-date this module are stored in
    plaintext (no prefix). We detect by prefix, attempt Fernet decode
    only on prefixed values, and fall through to "treat as plaintext"
    on any decode failure.
    """
    if not value:
        return value
    if not value.startswith(_PREFIX):
        # Legacy plaintext row — return as-is. Will get re-encrypted next
        # write or by the v17 migration.
        return value
    token = value[len(_PREFIX):].encode("ascii")
    try:
        return _get_fernet().decrypt(token).decode("utf-8")
    except InvalidToken:
        # Corrupted / wrong-key value. Don't crash — return the raw stored
        # string (best-effort) and let the caller deal with it.
        return value


def is_encrypted(value: str | None) -> bool:
    """Cheap check used by the migration to skip already-encrypted rows."""
    return bool(value) and value.startswith(_PREFIX)


def reset_for_tests() -> None:
    """Wipe the cached cipher so subsequent calls re-read CRAIG_SECRETS_KEY.
    Tests use this to flip between key configurations."""
    global _FERNET, _FERNET_LOCAL_KEY
    _FERNET = None
    _FERNET_LOCAL_KEY = None
