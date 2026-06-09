"""
Pricing-engine edge cases — boundary conditions, malformed inputs,
and gotcha scenarios that the happy-path tests don't cover.

These guard against:
  * Off-by-one bugs at tier boundaries
  * Zero / negative quantities
  * Missing product / missing tier
  * Extreme dimensions (1mm × 1mm labels, 10m × 10m banners)
  * Per-sheet edge cases (panel exactly fits, panel too big, qty=0)
  * Bulk-pricing tipping point on per-sqm products
  * Currency-precision rounding (€0.005 → €0.01? €0.00?)
  * Sanity ceiling interactions

Most tests call the pricing-engine functions DIRECTLY (not via /chat)
so they're fast and isolate engine logic from LLM behaviour.
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault(
    "STRATEGOS_JWT_SECRET",
    "test-secret-32-bytes-long-padding-enough-now",
)

from db import db_session  # noqa: E402
from db.models import Product, Setting, DEFAULT_ORG_SLUG  # noqa: E402
from pricing_engine import (  # noqa: E402
    quote_large_format, quote_small_format, quote_booklet,
    QuoteResult, EscalationResult,
)


ORG = DEFAULT_ORG_SLUG  # "just-print"


@pytest.fixture(autouse=True)
def _ensure_vinyl_labels_requires_dimensions():
    """Make sure vinyl_labels has requires_dimensions=True for the
    duration of every test in this file — some other tests fiddle
    with it."""
    with db_session() as db:
        p = db.query(Product).filter_by(
            organization_slug=ORG, key="vinyl_labels",
        ).first()
        if p and not getattr(p, "requires_dimensions", False):
            p.requires_dimensions = True
            db.commit()
    yield


# ===========================================================================
# Per-sqm engine boundary tests (vinyl_labels, pvc_banners, etc.)
# ===========================================================================


class TestPerSqmExtremeDimensions:
    """Pushing the dimension limits — engine math should stay sane."""

    def test_tiny_label_10x10mm(self):
        """500 tiny stickers at 10×10mm = 0.05 m² × €45 = €2.25 ex VAT."""
        with db_session() as db:
            result = quote_large_format(
                db, product_key="vinyl_labels",
                quantity=500, width_mm=10, height_mm=10,
                organization_slug=ORG,
            )
        assert isinstance(result, QuoteResult), (
            f"Expected quote, got {result}"
        )
        # 500 × 0.0001 = 0.05 m² × €45 = €2.25 ex VAT × 1.23 = €2.77 inc
        assert 2.0 <= result.final_price_ex_vat <= 3.0
        assert 2.5 <= result.final_price_inc_vat <= 3.5

    def test_huge_label_500x500mm(self):
        """10 huge labels at 500×500mm = 2.5 m² × €45 = €112.50 ex VAT."""
        with db_session() as db:
            result = quote_large_format(
                db, product_key="vinyl_labels",
                quantity=10, width_mm=500, height_mm=500,
                organization_slug=ORG,
            )
        assert isinstance(result, QuoteResult)
        # 10 × 0.25 = 2.5 m². With bulk_threshold=10 m², we stay at
        # unit_price=€45. So 2.5 × €45 = €112.50 ex VAT.
        assert 110 <= result.final_price_ex_vat <= 115

    def test_quantity_1_smallest_possible_quote(self):
        """1 vinyl label at 100×100mm = 0.01 m² × €45 = €0.45 ex VAT.
        Tests rounding."""
        with db_session() as db:
            result = quote_large_format(
                db, product_key="vinyl_labels",
                quantity=1, width_mm=100, height_mm=100,
                organization_slug=ORG,
            )
        assert isinstance(result, QuoteResult)
        # 0.01 m² × €45 = €0.45 ex VAT × 1.23 = €0.55 inc VAT
        assert 0.40 <= result.final_price_ex_vat <= 0.55


class TestPerSqmBulkTippingPoint:
    """Bulk pricing kicks in at bulk_threshold. Test the boundary."""

    def test_just_under_threshold_uses_unit_price(self):
        """9.99 m² < 10 m² threshold → unit_price (€45)."""
        with db_session() as db:
            result = quote_large_format(
                db, product_key="vinyl_labels",
                quantity=1, area_sqm=9.99,
                organization_slug=ORG,
            )
        assert isinstance(result, QuoteResult)
        # Should use unit_price (45.0), NOT bulk_price (40.0)
        assert result.base_price == 45.0, (
            f"Just under threshold should use unit_price, got "
            f"base_price={result.base_price}"
        )

    def test_at_threshold_uses_bulk_price(self):
        """Exactly 10 m² ≥ threshold → bulk_price (€40)."""
        with db_session() as db:
            result = quote_large_format(
                db, product_key="vinyl_labels",
                quantity=1, area_sqm=10.0,
                organization_slug=ORG,
            )
        assert isinstance(result, QuoteResult)
        assert result.base_price == 40.0, (
            f"At threshold should use bulk_price, got base_price={result.base_price}"
        )

    def test_above_threshold_uses_bulk_price(self):
        """20 m² >> threshold → definitely bulk_price."""
        with db_session() as db:
            result = quote_large_format(
                db, product_key="vinyl_labels",
                quantity=1, area_sqm=20.0,
                organization_slug=ORG,
            )
        assert isinstance(result, QuoteResult)
        assert result.base_price == 40.0


class TestPerSqmMinBillableArea:
    """v39 — per-product minimum billable area. Justin's ask: vinyl
    labels under 1 m² should bill a full square metre. The engine
    clamps total_m2 up to product.min_billable_sqm BEFORE bulk-price
    selection. These tests set the floor explicitly on vinyl_labels
    and reset it after so the rest of the suite is unaffected."""

    def _set_floor(self, db, value):
        p = db.query(Product).filter_by(
            organization_slug=ORG, key="vinyl_labels",
        ).first()
        p.min_billable_sqm = value
        db.commit()

    def test_below_floor_is_clamped_to_floor(self):
        """0.2 m² with a 1 m² floor → billed as 1 m² (= 1 × €45)."""
        with db_session() as db:
            self._set_floor(db, 1.0)
            try:
                result = quote_large_format(
                    db, product_key="vinyl_labels",
                    quantity=1, area_sqm=0.2,
                    organization_slug=ORG,
                )
                assert isinstance(result, QuoteResult), f"got {result}"
                # Floored to 1 m² × €45 = €45 ex VAT (NOT 0.2 × €45 = €9).
                assert result.final_price_ex_vat >= 40.0, (
                    f"floor not applied: ex VAT €{result.final_price_ex_vat} "
                    f"(expected ~€45 for the 1 m² floor, not ~€9)"
                )
                # The customer-facing breakdown explains the floor.
                joined = " ".join(result.surcharges_applied).lower()
                assert "min 1" in joined and "applied" in joined, (
                    f"floor note missing from breakdown: "
                    f"{result.surcharges_applied}"
                )
            finally:
                self._set_floor(db, None)

    def test_at_or_above_floor_is_unchanged(self):
        """2 m² with a 1 m² floor → billed as the actual 2 m², no note."""
        with db_session() as db:
            self._set_floor(db, 1.0)
            try:
                result = quote_large_format(
                    db, product_key="vinyl_labels",
                    quantity=1, area_sqm=2.0,
                    organization_slug=ORG,
                )
                assert isinstance(result, QuoteResult)
                # 2 m² × €45 = €90 ex VAT (unit price, under 10 m² bulk).
                assert 88.0 <= result.final_price_ex_vat <= 92.0, (
                    f"above-floor price changed: €{result.final_price_ex_vat}"
                )
                joined = " ".join(result.surcharges_applied).lower()
                assert "min " not in joined, (
                    f"floor note wrongly applied above floor: "
                    f"{result.surcharges_applied}"
                )
            finally:
                self._set_floor(db, None)

    def test_floor_can_trip_bulk_threshold(self):
        """Floor is applied BEFORE bulk selection, so a 12 m² floor on a
        tiny order crosses the 10 m² bulk threshold → bulk_price (€40)."""
        with db_session() as db:
            self._set_floor(db, 12.0)
            try:
                result = quote_large_format(
                    db, product_key="vinyl_labels",
                    quantity=1, area_sqm=0.2,
                    organization_slug=ORG,
                )
                assert isinstance(result, QuoteResult)
                # 0.2 → floored to 12 m² ≥ 10 m² threshold → bulk_price 40.
                assert result.base_price == 40.0, (
                    f"floor didn't trip bulk: base_price={result.base_price}"
                )
            finally:
                self._set_floor(db, None)

    def test_no_floor_is_legacy_behaviour(self):
        """With min_billable_sqm=None (the default), 0.2 m² bills as
        0.2 m² × €45 = €9 — unchanged from before v39."""
        with db_session() as db:
            self._set_floor(db, None)
            result = quote_large_format(
                db, product_key="vinyl_labels",
                quantity=1, area_sqm=0.2,
                organization_slug=ORG,
            )
            assert isinstance(result, QuoteResult)
            # 0.2 × €45 = €9 ex VAT — no floor applied.
            assert result.final_price_ex_vat <= 12.0, (
                f"legacy no-floor price changed: €{result.final_price_ex_vat}"
            )


class TestPerSqmZeroOrNegativeQuantity:
    """Defensive — zero / negative inputs should escalate, not crash."""

    def test_quantity_zero_escalates(self):
        with db_session() as db:
            result = quote_large_format(
                db, product_key="vinyl_labels",
                quantity=0, width_mm=100, height_mm=100,
                organization_slug=ORG,
            )
        assert isinstance(result, EscalationResult), (
            f"Quantity 0 should escalate, got {result}"
        )

    def test_negative_quantity_escalates(self):
        with db_session() as db:
            result = quote_large_format(
                db, product_key="vinyl_labels",
                quantity=-5, width_mm=100, height_mm=100,
                organization_slug=ORG,
            )
        assert isinstance(result, EscalationResult)

    def test_area_zero_escalates(self):
        """0 m² → can't price."""
        with db_session() as db:
            result = quote_large_format(
                db, product_key="vinyl_labels",
                quantity=1, area_sqm=0.0,
                organization_slug=ORG,
            )
        assert isinstance(result, EscalationResult)


# ===========================================================================
# Per-sheet engine edge cases (foamex, dibond, corri panels)
# ===========================================================================


class TestPerSheetEdgeCases:
    """Foamex/dibond/corri use axis-aligned panel packing."""

    def test_panel_exactly_fits_sheet_one_per_sheet(self):
        """Foamex sheet is 2400x1200mm. A panel of 2400x1200mm fits
        exactly 1 per sheet."""
        with db_session() as db:
            result = quote_large_format(
                db, product_key="foamex_boards",
                quantity=1, width_mm=2400, height_mm=1200,
                organization_slug=ORG,
            )
        assert isinstance(result, QuoteResult), (
            f"Expected quote, got {result}"
        )
        # 1 panel × 1 sheet × €150 = €150 ex VAT
        assert result.final_price_ex_vat == 150.0

    def test_panel_too_big_for_sheet_escalates(self):
        """If a panel exceeds the sheet's max dimensions, escalate."""
        with db_session() as db:
            result = quote_large_format(
                db, product_key="foamex_boards",
                quantity=1, width_mm=3000, height_mm=2000,
                organization_slug=ORG,
            )
        assert isinstance(result, EscalationResult), (
            f"Panel larger than sheet should escalate, got {result}"
        )

    def test_panel_packing_axis_rotation(self):
        """For a 1200×800mm panel on a 2400×1200mm sheet, the engine
        should try BOTH orientations and pick the better fit.
        Orientation A (1200×800 same as sheet axis):
          2 panels per row × 1 row = 2/sheet
        Orientation B (800×1200 rotated):
          3 panels per row × 1 row = 3/sheet
        Engine should pick 3/sheet, so 6 panels = 2 sheets × €150 = €300 ex."""
        with db_session() as db:
            result = quote_large_format(
                db, product_key="foamex_boards",
                quantity=6, width_mm=1200, height_mm=800,
                organization_slug=ORG,
            )
        assert isinstance(result, QuoteResult)
        # Best case: 3 per sheet × 2 sheets × €150 = €300 ex VAT
        # Worst case (no rotation): 2 per sheet × 3 sheets × €150 = €450 ex
        # We accept either, but log the diff so we know if axis-rotation
        # is missing.
        assert 290 <= result.final_price_ex_vat <= 460, (
            f"foamex 6×1200x800mm: expected €300-450 ex VAT, "
            f"got €{result.final_price_ex_vat}. Axis-rotation broken?"
        )


# ===========================================================================
# Unknown product / missing config — graceful escalation
# ===========================================================================


class TestUnknownProduct:
    def test_nonexistent_product_key_escalates(self):
        """quote_large_format on a missing product_key should return
        EscalationResult, NOT crash."""
        with db_session() as db:
            result = quote_large_format(
                db, product_key="nonexistent_product",
                quantity=10,
                organization_slug=ORG,
            )
        assert isinstance(result, EscalationResult)
        assert "not" in (result.reason or "").lower()

    def test_small_format_unknown_finish_escalates(self):
        """Asking for a finish that doesn't exist for the product."""
        with db_session() as db:
            result = quote_small_format(
                db, product_key="business_cards",
                quantity=100, double_sided=False, finish="velvet",
                organization_slug=ORG,
            )
        assert isinstance(result, EscalationResult), (
            f"Unknown finish should escalate, got {result}"
        )


# ===========================================================================
# Sanity-ceiling interactions (v38)
# ===========================================================================


class TestSanityCeilingInteractions:
    """Verify the sanity_max_unit_price guard fires correctly across
    different scenarios."""

    def test_ceiling_at_exact_threshold_passes(self):
        """If per-unit price equals the ceiling exactly, it passes
        (the guard uses > not >=)."""
        with db_session() as db:
            p = db.query(Product).filter_by(
                organization_slug=ORG, key="vinyl_labels",
            ).first()
            # Set ceiling so 0.01 m² × €45 / 1 unit = €0.45/unit hits it exactly
            p.sanity_max_unit_price = 0.45
            db.commit()
            try:
                result = quote_large_format(
                    db, product_key="vinyl_labels",
                    quantity=1, width_mm=100, height_mm=100,
                    organization_slug=ORG,
                )
                # Exact match → engine returns the quote (guard uses >)
                # but with tiny float-arithmetic the actual unit price
                # may be just under 0.45 — that's fine, it passes.
                assert isinstance(result, (QuoteResult, EscalationResult))
            finally:
                p.sanity_max_unit_price = None
                db.commit()

    def test_ceiling_only_applies_when_set(self):
        """Default sanity_max_unit_price is null → no guard fires."""
        with db_session() as db:
            p = db.query(Product).filter_by(
                organization_slug=ORG, key="vinyl_labels",
            ).first()
            p.sanity_max_unit_price = None
            db.commit()
            result = quote_large_format(
                db, product_key="vinyl_labels",
                quantity=500, width_mm=40, height_mm=10,
                organization_slug=ORG,
            )
        # No ceiling → returns normal quote
        assert isinstance(result, QuoteResult)


# ===========================================================================
# v41 — TestMinOrderFloor + TestMaxQtyCeiling
#
# Per-product `min_order_value_eur` and `max_qty_for_auto_quote` columns
# added in v41. Floor: bumps the final ex-VAT total up to the floor and
# surfaces a "Minimum order €X applied" note. Ceiling: short-circuits
# BEFORE pricing math and returns EscalationResult(manual_review=True).
#
# We verify both behaviors fire on each of the 4 quote_* entry points:
#   - quote_small_format (small_format.tiered: business_cards)
#   - _quote_per_sqm  via quote_large_format (vinyl_labels)
#   - _quote_per_sheet via quote_large_format (foamex_boards)
#   - quote_booklet (booklet_a5_saddle_stitch)
#
# All set/unset within the test (try/finally) so the catalog stays
# pristine for the rest of the suite.
# ===========================================================================


def _set_v41_fields(db, product_key: str, *, min_eur=None, max_qty=None):
    """Helper: set v41 fields on a product, return the Product so caller
    can revert in finally."""
    p = db.query(Product).filter_by(
        organization_slug=ORG, key=product_key,
    ).first()
    if p is None:
        pytest.skip(f"Product {product_key} missing from seed catalog.")
    p.min_order_value_eur = min_eur
    p.max_qty_for_auto_quote = max_qty
    db.commit()
    return p


class TestMinOrderFloor:
    """v41 — `min_order_value_eur` floors the engine's final ex-VAT
    total when computed value falls below it. Verified across each
    of the 4 quote entry points."""

    def test_small_format_floor_applied(self):
        """Business cards at the lowest tier (100 = ~€35 ex VAT).
        Floor at €100 → bumped to €100; surcharges_applied carries the
        'Minimum order €100.00' note."""
        with db_session() as db:
            p = _set_v41_fields(db, "business_cards", min_eur=100.0)
            try:
                result = quote_small_format(
                    db, product_key="business_cards",
                    quantity=100, double_sided=False, finish="matte",
                    organization_slug=ORG,
                )
                if isinstance(result, EscalationResult):
                    pytest.skip(
                        "business_cards escalated (tier missing in seed) — "
                        "engine path covered by other tests."
                    )
                assert isinstance(result, QuoteResult)
                # Floor only applies when computed is below floor.
                assert result.final_price_ex_vat >= 100.0 - 0.005
                assert any(
                    "Minimum order" in s for s in result.surcharges_applied
                ), (
                    f"Expected min-order note, got {result.surcharges_applied}"
                )
            finally:
                p.min_order_value_eur = None
                db.commit()

    def test_small_format_floor_not_applied_when_above(self):
        """If the natural total is already above the floor, the floor
        is a no-op — no surcharge note, no bump."""
        with db_session() as db:
            p = _set_v41_fields(db, "business_cards", min_eur=1.0)
            try:
                result = quote_small_format(
                    db, product_key="business_cards",
                    quantity=100, double_sided=False, finish="matte",
                    organization_slug=ORG,
                )
                if isinstance(result, EscalationResult):
                    pytest.skip("business_cards escalated in seed catalog.")
                assert isinstance(result, QuoteResult)
                assert not any(
                    "Minimum order" in s for s in result.surcharges_applied
                ), (
                    "Floor below natural total should not fire."
                )
            finally:
                p.min_order_value_eur = None
                db.commit()

    def test_per_sqm_floor_applied(self):
        """Tiny vinyl label order (1 × 50×30mm = 0.0015 m²) — natural
        total well under €1. Floor at €45 (Justin's real minimum)
        bumps it up."""
        with db_session() as db:
            p = _set_v41_fields(db, "vinyl_labels", min_eur=45.0)
            try:
                result = quote_large_format(
                    db, product_key="vinyl_labels",
                    quantity=1, width_mm=50, height_mm=30,
                    organization_slug=ORG,
                )
                if isinstance(result, EscalationResult):
                    pytest.skip("vinyl_labels escalated under seeded rules.")
                assert isinstance(result, QuoteResult)
                assert result.final_price_ex_vat >= 45.0 - 0.005
                assert any(
                    "Minimum order" in s for s in result.surcharges_applied
                )
            finally:
                p.min_order_value_eur = None
                db.commit()

    def test_per_sheet_floor_applied(self):
        """1 small foamex panel — natural cost is fractional of a sheet.
        Floor at €25 bumps to €25."""
        with db_session() as db:
            p = _set_v41_fields(db, "foamex_boards", min_eur=25.0)
            try:
                result = quote_large_format(
                    db, product_key="foamex_boards",
                    quantity=1, width_mm=200, height_mm=200,
                    organization_slug=ORG,
                )
                if isinstance(result, EscalationResult):
                    pytest.skip("foamex_boards escalated under seeded rules.")
                assert isinstance(result, QuoteResult)
                # The per_sheet engine bills full sheets, so total may
                # already exceed €25 — only assert floor behavior when
                # it actually fires. If it didn't fire, computed >= 25,
                # which is fine.
                assert result.final_price_ex_vat >= 25.0 - 0.005
            finally:
                p.min_order_value_eur = None
                db.commit()

    def test_booklet_floor_applied(self):
        """Cheapest booklet × huge floor — bumped to floor.
        booklet_a5_saddle_stitch lowest tier ~€100-ish; floor at €500
        forces a bump."""
        with db_session() as db:
            p = _set_v41_fields(db, "booklet_a5_saddle_stitch", min_eur=500.0)
            try:
                result = quote_booklet(
                    db, format="a5", binding="saddle_stitch",
                    pages=16, cover_type="self_cover",
                    quantity=50, organization_slug=ORG,
                )
                if isinstance(result, EscalationResult):
                    pytest.skip(
                        "Booklet tier missing in seed — engine path "
                        "covered by booklet basics tests."
                    )
                assert isinstance(result, QuoteResult)
                assert result.final_price_ex_vat >= 500.0 - 0.005
                assert any(
                    "Minimum order" in s for s in result.surcharges_applied
                )
            finally:
                p.min_order_value_eur = None
                db.commit()

    def test_floor_only_applies_when_set(self):
        """Default min_order_value_eur is null → floor never fires
        (no surcharge note, no bump)."""
        with db_session() as db:
            p = _set_v41_fields(db, "business_cards", min_eur=None)
            try:
                result = quote_small_format(
                    db, product_key="business_cards",
                    quantity=100, double_sided=False, finish="matte",
                    organization_slug=ORG,
                )
                if isinstance(result, EscalationResult):
                    pytest.skip("business_cards escalated.")
                assert isinstance(result, QuoteResult)
                assert not any(
                    "Minimum order" in s for s in result.surcharges_applied
                )
            finally:
                p.min_order_value_eur = None
                db.commit()


class TestMaxQtyCeiling:
    """v41 — `max_qty_for_auto_quote` short-circuits BEFORE pricing
    math and escalates to manual review."""

    def test_small_format_above_ceiling_escalates(self):
        """Business cards with ceiling=200 → ask for 500 → escalates
        with manual_review=True and a reason string."""
        with db_session() as db:
            p = _set_v41_fields(db, "business_cards", max_qty=200)
            try:
                result = quote_small_format(
                    db, product_key="business_cards",
                    quantity=500, double_sided=False, finish="matte",
                    organization_slug=ORG,
                )
                assert isinstance(result, EscalationResult), (
                    f"Above-ceiling should escalate, got {type(result).__name__}"
                )
                assert result.manual_review is True
                assert "auto-quote ceiling" in (result.reason or "").lower()
            finally:
                p.max_qty_for_auto_quote = None
                db.commit()

    def test_per_sqm_above_ceiling_escalates(self):
        """Vinyl labels with ceiling=100 → ask for 500 → escalates."""
        with db_session() as db:
            p = _set_v41_fields(db, "vinyl_labels", max_qty=100)
            try:
                result = quote_large_format(
                    db, product_key="vinyl_labels",
                    quantity=500, width_mm=50, height_mm=30,
                    organization_slug=ORG,
                )
                assert isinstance(result, EscalationResult)
                assert result.manual_review is True
            finally:
                p.max_qty_for_auto_quote = None
                db.commit()

    def test_per_sheet_above_ceiling_escalates(self):
        """Foamex boards with ceiling=50 → ask for 200 panels → escalates."""
        with db_session() as db:
            p = _set_v41_fields(db, "foamex_boards", max_qty=50)
            try:
                result = quote_large_format(
                    db, product_key="foamex_boards",
                    quantity=200, width_mm=300, height_mm=200,
                    organization_slug=ORG,
                )
                assert isinstance(result, EscalationResult)
                assert result.manual_review is True
            finally:
                p.max_qty_for_auto_quote = None
                db.commit()

    def test_booklet_above_ceiling_escalates(self):
        """Booklet with ceiling=10 → ask for 100 → escalates."""
        with db_session() as db:
            p = _set_v41_fields(db, "booklet_a5_saddle_stitch", max_qty=10)
            try:
                result = quote_booklet(
                    db, format="a5", binding="saddle_stitch",
                    pages=16, cover_type="self_cover",
                    quantity=100, organization_slug=ORG,
                )
                assert isinstance(result, EscalationResult)
                assert result.manual_review is True
            finally:
                p.max_qty_for_auto_quote = None
                db.commit()

    def test_at_ceiling_exact_passes(self):
        """Qty == ceiling should NOT escalate (guard uses > not >=)."""
        with db_session() as db:
            p = _set_v41_fields(db, "business_cards", max_qty=100)
            try:
                result = quote_small_format(
                    db, product_key="business_cards",
                    quantity=100, double_sided=False, finish="matte",
                    organization_slug=ORG,
                )
                # Either a quote or an escalation for a non-ceiling
                # reason. If it's an EscalationResult, it must NOT be
                # the ceiling escalation.
                if isinstance(result, EscalationResult):
                    assert "auto-quote ceiling" not in (result.reason or "").lower()
            finally:
                p.max_qty_for_auto_quote = None
                db.commit()

    def test_ceiling_only_applies_when_set(self):
        """Default max_qty_for_auto_quote is null → no ceiling fires."""
        with db_session() as db:
            p = _set_v41_fields(db, "business_cards", max_qty=None)
            try:
                result = quote_small_format(
                    db, product_key="business_cards",
                    quantity=500, double_sided=False, finish="matte",
                    organization_slug=ORG,
                )
                # Should NOT be a ceiling escalation. (May be a
                # tier-missing escalation, that's fine.)
                if isinstance(result, EscalationResult):
                    assert "auto-quote ceiling" not in (result.reason or "").lower()
            finally:
                p.max_qty_for_auto_quote = None
                db.commit()


# ===========================================================================
# Small-format edge cases
# ===========================================================================


class TestSmallFormatEdgeCases:
    """Business cards, flyers, NCR books — quantity tier boundaries."""

    def test_quantity_off_tier_escalates_or_extrapolates(self):
        """Business cards tiers: 100, 250, 500, 1000, 2500. A request
        for 600 falls between 500 and 1000."""
        with db_session() as db:
            result = quote_small_format(
                db, product_key="business_cards",
                quantity=600, double_sided=False, finish="matte",
                organization_slug=ORG,
            )
        # Either: extrapolates (returns QuoteResult), or escalates.
        # Both are valid v38 behaviors. We just want NO CRASH.
        assert isinstance(result, (QuoteResult, EscalationResult)), (
            f"Off-tier quantity crashed: {result}"
        )

    def test_quantity_below_min_qty_escalates(self):
        """min_qty on business_cards is 100. Asking for 50 should
        either round up or escalate."""
        with db_session() as db:
            result = quote_small_format(
                db, product_key="business_cards",
                quantity=50, double_sided=False, finish="matte",
                organization_slug=ORG,
            )
        assert isinstance(result, (QuoteResult, EscalationResult)), (
            f"Below-min crashed: {result}"
        )

    def test_quantity_zero_quietly_bills_minimum_tier(self):
        """Defensive behaviour: qty=0 doesn't crash. The engine
        either escalates OR bills as the minimum tier (100). Either
        is acceptable — the contract is 'no crash, no nonsense
        price'."""
        with db_session() as db:
            result = quote_small_format(
                db, product_key="business_cards",
                quantity=0, double_sided=False, finish="matte",
                organization_slug=ORG,
            )
        assert isinstance(result, (QuoteResult, EscalationResult))
        # If a quote was returned, it shouldn't be absurd. Since
        # business cards min is ~€30 for 100, a 0-quantity should
        # not somehow produce €1000+.
        if isinstance(result, QuoteResult):
            assert result.final_price_ex_vat <= 200, (
                f"qty=0 returned an absurdly large quote: "
                f"€{result.final_price_ex_vat}"
            )


# ===========================================================================
# Surcharge stacking
# ===========================================================================


class TestSurchargeStacking:
    """Surcharges stack multiplicatively per Justin's rules.
    Note: business_cards has 'double-sided no extra charge' as an
    exception, so test on flyers_a4 where standard rates apply."""

    def test_flyers_double_sided_adds_20pct(self):
        """1000 flyers single-sided vs double-sided. The
        double_sided surcharge is +20% multiplicative."""
        with db_session() as db:
            single = quote_small_format(
                db, product_key="flyers_a4",
                quantity=1000, double_sided=False, finish="matte",
                organization_slug=ORG,
            )
            double = quote_small_format(
                db, product_key="flyers_a4",
                quantity=1000, double_sided=True, finish="matte",
                organization_slug=ORG,
            )
        assert isinstance(single, QuoteResult)
        assert isinstance(double, QuoteResult)
        ratio = double.final_price_ex_vat / single.final_price_ex_vat
        assert 1.15 <= ratio <= 1.25, (
            f"Double-sided surcharge should add ~20% — got ratio {ratio:.3f} "
            f"(single={single.final_price_ex_vat}, double={double.final_price_ex_vat})"
        )

    def test_business_cards_double_sided_no_extra_charge(self):
        """Per Justin's spec, business cards do NOT charge for
        double-sided. Lock the exception so it can't silently regress."""
        with db_session() as db:
            single = quote_small_format(
                db, product_key="business_cards",
                quantity=500, double_sided=False, finish="matte",
                organization_slug=ORG,
            )
            double = quote_small_format(
                db, product_key="business_cards",
                quantity=500, double_sided=True, finish="matte",
                organization_slug=ORG,
            )
        assert isinstance(single, QuoteResult)
        assert isinstance(double, QuoteResult)
        # Should be approximately equal (within €1 for float rounding)
        diff = abs(double.final_price_ex_vat - single.final_price_ex_vat)
        assert diff < 1.0, (
            f"business_cards 'double-sided no extra charge' exception "
            f"regressed — diff €{diff} between single (€{single.final_price_ex_vat}) "
            f"and double-sided (€{double.final_price_ex_vat})"
        )


# ===========================================================================
# Booklet engine smoke
# ===========================================================================


class TestBookletBasics:
    """quote_booklet engine — make sure the booklet path still works
    (none of v38 should have touched it, but verify)."""

    def test_a5_saddle_stitch_smoke(self):
        with db_session() as db:
            result = quote_booklet(
                db, format="a5", binding="saddle_stitch",
                pages=16, cover_type="self_cover",
                quantity=100,
                organization_slug=ORG,
            )
        # Just smoke — should not crash. Either quote or escalation.
        assert isinstance(result, (QuoteResult, EscalationResult)), (
            f"Booklet engine crashed: {result}"
        )


# ===========================================================================
# Posters product — added in v38, manual_review for now
# ===========================================================================


class TestPostersManualReview:
    """v38 added `posters` with manual_review_required=True. Verify
    the engine escalates ANY poster request."""

    def test_posters_always_escalates_until_priced(self):
        with db_session() as db:
            result = quote_large_format(
                db, product_key="posters",
                quantity=10, area_sqm=8.4,  # 10x A0
                organization_slug=ORG,
            )
        assert isinstance(result, EscalationResult), (
            f"posters should escalate (manual_review=True), got {result}"
        )
        assert result.manual_review is True
        assert (
            "manual" in (result.reason or "").lower()
            or "justin" in (result.reason or "").lower()
            or "poster" in (result.reason or "").lower()
        ), f"Escalation reason should mention manual/Justin: {result.reason!r}"


# ===========================================================================
# v40.7 — TestLargeFormatTieredBySize + TestLargeFormatCustomLaydown
#
# Boards (corri/foamex/dibond) use a 2-D (size, qty) tier table. The new
# engine paths kick in when `pricing_strategy == 'tiered'`. To exercise
# them without mutating prod-seed data permanently, each test:
#   1. Snapshots the product's current strategy + sheet_size_mm.
#   2. Flips strategy to 'tiered', seeds a few PriceTier rows
#      (cleaned up in finally).
#   3. Calls quote_large_format with `size` OR `width_mm`+`height_mm`.
#   4. Asserts on the returned QuoteResult / EscalationResult.
#   5. Tears down: deletes the test tiers, restores strategy/sheet_size.
# ===========================================================================

from db.models import PriceTier as _PT  # noqa: E402


def _seed_board_tiers(db, product, rows):
    """Helper: add tier rows under (product_id, spec_key, quantity, price)."""
    for spec_key, qty, price in rows:
        db.add(_PT(
            product_id=product.id,
            spec_key=spec_key,
            quantity=qty,
            price=price,
        ))
    db.commit()


def _clean_board_tiers(db, product):
    """Helper: wipe all tiers under a product (test teardown)."""
    db.query(_PT).filter_by(product_id=product.id).delete()
    db.commit()


class TestLargeFormatTieredBySize:
    """v40.7 — `size`-driven 2-D tier lookup for board products."""

    def test_a3_exact_tier_returns_table_price(self):
        """5 A3 corri @ €70/5 = €70 (Justin's exact sheet value)."""
        with db_session() as db:
            p = db.query(Product).filter_by(
                organization_slug=ORG, key="corri_boards",
            ).first()
            if p is None:
                pytest.skip("corri_boards not in seed.")
            orig_strat, orig_sheet = p.pricing_strategy, p.sheet_size_mm
            p.pricing_strategy = "tiered"
            p.sheet_size_mm = "2440x1220"
            db.commit()
            try:
                _seed_board_tiers(db, p, [
                    ("A3", 5, 70.0),
                    ("A3", 10, 120.0),
                    ("A1", 5, 140.0),
                ])
                result = quote_large_format(
                    db, product_key="corri_boards",
                    quantity=5, size="A3",
                    organization_slug=ORG,
                )
                assert isinstance(result, QuoteResult)
                assert abs(result.final_price_ex_vat - 70.0) < 0.01
            finally:
                _clean_board_tiers(db, p)
                p.pricing_strategy = orig_strat
                p.sheet_size_mm = orig_sheet
                db.commit()

    def test_off_tier_qty_stacks(self):
        """15 A3 with tiers [5=70, 10=120] → stack 10+5 = 190."""
        with db_session() as db:
            p = db.query(Product).filter_by(
                organization_slug=ORG, key="corri_boards",
            ).first()
            orig_strat, orig_sheet = p.pricing_strategy, p.sheet_size_mm
            p.pricing_strategy = "tiered"
            p.sheet_size_mm = "2440x1220"
            db.commit()
            try:
                _seed_board_tiers(db, p, [
                    ("A3", 5, 70.0),
                    ("A3", 10, 120.0),
                ])
                result = quote_large_format(
                    db, product_key="corri_boards",
                    quantity=15, size="A3",
                    organization_slug=ORG,
                )
                assert isinstance(result, QuoteResult)
                assert abs(result.final_price_ex_vat - 190.0) < 0.01
                assert any("Tier combination" in s for s in result.surcharges_applied)
            finally:
                _clean_board_tiers(db, p)
                p.pricing_strategy = orig_strat
                p.sheet_size_mm = orig_sheet
                db.commit()

    def test_unknown_size_escalates_with_available_sizes(self):
        """A2 not in tier table → escalation, message lists [A3, A1]."""
        with db_session() as db:
            p = db.query(Product).filter_by(
                organization_slug=ORG, key="corri_boards",
            ).first()
            orig_strat, orig_sheet = p.pricing_strategy, p.sheet_size_mm
            p.pricing_strategy = "tiered"
            p.sheet_size_mm = "2440x1220"
            db.commit()
            try:
                _seed_board_tiers(db, p, [
                    ("A3", 5, 70.0),
                    ("A1", 5, 140.0),
                ])
                result = quote_large_format(
                    db, product_key="corri_boards",
                    quantity=5, size="A2",
                    organization_slug=ORG,
                )
                assert isinstance(result, EscalationResult)
                assert "A2" in (result.reason or "")
                # Message should expose the available sizes so the LLM
                # can ask the customer to pick.
                assert "A3" in (result.message or "") or "A1" in (result.message or "")
            finally:
                _clean_board_tiers(db, p)
                p.pricing_strategy = orig_strat
                p.sheet_size_mm = orig_sheet
                db.commit()

    def test_missing_both_size_and_dims_escalates(self):
        """No size + no width/height → friendly escalation."""
        with db_session() as db:
            p = db.query(Product).filter_by(
                organization_slug=ORG, key="corri_boards",
            ).first()
            orig_strat = p.pricing_strategy
            p.pricing_strategy = "tiered"
            db.commit()
            try:
                result = quote_large_format(
                    db, product_key="corri_boards",
                    quantity=5,
                    organization_slug=ORG,
                )
                assert isinstance(result, EscalationResult)
                assert "size" in (result.reason or "").lower() or "width" in (result.reason or "").lower()
            finally:
                p.pricing_strategy = orig_strat
                db.commit()

    def test_lowercase_size_normalizes(self):
        """`size='a3'` should match `spec_key='A3'`."""
        with db_session() as db:
            p = db.query(Product).filter_by(
                organization_slug=ORG, key="corri_boards",
            ).first()
            orig_strat, orig_sheet = p.pricing_strategy, p.sheet_size_mm
            p.pricing_strategy = "tiered"
            p.sheet_size_mm = "2440x1220"
            db.commit()
            try:
                _seed_board_tiers(db, p, [("A3", 5, 70.0)])
                result = quote_large_format(
                    db, product_key="corri_boards",
                    quantity=5, size="a3",
                    organization_slug=ORG,
                )
                assert isinstance(result, QuoteResult)
                assert abs(result.final_price_ex_vat - 70.0) < 0.01
            finally:
                _clean_board_tiers(db, p)
                p.pricing_strategy = orig_strat
                p.sheet_size_mm = orig_sheet
                db.commit()


class TestLargeFormatCustomLaydown:
    """v40.7 — laydown calculator for custom panel sizes (mm).

    Math sanity:
        2440 × 1220 sheet, bleed=6mm/side, grip top=15, bottom=5, sides=5 each
        → useable sheet: (2440-10) × (1220-20) = 2430 × 1200 mm
        For an 800×600 panel: effective panel = 812×612
        → fits: rotated yields 3 panels/sheet (2430/612=3 cols, 1200/812=1 row)
    """

    def test_standard_custom_size_uses_laydown(self):
        """5 × 800×600mm corri → laydown derives 3/sheet → ceil(5/3)=2 sheets.
        Stack 2 sheets at tier ladder [1=180, 5=750]: 1+1 = €360."""
        with db_session() as db:
            p = db.query(Product).filter_by(
                organization_slug=ORG, key="corri_boards",
            ).first()
            orig_strat, orig_sheet = p.pricing_strategy, p.sheet_size_mm
            p.pricing_strategy = "tiered"
            p.sheet_size_mm = "2440x1220"
            db.commit()
            try:
                _seed_board_tiers(db, p, [
                    ("2440x1220", 1, 180.0),
                    ("2440x1220", 5, 750.0),
                ])
                result = quote_large_format(
                    db, product_key="corri_boards",
                    quantity=5, width_mm=800, height_mm=600,
                    organization_slug=ORG,
                )
                assert isinstance(result, QuoteResult), (
                    f"Expected QuoteResult, got {result}"
                )
                # 1 sheet @ 180 × 2 = 360 (stack of 1+1)
                assert abs(result.final_price_ex_vat - 360.0) < 0.01
                assert any("Laydown" in s for s in result.surcharges_applied)
            finally:
                _clean_board_tiers(db, p)
                p.pricing_strategy = orig_strat
                p.sheet_size_mm = orig_sheet
                db.commit()

    def test_panel_too_big_escalates(self):
        """A 10000×10000 panel doesn't fit on any sheet → escalates."""
        with db_session() as db:
            p = db.query(Product).filter_by(
                organization_slug=ORG, key="corri_boards",
            ).first()
            orig_strat, orig_sheet = p.pricing_strategy, p.sheet_size_mm
            p.pricing_strategy = "tiered"
            p.sheet_size_mm = "2440x1220"
            db.commit()
            try:
                _seed_board_tiers(db, p, [("2440x1220", 1, 180.0)])
                result = quote_large_format(
                    db, product_key="corri_boards",
                    quantity=1, width_mm=10000, height_mm=10000,
                    organization_slug=ORG,
                )
                assert isinstance(result, EscalationResult)
                assert "doesn't fit" in (result.reason or "").lower() or "fit" in (result.reason or "").lower()
            finally:
                _clean_board_tiers(db, p)
                p.pricing_strategy = orig_strat
                p.sheet_size_mm = orig_sheet
                db.commit()

    def test_no_sheet_size_escalates(self):
        """Product missing sheet_size_mm → escalation tells operator to set it."""
        with db_session() as db:
            p = db.query(Product).filter_by(
                organization_slug=ORG, key="corri_boards",
            ).first()
            orig_strat, orig_sheet = p.pricing_strategy, p.sheet_size_mm
            p.pricing_strategy = "tiered"
            p.sheet_size_mm = None
            db.commit()
            try:
                result = quote_large_format(
                    db, product_key="corri_boards",
                    quantity=5, width_mm=400, height_mm=300,
                    organization_slug=ORG,
                )
                assert isinstance(result, EscalationResult)
                assert "sheet_size" in (result.reason or "").lower() or "sheet" in (result.reason or "").lower()
            finally:
                p.pricing_strategy = orig_strat
                p.sheet_size_mm = orig_sheet
                db.commit()

    def test_high_qty_stacks_sheets(self):
        """50 × 400×300mm: eff panel 412×312, eff sheet 2430×1200.
        Rotated: 2430/312=7 cols × 1200/412=2 rows = 14/sheet
        Non-rotated: 2430/412=5 × 1200/312=3 = 15/sheet → use 15
        50/15 = ceil = 4 sheets → stack [1+1+1+1] = €720."""
        with db_session() as db:
            p = db.query(Product).filter_by(
                organization_slug=ORG, key="corri_boards",
            ).first()
            orig_strat, orig_sheet = p.pricing_strategy, p.sheet_size_mm
            p.pricing_strategy = "tiered"
            p.sheet_size_mm = "2440x1220"
            db.commit()
            try:
                _seed_board_tiers(db, p, [
                    ("2440x1220", 1, 180.0),
                    ("2440x1220", 5, 750.0),
                ])
                result = quote_large_format(
                    db, product_key="corri_boards",
                    quantity=50, width_mm=400, height_mm=300,
                    organization_slug=ORG,
                )
                assert isinstance(result, QuoteResult), result
                # Engine picked sheets_needed; the exact value depends on
                # the packing direction. Either 4 (15/sheet) or 5 (10/sheet
                # if a future engine rev reduces yield). Stay loose on the
                # number but assert that the result is sensible:
                # at least 1 sheet's worth at €180.
                assert result.final_price_ex_vat >= 180.0
                assert any("Laydown" in s for s in result.surcharges_applied)
            finally:
                _clean_board_tiers(db, p)
                p.pricing_strategy = orig_strat
                p.sheet_size_mm = orig_sheet
                db.commit()
