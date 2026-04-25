"""
Single source of truth for "which Setting keys are secrets" and how we
handle them in API responses + at rest.

Used by:
  - admin_api._setting_to_dict     (mask in GET responses)
  - admin_api.update_setting       (reject saves of the literal mask)
  - secrets_crypto                 (encrypt-at-rest before persisting)

Why this lives in its own module: the mask + the encryption + the audit
log all key off the same allowlist. Putting it in admin_api.py would
give DB code a circular import; putting it in db/models.py would mix
schema with policy. This is policy.
"""

from __future__ import annotations

# Setting keys that hold secrets. Values for these keys are:
#   1. Encrypted at rest via secrets_crypto
#   2. Returned as the SECRET_MASK string in GET responses
#   3. Rejected by PATCH if the body value is the literal mask
#      (prevents the dashboard's round-trip from overwriting a real
#      secret with "********" if the user clicks Save without retyping)
SECRET_KEYS: frozenset[str] = frozenset({
    "stripe_secret_key",
    "stripe_webhook_secret",
    "printlogic_api_key",
    "missive_api_token",
    "missive_webhook_secret",
})

# The exact string returned in place of secret values in API responses.
# Frontend can detect this and render a "leave blank to keep current"
# affordance, OR the user retypes the full secret to overwrite.
SECRET_MASK = "********"


def is_secret(key: str) -> bool:
    """True if `key` should be treated as a secret in transit + at rest."""
    return key in SECRET_KEYS


def mask_value(key: str, value: str | None) -> str | None:
    """
    Return what should go on the wire for this (key, value) pair.

    - If `key` is not a secret → pass through unchanged.
    - If `key` is a secret AND value is a real secret → return SECRET_MASK.
    - If `key` is a secret AND value is empty/None → return value as-is
      (so the dashboard can distinguish "not yet set" from "set & masked").
    """
    if not is_secret(key):
        return value
    if not value:
        return value
    return SECRET_MASK


def is_mask(value: str | None) -> bool:
    """
    True iff `value` is the literal mask string. Lets the PATCH endpoint
    skip the write — saving '********' as a real secret would clobber the
    actual value with garbage.
    """
    return value == SECRET_MASK
