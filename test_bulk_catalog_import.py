"""
Tests for v40.2 — bulk catalog import (admin_api.py bulk product + surcharge endpoints).

We exercise the endpoints via the FastAPI TestClient against the real
in-process app + SQLite test DB (same fixture pattern as the rest of
the admin_api tests, e.g. the JWT helper from test_admin_api.py).

Coverage groups:
  1. Bulk products — happy path, dry_run, skip default, update replaces
     tiers, fail rejects duplicates, invalid pricing_strategy row error,
     duplicate key within upload, category case-normalization, auto-
     created categories with title-cased names, per-row savepoint
     isolation, client_member 403.
  2. Bulk surcharges — happy path, conflict skip, applies_to_product_keys
     validation against DB + X-Known-Keys header.
  3. v38 fields round-trip — requires_dimensions + sanity_max_unit_price
     persisted via bulk endpoint AND via single create endpoint.
"""

from __future__ import annotations

import os
import time
import jwt

# Ensure imports that look at the env at module-load time see a value.
os.environ.setdefault("STRATEGOS_JWT_SECRET", "test-secret-32b-pad-enough-now")

import pytest
from fastapi.testclient import TestClient

from app import app
from db import db_session
from db.models import (
    Category, DEFAULT_ORG_SLUG, PriceTier, Product, SurchargeRule,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _token(role: str = "client_owner", org: str = DEFAULT_ORG_SLUG) -> str:
    """Mint a JWT matching auth.jwt_auth.require_claims expectations."""
    now = int(time.time())
    return jwt.encode(
        {
            "email": f"test-{role}@strategos-ai.com",
            "org_slug": org,
            "role": role,
            "iat": now, "exp": now + 300,
            "iss": "strategos-dashboard",
            "sub": f"test-{role}@strategos-ai.com",
        },
        os.environ["STRATEGOS_JWT_SECRET"],
        algorithm="HS256",
    )


def _auth_headers(role: str = "client_owner") -> dict[str, str]:
    return {"Authorization": f"Bearer {_token(role)}"}


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def clean_catalog():
    """Wipe products + tiers + surcharges + categories (except the seeded
    just-print baseline rows) so each test starts from a known state."""
    with db_session() as db:
        # Delete only the rows this test suite would touch — leaving the
        # baseline catalog intact.
        for prod_key in (
            "biz_cards_v402", "flyers_v402", "vinyl_v402", "roller_v402",
            "foamex_v402", "bad_strategy_v402", "merge_cat_a_v402",
            "merge_cat_b_v402", "savepoint_ok_a", "savepoint_ok_b",
            "savepoint_bad", "v38_fields_test",
        ):
            existing = db.query(Product).filter_by(
                organization_slug=DEFAULT_ORG_SLUG, key=prod_key,
            ).first()
            if existing:
                db.delete(existing)
        for cat_slug in (
            "v402_test_cat", "small_format_v402", "v402_new_category",
            "v402_merged_cat",
        ):
            existing = db.query(Category).filter_by(
                organization_slug=DEFAULT_ORG_SLUG, slug=cat_slug,
            ).first()
            if existing:
                db.delete(existing)
        for sc_name in (
            "v402_test_surcharge", "v402_skip_surcharge",
            "v402_unknown_keys_surcharge", "v402_known_keys_surcharge",
        ):
            existing = db.query(SurchargeRule).filter_by(
                organization_slug=DEFAULT_ORG_SLUG, name=sc_name,
            ).first()
            if existing:
                db.delete(existing)
        # Ensure the helper category exists so non-cat tests don't trip
        # the "missing category" guard.
        cat = db.query(Category).filter_by(
            organization_slug=DEFAULT_ORG_SLUG, slug="v402_test_cat",
        ).first()
        if not cat:
            db.add(Category(
                organization_slug=DEFAULT_ORG_SLUG,
                slug="v402_test_cat", name="V402 Test Category",
            ))
        db.commit()
    yield


URL_PRODUCTS = f"/admin/api/orgs/{DEFAULT_ORG_SLUG}/products/bulk"
URL_SURCHARGES = f"/admin/api/orgs/{DEFAULT_ORG_SLUG}/surcharges/bulk"


# ---------------------------------------------------------------------------
# 1. Bulk products
# ---------------------------------------------------------------------------


class TestBulkProductsHappy:
    def test_creates_products_and_tiers(self, client, clean_catalog):
        payload = {
            "products": [
                {
                    "name": "Biz Cards v402",
                    "key": "biz_cards_v402",
                    "category": "v402_test_cat",
                    "pricing_strategy": "tiered",
                    "min_qty": 100,
                    "double_sided_surcharge": True,
                    "tiers": [
                        {"spec_key": "", "quantity": 100, "price": 35.00},
                        {"spec_key": "", "quantity": 250, "price": 49.00},
                    ],
                },
                {
                    "name": "Roller v402",
                    "key": "roller_v402",
                    "category": "v402_test_cat",
                    "pricing_strategy": "per_unit",
                    "min_qty": 1,
                    "double_sided_surcharge": False,
                    "unit_price": 89.00,
                    "tiers": [],
                },
            ],
            "conflict_policy": "skip",
            "dry_run": False,
        }
        r = client.post(URL_PRODUCTS, json=payload, headers=_auth_headers())
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["summary"] == {
            "created": 2, "updated": 0, "skipped": 0, "failed": 0,
        }
        assert len(data["ok"]) == 2
        assert data["ok"][0]["action"] == "created"
        assert data["ok"][0]["tier_count"] == 2
        assert data["ok"][1]["tier_count"] == 0
        # Verify DB
        with db_session() as db:
            p = db.query(Product).filter_by(
                organization_slug=DEFAULT_ORG_SLUG, key="biz_cards_v402",
            ).first()
            assert p is not None
            tiers = db.query(PriceTier).filter_by(product_id=p.id).all()
            assert len(tiers) == 2

    def test_dry_run_does_not_commit(self, client, clean_catalog):
        payload = {
            "products": [{
                "name": "Dry Run Test",
                "key": "biz_cards_v402",
                "category": "v402_test_cat",
                "pricing_strategy": "per_unit",
                "min_qty": 1,
                "double_sided_surcharge": False,
                "unit_price": 10.0,
                "tiers": [],
            }],
            "dry_run": True,
        }
        r = client.post(URL_PRODUCTS, json=payload, headers=_auth_headers())
        assert r.status_code == 200, r.text
        data = r.json()
        # Response reports success — but DB is unchanged.
        assert data["summary"]["created"] == 1
        with db_session() as db:
            assert db.query(Product).filter_by(
                organization_slug=DEFAULT_ORG_SLUG, key="biz_cards_v402",
            ).first() is None


class TestBulkProductsConflictPolicies:
    def test_skip_leaves_existing_untouched(self, client, clean_catalog):
        # Pre-seed one product
        with db_session() as db:
            db.add(Product(
                organization_slug=DEFAULT_ORG_SLUG,
                key="biz_cards_v402",
                name="Original Name",
                category="v402_test_cat",
                pricing_strategy="per_unit",
                unit_price=10.0,
                min_qty=1,
            ))
            db.commit()

        payload = {
            "products": [{
                "name": "Updated Name",   # different from existing
                "key": "biz_cards_v402",
                "category": "v402_test_cat",
                "pricing_strategy": "per_unit",
                "min_qty": 1,
                "double_sided_surcharge": False,
                "unit_price": 99.99,      # different from existing
                "tiers": [],
            }],
            "conflict_policy": "skip",
        }
        r = client.post(URL_PRODUCTS, json=payload, headers=_auth_headers())
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["summary"]["skipped"] == 1
        assert data["ok"][0]["action"] == "skipped"
        # Verify DB unchanged
        with db_session() as db:
            p = db.query(Product).filter_by(
                organization_slug=DEFAULT_ORG_SLUG, key="biz_cards_v402",
            ).first()
            assert p.name == "Original Name"
            assert p.unit_price == 10.0

    def test_update_replaces_fields_and_tiers(self, client, clean_catalog):
        with db_session() as db:
            p = Product(
                organization_slug=DEFAULT_ORG_SLUG,
                key="biz_cards_v402",
                name="Old Name",
                category="v402_test_cat",
                pricing_strategy="tiered",
                min_qty=100,
            )
            db.add(p); db.flush()
            db.add(PriceTier(
                organization_slug=DEFAULT_ORG_SLUG,
                product_id=p.id, spec_key="", quantity=999, price=999.0,
            ))
            db.commit()

        payload = {
            "products": [{
                "name": "New Name",
                "key": "biz_cards_v402",
                "category": "v402_test_cat",
                "pricing_strategy": "tiered",
                "min_qty": 100,
                "double_sided_surcharge": True,
                "tiers": [
                    {"spec_key": "", "quantity": 100, "price": 35.00},
                    {"spec_key": "", "quantity": 250, "price": 49.00},
                ],
            }],
            "conflict_policy": "update",
        }
        r = client.post(URL_PRODUCTS, json=payload, headers=_auth_headers())
        assert r.status_code == 200, r.text
        assert r.json()["summary"]["updated"] == 1
        with db_session() as db:
            p = db.query(Product).filter_by(
                organization_slug=DEFAULT_ORG_SLUG, key="biz_cards_v402",
            ).first()
            assert p.name == "New Name"
            tiers = db.query(PriceTier).filter_by(product_id=p.id).all()
            qtys = sorted(t.quantity for t in tiers)
            assert qtys == [100, 250]    # old qty=999 tier wiped

    def test_fail_records_conflict(self, client, clean_catalog):
        with db_session() as db:
            db.add(Product(
                organization_slug=DEFAULT_ORG_SLUG,
                key="biz_cards_v402", name="Existing",
                category="v402_test_cat", pricing_strategy="per_unit",
                unit_price=10.0, min_qty=1,
            ))
            db.commit()

        payload = {
            "products": [{
                "name": "Attempt", "key": "biz_cards_v402",
                "category": "v402_test_cat", "pricing_strategy": "per_unit",
                "min_qty": 1, "double_sided_surcharge": False,
                "unit_price": 20.0, "tiers": [],
            }],
            "conflict_policy": "fail",
        }
        r = client.post(URL_PRODUCTS, json=payload, headers=_auth_headers())
        assert r.status_code == 200, r.text
        assert r.json()["summary"]["failed"] == 1
        assert r.json()["failed"][0]["error"] == "product key already exists"


class TestBulkProductsValidation:
    def test_invalid_pricing_strategy_zod_caught_at_pydantic(self, client, clean_catalog):
        payload = {
            "products": [{
                "name": "Bad strategy",
                "key": "bad_strategy_v402",
                "category": "v402_test_cat",
                "pricing_strategy": "bogus",
                "min_qty": 1,
                "double_sided_surcharge": False,
                "tiers": [],
            }],
        }
        r = client.post(URL_PRODUCTS, json=payload, headers=_auth_headers())
        # Pydantic catches the bad enum BEFORE the endpoint body runs.
        assert r.status_code == 422, r.text

    def test_per_strategy_check_per_sqm_missing_unit_price(self, client, clean_catalog):
        payload = {
            "products": [{
                "name": "Vinyl missing price",
                "key": "vinyl_v402",
                "category": "v402_test_cat",
                "pricing_strategy": "per_sqm",
                "min_qty": 1,
                "double_sided_surcharge": False,
                # Note: NO unit_price → should be flagged at row level
                "tiers": [],
            }],
        }
        r = client.post(URL_PRODUCTS, json=payload, headers=_auth_headers())
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["summary"]["failed"] == 1
        assert "unit_price" in data["failed"][0]["error"]

    def test_duplicate_key_within_upload_both_flagged(self, client, clean_catalog):
        payload = {
            "products": [
                {
                    "name": "First", "key": "biz_cards_v402",
                    "category": "v402_test_cat", "pricing_strategy": "per_unit",
                    "min_qty": 1, "double_sided_surcharge": False,
                    "unit_price": 10.0, "tiers": [],
                },
                {
                    "name": "Dup", "key": "biz_cards_v402",
                    "category": "v402_test_cat", "pricing_strategy": "per_unit",
                    "min_qty": 1, "double_sided_surcharge": False,
                    "unit_price": 20.0, "tiers": [],
                },
            ],
        }
        r = client.post(URL_PRODUCTS, json=payload, headers=_auth_headers())
        assert r.status_code == 200, r.text
        data = r.json()
        # Both rows flagged in pre-flight
        assert data["summary"]["failed"] == 2
        assert all("duplicate_within_upload" in f["error"] for f in data["failed"])


class TestBulkProductsCategoryNormalization:
    def test_mixed_case_categories_merge_to_one_slug(self, client, clean_catalog):
        # Justin types "Small Format" in row 1, "small format" in row 2.
        # Both should normalize to slug `small_format` and produce ONE
        # new category (not two), reused across both products.
        payload = {
            "products": [
                {
                    "name": "Merge Cat A", "key": "merge_cat_a_v402",
                    "category": "Small Format",     # mixed case
                    "pricing_strategy": "per_unit",
                    "min_qty": 1, "double_sided_surcharge": False,
                    "unit_price": 10.0, "tiers": [],
                },
                {
                    "name": "Merge Cat B", "key": "merge_cat_b_v402",
                    "category": "small format",     # lowercase + space
                    "pricing_strategy": "per_unit",
                    "min_qty": 1, "double_sided_surcharge": False,
                    "unit_price": 20.0, "tiers": [],
                },
            ],
        }
        r = client.post(URL_PRODUCTS, json=payload, headers=_auth_headers())
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["summary"]["created"] == 2
        # Only ONE category created in this batch
        # (the seeded baseline `small_format` already exists from the
        # `just-print` v2 migration — but we wipe any v402_* leftovers,
        # so this category is the production small_format).
        # Verify both products point at slug `small_format`.
        with db_session() as db:
            a = db.query(Product).filter_by(
                organization_slug=DEFAULT_ORG_SLUG, key="merge_cat_a_v402",
            ).first()
            b = db.query(Product).filter_by(
                organization_slug=DEFAULT_ORG_SLUG, key="merge_cat_b_v402",
            ).first()
            assert a.category == "small_format"
            assert b.category == "small_format"

    def test_new_category_gets_title_cased_display_name(self, client, clean_catalog):
        # Operator types a brand-new category in mixed case.
        # Verify the auto-created Category row has the title-cased
        # display name (not the raw slug).
        payload = {
            "products": [{
                "name": "Cat Test",
                "key": "biz_cards_v402",
                "category": "V402 New Category",   # → slug v402_new_category
                "pricing_strategy": "per_unit",
                "min_qty": 1, "double_sided_surcharge": False,
                "unit_price": 10.0, "tiers": [],
            }],
        }
        r = client.post(URL_PRODUCTS, json=payload, headers=_auth_headers())
        assert r.status_code == 200, r.text
        data = r.json()
        assert "v402_new_category" in data["categories_created"]
        with db_session() as db:
            cat = db.query(Category).filter_by(
                organization_slug=DEFAULT_ORG_SLUG,
                slug="v402_new_category",
            ).first()
            assert cat is not None
            # Title-cased from the operator's typed value
            assert cat.name == "V402 New Category"


class TestBulkProductsSavepointIsolation:
    def test_one_bad_row_does_not_abort_the_other_three(self, client, clean_catalog):
        # 4 rows, row index 2 has an invalid strategy → only that row
        # fails, the other 3 commit (no all-or-nothing).
        payload = {
            "products": [
                {
                    "name": "OK A", "key": "savepoint_ok_a",
                    "category": "v402_test_cat", "pricing_strategy": "per_unit",
                    "min_qty": 1, "double_sided_surcharge": False,
                    "unit_price": 10.0, "tiers": [],
                },
                {
                    "name": "OK B", "key": "savepoint_ok_b",
                    "category": "v402_test_cat", "pricing_strategy": "per_unit",
                    "min_qty": 1, "double_sided_surcharge": False,
                    "unit_price": 20.0, "tiers": [],
                },
                {
                    "name": "Bad — per_sheet missing config",
                    "key": "savepoint_bad",
                    "category": "v402_test_cat",
                    "pricing_strategy": "per_sheet",   # needs sheet_size_mm + sheet_price
                    "min_qty": 1, "double_sided_surcharge": False,
                    "tiers": [],
                },
                {
                    "name": "OK D", "key": "vinyl_v402",
                    "category": "v402_test_cat", "pricing_strategy": "per_sqm",
                    "min_qty": 1, "double_sided_surcharge": False,
                    "unit_price": 45.0, "tiers": [],
                },
            ],
        }
        r = client.post(URL_PRODUCTS, json=payload, headers=_auth_headers())
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["summary"]["created"] == 3
        assert data["summary"]["failed"] == 1
        assert data["failed"][0]["row"] == 2
        # The 3 ok rows are persisted
        with db_session() as db:
            for k in ("savepoint_ok_a", "savepoint_ok_b", "vinyl_v402"):
                assert db.query(Product).filter_by(
                    organization_slug=DEFAULT_ORG_SLUG, key=k,
                ).first() is not None
            assert db.query(Product).filter_by(
                organization_slug=DEFAULT_ORG_SLUG, key="savepoint_bad",
            ).first() is None


class TestBulkProductsAuth:
    def test_client_member_gets_403(self, client, clean_catalog):
        payload = {
            "products": [{
                "name": "Whatever", "key": "biz_cards_v402",
                "category": "v402_test_cat", "pricing_strategy": "per_unit",
                "min_qty": 1, "double_sided_surcharge": False,
                "unit_price": 10.0, "tiers": [],
            }],
        }
        r = client.post(URL_PRODUCTS, json=payload, headers=_auth_headers("client_member"))
        assert r.status_code == 403


# ---------------------------------------------------------------------------
# 2. Bulk surcharges
# ---------------------------------------------------------------------------


class TestBulkSurcharges:
    def test_creates_surcharges(self, client, clean_catalog):
        # Pre-seed a product that surcharges can reference
        with db_session() as db:
            db.add(Product(
                organization_slug=DEFAULT_ORG_SLUG,
                key="biz_cards_v402", name="BC v402",
                category="v402_test_cat", pricing_strategy="per_unit",
                unit_price=10.0, min_qty=1,
            ))
            db.commit()

        payload = {
            "surcharges": [{
                "name": "v402_test_surcharge",
                "multiplier": 0.20,
                "kind": "multiplier",
                "applies_to_category": "v402_test_cat",
                "applies_to_product_keys": ["biz_cards_v402"],
                "description": "Test surcharge v40.2",
            }],
        }
        r = client.post(URL_SURCHARGES, json=payload, headers=_auth_headers())
        assert r.status_code == 200, r.text
        assert r.json()["summary"]["created"] == 1
        with db_session() as db:
            s = db.query(SurchargeRule).filter_by(
                organization_slug=DEFAULT_ORG_SLUG, name="v402_test_surcharge",
            ).first()
            assert s is not None
            assert s.multiplier == 0.20

    def test_skip_default_leaves_existing(self, client, clean_catalog):
        with db_session() as db:
            db.add(SurchargeRule(
                organization_slug=DEFAULT_ORG_SLUG,
                name="v402_skip_surcharge",
                multiplier=0.10, kind="multiplier",
                description="original",
            ))
            db.commit()
        payload = {
            "surcharges": [{
                "name": "v402_skip_surcharge",
                "multiplier": 0.99,    # different
                "kind": "multiplier",
                "description": "would-overwrite",
            }],
            "conflict_policy": "skip",
        }
        r = client.post(URL_SURCHARGES, json=payload, headers=_auth_headers())
        assert r.status_code == 200, r.text
        assert r.json()["summary"]["skipped"] == 1
        with db_session() as db:
            s = db.query(SurchargeRule).filter_by(
                organization_slug=DEFAULT_ORG_SLUG, name="v402_skip_surcharge",
            ).first()
            # Unchanged
            assert s.multiplier == 0.10
            assert s.description == "original"

    def test_unknown_product_keys_flagged_per_row(self, client, clean_catalog):
        payload = {
            "surcharges": [{
                "name": "v402_unknown_keys_surcharge",
                "multiplier": 0.20,
                "kind": "multiplier",
                "applies_to_product_keys": ["does_not_exist_v402"],
                "description": "test",
            }],
        }
        r = client.post(URL_SURCHARGES, json=payload, headers=_auth_headers())
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["summary"]["failed"] == 1
        assert "unknown" in data["failed"][0]["error"].lower()

    def test_x_known_keys_header_accepts_just_created_products(self, client, clean_catalog):
        # The dashboard's commit sequence runs products bulk first, then
        # surcharges with X-Known-Keys: <new product keys>. Verify that
        # a surcharge referencing a key NOT in DB but IN the header
        # passes cross-validation.
        payload = {
            "surcharges": [{
                "name": "v402_known_keys_surcharge",
                "multiplier": 15.00,
                "kind": "additive",
                "applies_to_product_keys": ["biz_cards_v402"],
                "description": "depends on just-created product",
            }],
        }
        headers = {
            **_auth_headers(),
            "X-Known-Keys": "biz_cards_v402, other_v402",
        }
        r = client.post(URL_SURCHARGES, json=payload, headers=headers)
        assert r.status_code == 200, r.text
        assert r.json()["summary"]["created"] == 1


# ---------------------------------------------------------------------------
# 3. v38 fields round-trip
# ---------------------------------------------------------------------------


class TestV38FieldsRoundTrip:
    def test_bulk_endpoint_persists_requires_dimensions_and_sanity_ceiling(
        self, client, clean_catalog,
    ):
        payload = {
            "products": [{
                "name": "V38 fields test",
                "key": "v38_fields_test",
                "category": "v402_test_cat",
                "pricing_strategy": "per_sqm",
                "min_qty": 1,
                "double_sided_surcharge": False,
                "unit_price": 45.0,
                "requires_dimensions": True,
                "sanity_max_unit_price": 0.50,
                "tiers": [],
            }],
        }
        r = client.post(URL_PRODUCTS, json=payload, headers=_auth_headers())
        assert r.status_code == 200, r.text
        with db_session() as db:
            p = db.query(Product).filter_by(
                organization_slug=DEFAULT_ORG_SLUG, key="v38_fields_test",
            ).first()
            assert p is not None
            assert p.requires_dimensions is True
            assert p.sanity_max_unit_price == 0.50

    def test_single_create_endpoint_persists_v38_fields(self, client, clean_catalog):
        # Same fields via the existing single-create endpoint —
        # verify the v40.2 extension flowed through cleanly.
        payload = {
            "name": "V38 single create",
            "key": "v38_fields_test",
            "category": "v402_test_cat",
            "pricing_strategy": "per_sqm",
            "min_qty": 1,
            "double_sided_surcharge": False,
            "unit_price": 45.0,
            "requires_dimensions": True,
            "sanity_max_unit_price": 0.75,
        }
        r = client.post(
            f"/admin/api/orgs/{DEFAULT_ORG_SLUG}/products",
            json=payload, headers=_auth_headers(),
        )
        assert r.status_code == 201, r.text
        with db_session() as db:
            p = db.query(Product).filter_by(
                organization_slug=DEFAULT_ORG_SLUG, key="v38_fields_test",
            ).first()
            assert p.requires_dimensions is True
            assert p.sanity_max_unit_price == 0.75
