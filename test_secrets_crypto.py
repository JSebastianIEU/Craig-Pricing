"""
Tests for the secret encryption layer + the API-level masking.

Two things to verify:
  1. `secrets_crypto.encrypt/decrypt` round-trips and rejects garbage cleanly.
  2. `admin_api` GET endpoints mask secrets and the PATCH endpoint refuses
     to overwrite real secrets with the literal mask string.
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(__file__))

import pytest

# Force a stable key for tests so encrypted rows survive across calls.
os.environ.setdefault(
    "CRAIG_SECRETS_KEY",
    "test-key-very-stable-just-for-pytest-do-not-use-in-prod",
)
os.environ.setdefault(
    "STRATEGOS_JWT_SECRET",
    "test-secret-32-bytes-long-padding-enough-now",
)

import secrets_crypto
from settings_security import SECRET_KEYS, SECRET_MASK, is_secret, is_mask, mask_value


# =============================================================================
# settings_security
# =============================================================================


def test_secret_key_allowlist_matches_known_keys():
    """The allowlist should contain exactly the 5 secret keys we recognize.
    Adding a new tenant secret key must update SECRET_KEYS in the same PR."""
    assert "stripe_secret_key" in SECRET_KEYS
    assert "stripe_webhook_secret" in SECRET_KEYS
    assert "printlogic_api_key" in SECRET_KEYS
    assert "missive_api_token" in SECRET_KEYS
    assert "missive_webhook_secret" in SECRET_KEYS


def test_is_secret_handles_known_and_unknown():
    assert is_secret("stripe_secret_key")
    assert not is_secret("system_prompt")
    assert not is_secret("vat_rate")
    assert not is_secret("")


def test_mask_value_masks_secret_only_when_value_present():
    assert mask_value("stripe_secret_key", "sk_live_abc") == SECRET_MASK
    # Empty secret stays empty so frontend can show "not yet configured"
    assert mask_value("stripe_secret_key", "") == ""
    assert mask_value("stripe_secret_key", None) is None
    # Non-secret keys pass through
    assert mask_value("system_prompt", "you are craig") == "you are craig"


def test_is_mask_only_true_for_exact_mask():
    assert is_mask(SECRET_MASK)
    assert not is_mask("not the mask")
    assert not is_mask("")
    assert not is_mask(None)


# =============================================================================
# secrets_crypto
# =============================================================================


def test_encrypt_decrypt_round_trip():
    secrets_crypto.reset_for_tests()
    plaintext = "sk_live_supersecretvalue_abc123"
    ciphertext = secrets_crypto.encrypt(plaintext)
    assert ciphertext != plaintext
    assert ciphertext.startswith("enc::v1::")
    assert secrets_crypto.decrypt(ciphertext) == plaintext


def test_encrypt_empty_returns_empty():
    """Empty values stay empty — saves a Fernet roundtrip and keeps
    'unconfigured' visible in DB."""
    secrets_crypto.reset_for_tests()
    assert secrets_crypto.encrypt("") == ""
    assert secrets_crypto.decrypt("") == ""


def test_decrypt_passes_through_legacy_plaintext():
    """Backwards compat: rows from before this module existed are
    plaintext with no `enc::v1::` prefix. Decrypt must return them as-is."""
    secrets_crypto.reset_for_tests()
    assert secrets_crypto.decrypt("legacy_plaintext_secret") == "legacy_plaintext_secret"


def test_decrypt_corrupted_token_returns_input():
    """A row with the prefix but corrupted/wrong-key body must NOT crash —
    return the raw stored string and let callers handle it."""
    secrets_crypto.reset_for_tests()
    bad = "enc::v1::definitely_not_a_valid_fernet_token"
    # Should not raise
    result = secrets_crypto.decrypt(bad)
    assert isinstance(result, str)


def test_is_encrypted_detects_prefix_only():
    secrets_crypto.reset_for_tests()
    assert secrets_crypto.is_encrypted("enc::v1::abc")
    assert not secrets_crypto.is_encrypted("plain text")
    assert not secrets_crypto.is_encrypted("")
    assert not secrets_crypto.is_encrypted(None)


def test_two_separate_encrypts_produce_different_ciphertexts():
    """Fernet uses random IVs — same plaintext encrypts differently each
    time. Important so attackers can't tell two tenants share the same
    secret value just by comparing ciphertext."""
    secrets_crypto.reset_for_tests()
    a = secrets_crypto.encrypt("same plaintext")
    b = secrets_crypto.encrypt("same plaintext")
    assert a != b
    assert secrets_crypto.decrypt(a) == "same plaintext"
    assert secrets_crypto.decrypt(b) == "same plaintext"


# =============================================================================
# admin_api integration — masking + PATCH guard
# =============================================================================


import jwt as _jwt
from fastapi.testclient import TestClient

from app import app

_client = TestClient(app)


def _token(role: str = "client_owner", org: str = "just-print") -> str:
    return _jwt.encode(
        {
            "email": "test@example.com",
            "org_slug": org,
            "role": role,
            "iat": int(time.time()),
            "exp": int(time.time()) + 300,
            "iss": "strategos-dashboard",
            "sub": "test@example.com",
        },
        os.environ["STRATEGOS_JWT_SECRET"],
        algorithm="HS256",
    )


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {_token()}"}


def test_get_settings_masks_secret_values():
    """GET /settings should return ******** for secret keys with a value
    set, and the raw value for non-secret keys."""
    # Set a real secret first
    r = _client.patch(
        "/admin/api/orgs/just-print/settings/stripe_secret_key",
        headers=_auth(),
        json={"value": "sk_test_real_secret_value_123", "value_type": "string"},
    )
    assert r.status_code == 200

    # Now fetch all settings and confirm the secret is masked
    r = _client.get("/admin/api/orgs/just-print/settings", headers=_auth())
    assert r.status_code == 200
    settings = {s["key"]: s["value"] for s in r.json()["settings"]}
    assert settings.get("stripe_secret_key") == SECRET_MASK, (
        "stripe_secret_key should be masked in GET responses"
    )

    # Non-secret keys should pass through
    if "vat_rate" in settings:
        assert settings["vat_rate"] != SECRET_MASK


def test_patch_with_mask_string_does_not_clobber_real_secret():
    """If the dashboard saves the form without retyping, body.value is
    the literal '********'. We must NOT overwrite the real secret."""
    # Plant a known-good real secret
    r = _client.patch(
        "/admin/api/orgs/just-print/settings/stripe_webhook_secret",
        headers=_auth(),
        json={"value": "whsec_real_value_keep_me", "value_type": "string"},
    )
    assert r.status_code == 200

    # Try to "save" with the mask
    r = _client.patch(
        "/admin/api/orgs/just-print/settings/stripe_webhook_secret",
        headers=_auth(),
        json={"value": SECRET_MASK, "value_type": "string"},
    )
    assert r.status_code == 200  # no-op, not error

    # Verify the real secret is still there by reading it via the
    # pricing_engine internal helper (which decrypts)
    from db import db_session
    from pricing_engine import _get_setting
    with db_session() as s:
        actual = _get_setting(s, "stripe_webhook_secret", default="", organization_slug="just-print")
    assert actual == "whsec_real_value_keep_me", (
        "saving '********' must not overwrite the real secret"
    )


def test_secret_value_stored_encrypted_in_db():
    """The raw DB row should hold ciphertext (enc::v1::...) — not the
    plaintext sk_test_. Direct DB read confirms encryption-at-rest."""
    _client.patch(
        "/admin/api/orgs/just-print/settings/printlogic_api_key",
        headers=_auth(),
        json={"value": "GA5_real_printlogic_key_for_test", "value_type": "string"},
    )

    from db import db_session
    from db.models import Setting
    with db_session() as s:
        row = (
            s.query(Setting)
            .filter_by(organization_slug="just-print", key="printlogic_api_key")
            .first()
        )
        assert row is not None
        raw_value = row.value
    assert raw_value.startswith("enc::v1::"), (
        f"Expected encrypted value, got: {raw_value[:30]}..."
    )
    assert "GA5_real_printlogic_key_for_test" not in raw_value


def test_pricing_engine_decrypts_transparently():
    """`_get_setting` must return decrypted plaintext to internal callers."""
    _client.patch(
        "/admin/api/orgs/just-print/settings/missive_api_token",
        headers=_auth(),
        json={"value": "missive_token_xyz_789", "value_type": "string"},
    )

    from db import db_session
    from pricing_engine import _get_setting
    with db_session() as s:
        actual = _get_setting(s, "missive_api_token", default="", organization_slug="just-print")
    assert actual == "missive_token_xyz_789"


def test_non_secret_setting_not_encrypted():
    """system_prompt and similar non-secret settings stay plaintext in DB."""
    _client.patch(
        "/admin/api/orgs/just-print/settings/system_prompt",
        headers=_auth(),
        json={"value": "You are a test prompt", "value_type": "string"},
    )

    from db import db_session
    from db.models import Setting
    with db_session() as s:
        row = (
            s.query(Setting)
            .filter_by(organization_slug="just-print", key="system_prompt")
            .first()
        )
        assert row is not None
        raw_value = row.value
    assert not raw_value.startswith("enc::v1::")
    assert raw_value == "You are a test prompt"
