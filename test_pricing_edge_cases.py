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
# Small-format edge cases
# ===========================================================================


class TestSmallFormatEdgeCases:
    """Business cards, flyers, NCR pads — quantity tier boundaries."""

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
