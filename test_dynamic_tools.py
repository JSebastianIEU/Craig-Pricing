"""
Tests for v40.4 — dynamic tool enums.

The LLM tool definitions (`TOOLS` in llm/craig_agent.py) historically
hard-coded the ``product_key`` enum for ``quote_small_format`` and
``quote_large_format``. That broke as soon as Justin added new
products via the v40.2 bulk import: DeepSeek would either refuse to
call the pricing tool or escalate, because the new product's key
wasn't in the static enum.

v40.4 wraps the static array in ``_build_tools_for_org(db, slug)``
which clones the tool list per chat turn and replaces those two
enums with the live keys from the tenant's catalog. These tests
cover:

  1. Enum reflects DB state — a brand-new product appears in the
     small_format enum immediately, no restart needed.
  2. Empty category → enum dropped entirely (so the LLM doesn't see
     ``enum: []`` which it would interpret as "you may not call me").
  3. Multi-tenant isolation — adding a product to org A doesn't
     leak into org B's tool list.
  4. ``quote_booklet`` is left untouched — its enums (a5/a4,
     saddle_stitch/perfect_bound) are fundamental structure, not
     catalog data.
  5. The module-level ``TOOLS`` constant is not mutated by the
     builder (concurrent chat turns share the constant).
"""

from __future__ import annotations

import os

os.environ.setdefault("STRATEGOS_JWT_SECRET", "test-secret-32b-pad-enough-now")

import pytest
from db import db_session
from db.models import Product, DEFAULT_ORG_SLUG


_DYNAMIC_TEST_KEYS = (
    "dyntool_test_sf_one",
    "dyntool_test_sf_two",
    "dyntool_test_lf_one",
    "dyntool_test_other_tenant_sf",
    "dyntool_test_empty_cat",
)


@pytest.fixture
def fresh_dynamic_state():
    """Wipe the test products this file creates and reset to a known state.
    Keeps the production seed (just-print's real products) intact."""
    with db_session() as db:
        for key in _DYNAMIC_TEST_KEYS:
            for p in db.query(Product).filter(Product.key == key).all():
                db.delete(p)
        db.commit()
    yield
    # Same cleanup after the test
    with db_session() as db:
        for key in _DYNAMIC_TEST_KEYS:
            for p in db.query(Product).filter(Product.key == key).all():
                db.delete(p)
        db.commit()


def _get_tool(tools: list, name: str) -> dict | None:
    for t in tools:
        fn = t.get("function", {})
        if fn.get("name") == name:
            return t
    return None


def _enum_for(tools: list, name: str) -> list[str] | None:
    t = _get_tool(tools, name)
    if not t:
        return None
    spec = (
        t.get("function", {})
        .get("parameters", {})
        .get("properties", {})
        .get("product_key", {})
    )
    return spec.get("enum")


# ---------------------------------------------------------------------------
# 1. Enum reflects DB state
# ---------------------------------------------------------------------------


class TestEnumReflectsDB:
    def test_new_small_format_product_appears_in_enum(self, fresh_dynamic_state):
        from llm.craig_agent import _build_tools_for_org
        with db_session() as db:
            db.add(Product(
                organization_slug=DEFAULT_ORG_SLUG,
                key="dyntool_test_sf_one",
                name="Dyntool Test SF One",
                category="small_format",
                pricing_strategy="tiered",
                min_qty=1,
            ))
            db.commit()
        with db_session() as db:
            tools = _build_tools_for_org(db, DEFAULT_ORG_SLUG)
        enum = _enum_for(tools, "quote_small_format") or []
        assert "dyntool_test_sf_one" in enum

    def test_new_large_format_product_appears_in_enum(self, fresh_dynamic_state):
        from llm.craig_agent import _build_tools_for_org
        with db_session() as db:
            db.add(Product(
                organization_slug=DEFAULT_ORG_SLUG,
                key="dyntool_test_lf_one",
                name="Dyntool Test LF One",
                category="large_format",
                pricing_strategy="per_unit",
                unit_price=10.0,
                min_qty=1,
            ))
            db.commit()
        with db_session() as db:
            tools = _build_tools_for_org(db, DEFAULT_ORG_SLUG)
        enum = _enum_for(tools, "quote_large_format") or []
        assert "dyntool_test_lf_one" in enum

    def test_enum_is_sorted_for_stable_diffs(self, fresh_dynamic_state):
        """Sorting the enum keeps the JSON Schema deterministic so the
        LLM's tool spec doesn't churn between identical-state turns."""
        from llm.craig_agent import _build_tools_for_org
        with db_session() as db:
            tools = _build_tools_for_org(db, DEFAULT_ORG_SLUG)
        enum = _enum_for(tools, "quote_small_format") or []
        assert enum == sorted(enum)


# ---------------------------------------------------------------------------
# 2. Empty category — enum dropped entirely
# ---------------------------------------------------------------------------


class TestEmptyCategoryDropsEnum:
    def test_unknown_tenant_drops_both_enums(self, fresh_dynamic_state):
        """A tenant with zero small_format AND zero large_format products
        sees no enum at all on either tool — they become free strings,
        and the prompt's catalog context is the only governing list."""
        from llm.craig_agent import _build_tools_for_org
        with db_session() as db:
            tools = _build_tools_for_org(db, "no_such_tenant_v404")
        small_enum = _enum_for(tools, "quote_small_format")
        large_enum = _enum_for(tools, "quote_large_format")
        assert small_enum is None, f"expected enum dropped, got {small_enum}"
        assert large_enum is None, f"expected enum dropped, got {large_enum}"


# ---------------------------------------------------------------------------
# 3. Multi-tenant isolation
# ---------------------------------------------------------------------------


class TestMultiTenantIsolation:
    def test_other_tenants_product_does_not_leak(self, fresh_dynamic_state):
        from llm.craig_agent import _build_tools_for_org
        OTHER = "v404_other_tenant"
        with db_session() as db:
            db.add(Product(
                organization_slug=OTHER,
                key="dyntool_test_other_tenant_sf",
                name="Other Tenant SF",
                category="small_format",
                pricing_strategy="tiered",
                min_qty=1,
            ))
            db.commit()
        with db_session() as db:
            tools_jp = _build_tools_for_org(db, DEFAULT_ORG_SLUG)
            tools_other = _build_tools_for_org(db, OTHER)
        enum_jp = _enum_for(tools_jp, "quote_small_format") or []
        enum_other = _enum_for(tools_other, "quote_small_format") or []
        assert "dyntool_test_other_tenant_sf" not in enum_jp
        assert "dyntool_test_other_tenant_sf" in enum_other


# ---------------------------------------------------------------------------
# 4. quote_booklet untouched
# ---------------------------------------------------------------------------


class TestBookletToolUnchanged:
    def test_booklet_tool_format_enum_remains_hardcoded(self, fresh_dynamic_state):
        """quote_booklet's enums are fundamental product structure
        (a5/a4, saddle_stitch/perfect_bound) — they must NOT shift
        based on what's in the catalog. The builder leaves them alone."""
        from llm.craig_agent import _build_tools_for_org
        with db_session() as db:
            tools = _build_tools_for_org(db, DEFAULT_ORG_SLUG)
        booklet = _get_tool(tools, "quote_booklet")
        assert booklet is not None
        props = booklet["function"]["parameters"]["properties"]
        assert props["format"]["enum"] == ["a5", "a4"]
        assert props["binding"]["enum"] == ["saddle_stitch", "perfect_bound"]
        assert props["cover_type"]["enum"] == [
            "self_cover", "card_cover", "card_cover_lam",
        ]


# ---------------------------------------------------------------------------
# 5. Module-level TOOLS constant not mutated
# ---------------------------------------------------------------------------


class TestModuleConstantImmutable:
    def test_tools_constant_unchanged_after_builder(self, fresh_dynamic_state):
        """The builder MUST deep-copy and not mutate the module-level
        TOOLS array — concurrent chat turns share the module-level
        list, and a mutation would race across requests."""
        from llm.craig_agent import _build_tools_for_org, TOOLS
        # Snapshot the canonical enum first
        canonical_small = next(
            t for t in TOOLS if t["function"]["name"] == "quote_small_format"
        )
        before = list(canonical_small["function"]["parameters"]["properties"]["product_key"]["enum"])

        # Build twice with different tenants — neither should affect TOOLS
        with db_session() as db:
            _build_tools_for_org(db, DEFAULT_ORG_SLUG)
            _build_tools_for_org(db, "v404_other_tenant")

        after = list(canonical_small["function"]["parameters"]["properties"]["product_key"]["enum"])
        assert before == after, "TOOLS module constant was mutated by the builder"
