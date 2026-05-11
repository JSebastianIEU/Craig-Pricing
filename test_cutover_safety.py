"""
Tests for v37.7 cutover-safety migration.

Critical invariants:
1. First-time config (no last_known snapshot): snapshot recorded, no
   auto-OFF (it's initial setup, not a cutover).
2. Operator changes missive_from_address AND missive_enabled=true →
   migration flips missive_enabled to false.
3. Idempotent: re-running on stable state is a no-op.
4. internal_team_domains seeded with `["just-print.ie"]` for the
   just-print tenant; empty list for others.
"""

from __future__ import annotations

import json
import os

import pytest

os.environ.setdefault("STRATEGOS_JWT_SECRET", "test-secret-32b-pad-enough-now")

from db import db_session  # noqa: E402
from db.models import Setting  # noqa: E402
from scripts.v37_7_cutover_safety import migrate_for_tenant  # noqa: E402


def _clear_settings(db, org_slug: str, keys: list[str]) -> None:
    """Wipe specific Setting rows for an org so each test starts clean."""
    for key in keys:
        rows = db.query(Setting).filter_by(
            organization_slug=org_slug, key=key,
        ).all()
        for r in rows:
            db.delete(r)
    db.commit()


def _set(db, org_slug: str, key: str, value: str, value_type: str = "string") -> None:
    row = db.query(Setting).filter_by(
        organization_slug=org_slug, key=key,
    ).first()
    if row is None:
        db.add(Setting(
            organization_slug=org_slug, key=key,
            value=value, value_type=value_type,
        ))
    else:
        row.value = value
        row.value_type = value_type
    db.commit()


def _get(db, org_slug: str, key: str):
    return db.query(Setting).filter_by(
        organization_slug=org_slug, key=key,
    ).first()


_RELEVANT_KEYS = [
    "missive_enabled",
    "missive_from_address",
    "missive_from_address_last_known",
    "internal_team_domains",
    "internal_team_addresses",
]


@pytest.fixture
def org_slug():
    """Use a dedicated test org to avoid polluting just-print state."""
    slug = "cutover-test-tenant"
    with db_session() as db:
        _clear_settings(db, slug, _RELEVANT_KEYS)
    yield slug
    with db_session() as db:
        _clear_settings(db, slug, _RELEVANT_KEYS)


class TestSeedDefaults:
    def test_internal_team_domains_seeded_empty_for_unknown_tenant(self, org_slug):
        with db_session() as db:
            migrate_for_tenant(db, org_slug)
            db.commit()
            row = _get(db, org_slug, "internal_team_domains")
            assert row is not None
            assert json.loads(row.value) == []

    def test_internal_team_domains_seeded_with_justprint_for_just_print(self):
        slug = "just-print-test-clean"
        with db_session() as db:
            _clear_settings(db, slug, _RELEVANT_KEYS)
        try:
            # Re-using just-print's seed logic requires the slug match.
            # We'll directly test the seed code path by passing the
            # actual default org_slug. Skip if the prod just-print row
            # already exists with non-empty value.
            from db.models import DEFAULT_ORG_SLUG
            with db_session() as db:
                # Snapshot pre-existing state so we don't trample.
                pre = _get(db, DEFAULT_ORG_SLUG, "internal_team_domains")
                pre_value = pre.value if pre else None
                if pre is not None:
                    db.delete(pre)
                    db.commit()
                migrate_for_tenant(db, DEFAULT_ORG_SLUG)
                db.commit()
                row = _get(db, DEFAULT_ORG_SLUG, "internal_team_domains")
                assert row is not None
                assert "just-print.ie" in json.loads(row.value)
                # Restore if there was a prior value.
                if pre_value is not None:
                    row.value = pre_value
                    db.commit()
        finally:
            with db_session() as db:
                _clear_settings(db, slug, _RELEVANT_KEYS)

    def test_internal_team_addresses_seeded_empty(self, org_slug):
        with db_session() as db:
            migrate_for_tenant(db, org_slug)
            db.commit()
            row = _get(db, org_slug, "internal_team_addresses")
            assert row is not None
            assert json.loads(row.value) == []


class TestCutoverSafety:
    def test_first_setup_no_from_address_records_empty_sentinel(self, org_slug):
        """Initial state: no from_address configured yet. Migration
        records an empty sentinel so the first real config doesn't
        accidentally trigger auto-OFF."""
        with db_session() as db:
            migrate_for_tenant(db, org_slug)
            db.commit()
            row = _get(db, org_slug, "missive_from_address_last_known")
            assert row is not None
            assert row.value == ""

    def test_first_config_no_autooff(self, org_slug):
        """Operator configures from_address for the first time AFTER
        the migration has already run. The next migration run snapshots
        the value WITHOUT toggling enabled off (it's initial setup,
        not a cutover)."""
        with db_session() as db:
            _set(db, org_slug, "missive_from_address", "info@just-print.ie")
            _set(db, org_slug, "missive_enabled", "true")
            migrate_for_tenant(db, org_slug)
            db.commit()

            enabled = _get(db, org_slug, "missive_enabled")
            assert enabled.value == "true", "Initial config should NOT auto-OFF"

            snapshot = _get(db, org_slug, "missive_from_address_last_known")
            assert snapshot.value == "info@just-print.ie"

    def test_cutover_with_enabled_true_flips_to_false(self, org_slug):
        """The CORE invariant: when from_address changes from a known
        value to a different value AND enabled is currently true, the
        migration must flip enabled to false. Justin's reflex toggle
        is now the only path back to ON."""
        with db_session() as db:
            # Phase 1: initial config
            _set(db, org_slug, "missive_from_address", "sebastian@strategos-ai.com")
            _set(db, org_slug, "missive_enabled", "true")
            migrate_for_tenant(db, org_slug)
            db.commit()

            # Phase 2: operator changes from_address (cutover!)
            _set(db, org_slug, "missive_from_address", "info@just-print.ie")
            migrate_for_tenant(db, org_slug)
            db.commit()

            enabled = _get(db, org_slug, "missive_enabled")
            assert enabled.value == "false", (
                "Cutover MUST auto-OFF — Craig was left ON after pointing "
                "at a new from_address. This is the bug v37.7 prevents."
            )
            snapshot = _get(db, org_slug, "missive_from_address_last_known")
            assert snapshot.value == "info@just-print.ie"

    def test_cutover_with_enabled_false_is_noop_on_enabled(self, org_slug):
        """If Craig is already OFF when cutover happens, the migration
        just updates the snapshot — no spurious enabled flip."""
        with db_session() as db:
            _set(db, org_slug, "missive_from_address", "sebastian@strategos-ai.com")
            _set(db, org_slug, "missive_enabled", "false")
            migrate_for_tenant(db, org_slug)
            db.commit()

            _set(db, org_slug, "missive_from_address", "info@just-print.ie")
            migrate_for_tenant(db, org_slug)
            db.commit()

            enabled = _get(db, org_slug, "missive_enabled")
            assert enabled.value == "false"  # unchanged
            snapshot = _get(db, org_slug, "missive_from_address_last_known")
            assert snapshot.value == "info@just-print.ie"

    def test_idempotent_after_cutover(self, org_slug):
        """Once cutover has fired and been recorded, subsequent boots
        don't re-fire. Even if the operator manually flips Craig back
        ON, the next migration shouldn't auto-OFF again (there's no
        new cutover)."""
        with db_session() as db:
            # Set up cutover state
            _set(db, org_slug, "missive_from_address", "sebastian@strategos-ai.com")
            _set(db, org_slug, "missive_enabled", "true")
            migrate_for_tenant(db, org_slug)
            db.commit()
            _set(db, org_slug, "missive_from_address", "info@just-print.ie")
            migrate_for_tenant(db, org_slug)
            db.commit()

            # Operator manually re-enables after smoke-testing
            _set(db, org_slug, "missive_enabled", "true")

            # Re-run migration — should be a no-op
            migrate_for_tenant(db, org_slug)
            db.commit()

            enabled = _get(db, org_slug, "missive_enabled")
            assert enabled.value == "true", (
                "Re-running migration on stable state should NOT auto-OFF"
            )
