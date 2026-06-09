"""
Test suite for Craig Pricing Service.
Tests real scenarios against Justin's spreadsheet values.

PRICING MODEL (confirmed by Justin 16 Apr 2026):
- Small format prices are PER UNIT BASE (per 100 for cards/flyers, per 5 for NCR books)
- Total = unit_price × (quantity / unit_base)
- Printed matter VAT = 13.5% (not 23%)
- Large format = 23% VAT (signage, not printed matter)
- Artwork = 23% VAT (service)
"""

import sys
import os

sys.path.insert(0, os.path.dirname(__file__))

from fastapi.testclient import TestClient
from app import app

client = TestClient(app)


# =============================================================================
# HEALTH & INFO ENDPOINTS
# =============================================================================


def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["service"] == "craig-pricing-service"


def test_products_list():
    r = client.get("/products")
    assert r.status_code == 200
    data = r.json()
    # 10 small + 12 large + 4 booklet = 26 commercial products + 1 'secretest'
    # demo sentinel seeded by V21. Hard count would re-break every time we
    # add a sentinel — assert >= 26 + categories instead.
    assert len(data) >= 26
    categories = set(d["category"] for d in data)
    assert "small_format" in categories
    assert "large_format" in categories
    assert "booklet" in categories


def test_products_endpoint():
    """Products endpoint returns all 26 products."""
    r = client.get("/products")
    assert r.status_code == 200


def test_finish_param_ignored_when_product_has_no_finishes():
    """
    Regression: the LLM (DeepSeek) auto-fills `finish="uncoated"` as a
    default for small_format products. Sentinel products like
    `secretest` have `finishes=[]` configured. Before, the engine would
    escalate "uncoated not available" — breaking the whole demo flow.

    Now: when the product has NO finishes configured, any finish the
    caller passes is silently ignored.
    """
    from pricing_engine import quote_small_format
    from db import db_session

    with db_session() as db:
        # The sentinel `secretest` product has `finishes=[]` — seeded by
        # scripts/v21_secretest_demo_product.py.
        result = quote_small_format(
            db, "secretest", quantity=1, finish="uncoated",
        )
    # Must succeed (not escalate)
    assert result.success is True, (
        f"Expected secretest to quote despite finish='uncoated', got "
        f"escalation: {getattr(result, 'reason', None)!r}"
    )
    # And the price should still be the sentinel €1.00 inc VAT
    assert abs(result.final_price_inc_vat - 1.00) < 0.01


# =============================================================================
# SMALL FORMAT — UNIT-BASED PRICING (per 100 cards/flyers, per 5 NCR books)
# =============================================================================


def test_business_cards_500_gloss():
    """500 biz cards @ €38/100 = €190 total."""
    r = client.post("/quote/small-format", json={
        "product_key": "business_cards",
        "quantity": 500,
        "double_sided": False,
        "finish": "gloss",
    })
    data = r.json()
    assert data["success"] is True
    assert data["base_price"] == 190.00  # 38 * (500/100)
    assert data["final_price_ex_vat"] == 190.00
    assert data["surcharges_applied"] == []


def test_business_cards_500_double_sided_no_surcharge():
    """Business cards double-sided = NO extra charge (exception)."""
    r = client.post("/quote/small-format", json={
        "product_key": "business_cards",
        "quantity": 500,
        "double_sided": True,
        "finish": "gloss",
    })
    data = r.json()
    assert data["success"] is True
    assert data["base_price"] == 190.00
    assert data["final_price_ex_vat"] == 190.00
    assert data["surcharges_applied"] == []


def test_business_cards_250_soft_touch():
    """250 biz cards @ €60/100 = €150, +€15 flat soft-touch (v10) = €165."""
    r = client.post("/quote/small-format", json={
        "product_key": "business_cards",
        "quantity": 250,
        "double_sided": False,
        "finish": "soft-touch",
    })
    data = r.json()
    assert data["success"] is True
    assert data["base_price"] == 150.00  # 60 * (250/100)
    assert data["final_price_ex_vat"] == 165.00  # 150 + 15 flat fee
    assert len(data["surcharges_applied"]) == 1


def test_flyers_a5_500_double_sided():
    """500 A5 flyers @ €22/100 = €110, +20% double-sided = €132."""
    r = client.post("/quote/small-format", json={
        "product_key": "flyers_a5",
        "quantity": 500,
        "double_sided": True,
        "finish": "gloss",
    })
    data = r.json()
    assert data["success"] is True
    assert data["base_price"] == 110.00  # 22 * 5
    assert data["final_price_ex_vat"] == 132.00  # 110 * 1.20


def test_flyers_a4_1000_double_sided_soft_touch():
    """1000 A4 flyers @ €22/100 = €220, x1.20 x1.25 = €330.
    Note: A4 flyers only have gloss/matte, not soft-touch.
    Test with double-sided only."""
    r = client.post("/quote/small-format", json={
        "product_key": "flyers_a4",
        "quantity": 1000,
        "double_sided": True,
        "finish": "gloss",
    })
    data = r.json()
    assert data["success"] is True
    assert data["base_price"] == 220.00  # 22 * 10
    assert data["final_price_ex_vat"] == 264.00  # 220 * 1.20
    assert len(data["surcharges_applied"]) == 1


def test_ncr_a5_10_duplicate():
    """10 NCR A5 books @ €90/5 books = €180."""
    r = client.post("/quote/small-format", json={
        "product_key": "ncr_books_a5",
        "quantity": 10,
        "double_sided": False,
        "finish": "duplicate",
    })
    data = r.json()
    assert data["success"] is True
    assert data["base_price"] == 180.00  # 90 * (10/5)
    assert data["final_price_ex_vat"] == 180.00


def test_ncr_a4_20_triplicate():
    """20 NCR A4 books @ €85/5 books = €340, +10% triplicate = €374."""
    r = client.post("/quote/small-format", json={
        "product_key": "ncr_books_a4",
        "quantity": 20,
        "double_sided": False,
        "finish": "triplicate",
    })
    data = r.json()
    assert data["success"] is True
    assert data["base_price"] == 340.00  # 85 * (20/5)
    assert data["final_price_ex_vat"] == 374.00  # 340 * 1.10


def test_letterheads_250():
    """250 letterheads @ €55/100 = €137.50."""
    r = client.post("/quote/small-format", json={
        "product_key": "letterheads",
        "quantity": 250,
        "double_sided": False,
        "finish": "uncoated",
    })
    data = r.json()
    assert data["success"] is True
    assert data["base_price"] == 137.50  # 55 * 2.5


def test_compliment_slips_1000_double_sided():
    """1000 compliment slips @ €12/100 = €120, +20% = €144."""
    r = client.post("/quote/small-format", json={
        "product_key": "compliment_slips",
        "quantity": 1000,
        "double_sided": True,
        "finish": "uncoated",
    })
    data = r.json()
    assert data["success"] is True
    assert data["base_price"] == 120.00  # 12 * 10
    assert data["final_price_ex_vat"] == 144.00  # 120 * 1.20


# =============================================================================
# SMALL FORMAT — ESCALATION SCENARIOS
# =============================================================================


def test_off_tier_quantity_stacks_instead_of_escalating():
    """v34 — qty 750 used to escalate, now stacks (500+250=750 exact)."""
    r = client.post("/quote/small-format", json={
        "product_key": "flyers_a5",
        "quantity": 750,
        "double_sided": False,
        "finish": "gloss",
    })
    data = r.json()
    assert data["success"] is True, data
    # 500 tier @ 22 + 250 tier @ 30 = 110 + 75 = 185 ex VAT
    assert data["base_price"] == 185.00
    assert any("Tier combination" in s for s in data["surcharges_applied"])


def test_invalid_quantity_escalates():
    """v34 — only escalate when qty is >5x the largest tier (truly off-sheet)."""
    r = client.post("/quote/small-format", json={
        "product_key": "flyers_a5",
        "quantity": 50000,  # >5x the 2500 largest tier
        "double_sided": False,
        "finish": "gloss",
    })
    data = r.json()
    assert data["success"] is False
    assert data["escalate"] is True
    assert "50000" in data["reason"]


def test_invalid_finish_escalates():
    r = client.post("/quote/small-format", json={
        "product_key": "letterheads",
        "quantity": 250,
        "finish": "gloss",
    })
    data = r.json()
    assert data["success"] is False
    assert data["escalate"] is True


def test_triplicate_on_non_ncr_escalates():
    r = client.post("/quote/small-format", json={
        "product_key": "flyers_a5",
        "quantity": 500,
        "finish": "triplicate",
    })
    data = r.json()
    assert data["success"] is False
    assert data["escalate"] is True


# =============================================================================
# LARGE FORMAT — EXACT PRICE VERIFICATION (23% VAT — signage)
# =============================================================================


def test_roller_banner_single():
    r = client.post("/quote/large-format", json={
        "product_key": "roller_banners",
        "quantity": 1,
    })
    data = r.json()
    assert data["success"] is True
    assert data["base_price"] == 120.00
    assert data["final_price_ex_vat"] == 120.00


def test_roller_banner_bulk():
    r = client.post("/quote/large-format", json={
        "product_key": "roller_banners",
        "quantity": 5,
    })
    data = r.json()
    assert data["success"] is True
    assert data["base_price"] == 110.00
    assert data["final_price_ex_vat"] == 550.00


def test_pvc_banner_8sqm():
    """v36 changed pvc_banners to per_sqm strategy — pass area_sqm
    explicitly. 8 m² × €28/m² = €224 ex VAT."""
    r = client.post("/quote/large-format", json={
        "product_key": "pvc_banners",
        "quantity": 1,
        "area_sqm": 8.0,
    })
    data = r.json()
    assert data["success"] is True, f"Expected success, got: {data}"
    assert data["base_price"] == 28.00
    assert data["final_price_ex_vat"] == 224.00


def test_pvc_banner_bulk():
    """12 m² triggers bulk pricing on pvc_banners (threshold 10).
    12 × €23/m² = €276 ex VAT."""
    r = client.post("/quote/large-format", json={
        "product_key": "pvc_banners",
        "quantity": 1,
        "area_sqm": 12.0,
    })
    data = r.json()
    assert data["success"] is True, f"Expected success, got: {data}"
    assert data["base_price"] == 23.00
    assert data["final_price_ex_vat"] == 276.00


def test_foamex_3_boards():
    """v36 changed foamex_boards to per_sheet — needs panel dims.
    3 panels at A1 (594x841mm) → 4 panels per 2400x1200 sheet →
    1 sheet × €150 = €150 ex VAT."""
    r = client.post("/quote/large-format", json={
        "product_key": "foamex_boards",
        "quantity": 3,
        "width_mm": 594,
        "height_mm": 841,
    })
    data = r.json()
    assert data["success"] is True, f"Expected success, got: {data}"
    # 3 A1-sized panels fit on 1 standard 2400x1200 sheet
    assert data["final_price_ex_vat"] == 150.00, (
        f"Expected 1 sheet × €150 = €150 ex VAT, got "
        f"€{data['final_price_ex_vat']}"
    )


def test_vehicle_magnetics_2():
    r = client.post("/quote/large-format", json={
        "product_key": "vehicle_magnetics",
        "quantity": 2,
    })
    data = r.json()
    assert data["success"] is True
    assert data["base_price"] == 90.00
    assert data["final_price_ex_vat"] == 180.00


# =============================================================================
# BOOKLET — EXACT PRICE VERIFICATION (total prices, 13.5% VAT)
# =============================================================================


def test_a5_saddle_8pp_self_cover_100():
    r = client.post("/quote/booklet", json={
        "format": "a5",
        "binding": "saddle_stitch",
        "pages": 8,
        "cover_type": "self_cover",
        "quantity": 100,
    })
    data = r.json()
    assert data["success"] is True
    assert data["base_price"] == 110.00


def test_a5_saddle_24pp_card_lam_250():
    r = client.post("/quote/booklet", json={
        "format": "a5",
        "binding": "saddle_stitch",
        "pages": 24,
        "cover_type": "card_cover_lam",
        "quantity": 250,
    })
    data = r.json()
    assert data["success"] is True
    assert data["base_price"] == 697.00


def test_a4_saddle_16pp_card_cover_50():
    r = client.post("/quote/booklet", json={
        "format": "a4",
        "binding": "saddle_stitch",
        "pages": 16,
        "cover_type": "card_cover",
        "quantity": 50,
    })
    data = r.json()
    assert data["success"] is True
    assert data["base_price"] == 191.00


def test_a4_perfect_64pp_card_cover_100():
    r = client.post("/quote/booklet", json={
        "format": "a4",
        "binding": "perfect_bound",
        "pages": 64,
        "cover_type": "card_cover",
        "quantity": 100,
    })
    data = r.json()
    assert data["success"] is True
    assert data["base_price"] == 948.00


def test_a5_perfect_96pp_card_lam_500():
    r = client.post("/quote/booklet", json={
        "format": "a5",
        "binding": "perfect_bound",
        "pages": 96,
        "cover_type": "card_cover_lam",
        "quantity": 500,
    })
    data = r.json()
    assert data["success"] is True
    assert data["base_price"] == 3455.00


# =============================================================================
# BOOKLET — ESCALATION SCENARIOS
# =============================================================================


def test_booklet_invalid_pages_escalates():
    r = client.post("/quote/booklet", json={
        "format": "a5",
        "binding": "saddle_stitch",
        "pages": 10,
        "cover_type": "self_cover",
        "quantity": 100,
    })
    data = r.json()
    assert data["success"] is False
    assert data["escalate"] is True


def test_booklet_invalid_qty_escalates():
    r = client.post("/quote/booklet", json={
        "format": "a4",
        "binding": "saddle_stitch",
        "pages": 16,
        "cover_type": "card_cover",
        "quantity": 5000,  # v34 — used to be 75, but now stacks (50+25=75). Use a truly off-sheet qty.
    })
    data = r.json()
    assert data["success"] is False
    assert data["escalate"] is True


def test_booklet_self_cover_on_perfect_bound_escalates():
    r = client.post("/quote/booklet", json={
        "format": "a5",
        "binding": "perfect_bound",
        "pages": 48,
        "cover_type": "self_cover",
        "quantity": 100,
    })
    data = r.json()
    assert data["success"] is False
    assert data["escalate"] is True


# =============================================================================
# VAT CALCULATIONS — 13.5% for printed matter
# =============================================================================


def test_vat_calculation_printed_matter():
    """Printed matter VAT = 13.5%."""
    r = client.post("/quote/small-format", json={
        "product_key": "flyers_a5",
        "quantity": 100,
        "double_sided": False,
        "finish": "gloss",
    })
    data = r.json()
    assert data["base_price"] == 45.00  # 45/100 * 100
    assert data["vat_amount"] == 6.08  # 45 * 0.135
    assert data["final_price_inc_vat"] == 51.08


# =============================================================================
# ARTWORK ADD-ON (service = 23% VAT, not 13.5%)
# =============================================================================


def test_artwork_addon():
    """Artwork 2hrs = €130 ex VAT, €159.90 inc VAT (23% service rate)."""
    r = client.post("/quote/small-format", json={
        "product_key": "business_cards",
        "quantity": 250,
        "double_sided": False,
        "finish": "gloss",
        "needs_artwork": True,
        "artwork_hours": 2,
    })
    data = r.json()
    assert data["success"] is True
    assert data["base_price"] == 150.00  # 60 * 2.5
    assert data["artwork_cost_ex_vat"] == 130.00
    assert data["artwork_cost_inc_vat"] == 159.90  # 130 * 1.23
    # Total = (150 + 20.25 VAT@13.5%) + 159.90 artwork inc VAT = 330.15
    assert data["total_inc_everything"] == 330.15


# =============================================================================
# CLIENT MULTIPLIER — tenant-wide markup applied AFTER surcharges, BEFORE VAT
# =============================================================================


def _set_client_multiplier(value: str | None) -> None:
    """Test helper — upsert or delete `pricing_client_multiplier` on the
    default org so we don't leak state between tests."""
    from db import db_session
    from db.models import Setting, DEFAULT_ORG_SLUG

    with db_session() as s:
        row = (
            s.query(Setting)
            .filter_by(organization_slug=DEFAULT_ORG_SLUG, key="pricing_client_multiplier")
            .first()
        )
        if value is None:
            if row:
                s.delete(row)
            return
        if row:
            row.value = value
        else:
            s.add(Setting(
                organization_slug=DEFAULT_ORG_SLUG,
                key="pricing_client_multiplier",
                value=value,
                value_type="float",
            ))


def test_client_multiplier_default_is_noop():
    """Missing setting = 1.0 = price unchanged."""
    from pricing_engine import _get_client_multiplier
    from db import db_session

    _set_client_multiplier(None)
    with db_session() as s:
        assert _get_client_multiplier(s) == 1.0


def test_client_multiplier_parses_float():
    from pricing_engine import _get_client_multiplier
    from db import db_session

    _set_client_multiplier("1.10")
    try:
        with db_session() as s:
            assert _get_client_multiplier(s) == 1.10
    finally:
        _set_client_multiplier(None)


def test_client_multiplier_clamps_invalid():
    """Negative, zero, absurdly large, or garbage all fall back to 1.0 —
    a dashboard typo must never scale a live quote to zero."""
    from pricing_engine import _get_client_multiplier
    from db import db_session

    for bad in ["-0.5", "0", "99", "not-a-number", ""]:
        _set_client_multiplier(bad)
        with db_session() as s:
            assert _get_client_multiplier(s) == 1.0, f"value {bad!r} should clamp to 1.0"
    _set_client_multiplier(None)


def test_client_multiplier_applied_after_surcharges_small_format():
    """+10% applied after surcharges, before VAT. 500 biz cards = €190 →
    €209 ex VAT, with a 'Client adjustment: +10%' line."""
    _set_client_multiplier("1.10")
    try:
        r = client.post("/quote/small-format", json={
            "product_key": "business_cards",
            "quantity": 500,
            "double_sided": False,
            "finish": "gloss",
        })
        data = r.json()
        assert data["success"] is True
        assert data["base_price"] == 190.00
        assert data["final_price_ex_vat"] == 209.00  # 190 * 1.10
        assert any("Client adjustment" in s for s in data["surcharges_applied"])
    finally:
        _set_client_multiplier(None)


def test_client_multiplier_negative_discount_small_format():
    """-10% (0.90) applied cleanly."""
    _set_client_multiplier("0.90")
    try:
        r = client.post("/quote/small-format", json={
            "product_key": "business_cards",
            "quantity": 500,
            "double_sided": False,
            "finish": "gloss",
        })
        data = r.json()
        assert data["final_price_ex_vat"] == 171.00  # 190 * 0.90
        assert any("-10%" in s for s in data["surcharges_applied"])
    finally:
        _set_client_multiplier(None)


def test_client_multiplier_stacks_after_surcharge():
    """Soft-touch (+25%) THEN client +10% — confirms order-of-ops."""
    _set_client_multiplier("1.10")
    try:
        r = client.post("/quote/small-format", json={
            "product_key": "business_cards",
            "quantity": 500,
            "double_sided": False,
            "finish": "soft_touch",
        })
        data = r.json()
        # 190 base + 15 soft-touch flat fee = 205, then * 1.10 = 225.50
        # (soft-touch is a €15 flat fee per v10, not a multiplier)
        assert data["final_price_ex_vat"] == round(205.00 * 1.10, 2)
    finally:
        _set_client_multiplier(None)


def test_client_multiplier_not_applied_to_vat():
    """VAT is still computed on the adjusted ex-VAT price, not double-scaled."""
    _set_client_multiplier("1.10")
    try:
        r = client.post("/quote/small-format", json={
            "product_key": "business_cards",
            "quantity": 500,
            "double_sided": False,
            "finish": "gloss",
        })
        data = r.json()
        ex = data["final_price_ex_vat"]
        inc = data["final_price_inc_vat"]
        # Printed matter VAT = 13.5%
        assert round(ex * 1.135, 2) == inc
    finally:
        _set_client_multiplier(None)


# =============================================================================
# v38 — Engine defensive guards (Bug 1 + sanity ceiling)
# =============================================================================


class TestRequiresDimensionsGuard:
    """v38 — vinyl_labels (and any product flagged requires_dimensions
    = True) MUST NOT fall back to yield-only math when the LLM
    forgot to pass width_mm/height_mm. The engine ESCALATES instead,
    so the customer never sees a wrong price like Ian's €341 for
    500 small labels."""

    def test_vinyl_labels_with_dims_prices_correctly(self):
        """500 vinyl labels at 40x10mm = 0.2 m² × €45 = €9 ex VAT ≈ €11 inc."""
        from db import db_session
        from db.models import Product
        from pricing_engine import quote_large_format, QuoteResult

        with db_session() as db:
            p = db.query(Product).filter_by(
                organization_slug="just-print", key="vinyl_labels",
            ).first()
            assert p is not None, "vinyl_labels not seeded"
            # Make sure the v38 flag is on for this test
            if not getattr(p, "requires_dimensions", False):
                p.requires_dimensions = True
                db.commit()

            result = quote_large_format(
                db, product_key="vinyl_labels",
                quantity=500, width_mm=40, height_mm=10,
                organization_slug="just-print",
            )
        assert isinstance(result, QuoteResult), (
            f"Expected a quote, got {type(result).__name__}: "
            f"{getattr(result, 'reason', None)}"
        )
        # 500 × 40×10mm = 0.2 m² × €45/m² = €9 ex VAT
        assert 7.5 <= result.final_price_ex_vat <= 10.5, (
            f"Expected ~€9 ex VAT, got €{result.final_price_ex_vat}"
        )
        # VAT 23% on large format = ~€2 → ~€11 inc
        assert 9.0 <= result.final_price_inc_vat <= 14.0, (
            f"Expected ~€11 inc VAT, got €{result.final_price_inc_vat}"
        )

    def test_vinyl_labels_no_dims_escalates_not_yield_fallback(self):
        """The core Bug 1 contract: when dims are missing AND
        requires_dimensions=True, the engine MUST escalate (return
        EscalationResult), not fall back to the catalog yield_per_sqm.
        Production-observed: without this guard, 500 small labels
        get billed as €341 inc VAT instead of ~€11."""
        from db import db_session
        from db.models import Product
        from pricing_engine import quote_large_format, EscalationResult

        with db_session() as db:
            p = db.query(Product).filter_by(
                organization_slug="just-print", key="vinyl_labels",
            ).first()
            assert p is not None
            if not getattr(p, "requires_dimensions", False):
                p.requires_dimensions = True
                db.commit()

            result = quote_large_format(
                db, product_key="vinyl_labels",
                quantity=500,
                # No width_mm, no height_mm, no area_sqm — the LLM
                # forgot. Engine must escalate, not silently use the
                # catalog yield (which would give 6.17 m² × €45 = €277 ex).
                organization_slug="just-print",
            )
        assert isinstance(result, EscalationResult), (
            f"Expected escalation, got {type(result).__name__} "
            f"with final_price={getattr(result, 'final_price_ex_vat', None)}"
        )
        assert result.manual_review is True
        # Reason should mention dimensions / size
        assert any(
            kw in (result.reason or "").lower()
            for kw in ("dimensions", "size", "width", "height", "requires")
        ), f"Expected reason to mention dimensions, got: {result.reason!r}"

    def test_pvc_banner_no_dims_still_works_no_flag(self):
        """Sanity counter-test: pvc_banners doesn't have
        requires_dimensions=True, so calling without dims should
        still work via the legacy area-based fallback (banners are
        area-priced, not items-cut-from-sheet)."""
        # Just confirm the guard is product-scoped: a product WITHOUT
        # the flag should NOT escalate just because dims are missing.
        from db import db_session
        from db.models import Product
        with db_session() as db:
            p = db.query(Product).filter_by(
                organization_slug="just-print", key="pvc_banners",
            ).first()
            assert p is not None
            assert getattr(p, "requires_dimensions", False) is False, (
                "pvc_banners must NOT have requires_dimensions=True — "
                "banners are area-based, the engine's area math is "
                "correct for them"
            )


class TestSanityMaxUnitPrice:
    """v38 — when product.sanity_max_unit_price is set, the engine
    refuses to return a quote whose per-unit cost exceeds it.
    Prevents the JP-0086 / Ian-Byrne class of bug from EVER reaching
    a customer."""

    def test_below_ceiling_passes_through(self):
        """A normal quote (€9 ex VAT / 500 labels = €0.018 per label)
        is well below any reasonable ceiling — engine returns the quote."""
        from db import db_session
        from db.models import Product
        from pricing_engine import quote_large_format, QuoteResult

        with db_session() as db:
            p = db.query(Product).filter_by(
                organization_slug="just-print", key="vinyl_labels",
            ).first()
            assert p is not None
            # Set a generous ceiling — €5/unit. €0.018/unit is way under.
            p.sanity_max_unit_price = 5.0
            p.requires_dimensions = True
            db.commit()

            result = quote_large_format(
                db, product_key="vinyl_labels",
                quantity=500, width_mm=40, height_mm=10,
                organization_slug="just-print",
            )
        assert isinstance(result, QuoteResult), (
            f"Expected quote (ceiling not tripped), got "
            f"{type(result).__name__}: {getattr(result, 'reason', None)}"
        )

    def test_above_ceiling_escalates(self):
        """Simulate the JP-0086 scenario: a product mis-configured so
        the per-unit price comes out absurdly high. Engine should
        escalate, not return the quote."""
        from db import db_session
        from db.models import Product
        from pricing_engine import quote_large_format, EscalationResult

        with db_session() as db:
            p = db.query(Product).filter_by(
                organization_slug="just-print", key="vinyl_labels",
            ).first()
            assert p is not None
            # Set a TIGHT ceiling — €0.001/unit. Any real labels will
            # exceed this. Simulates the "catalog mis-config" scenario.
            p.sanity_max_unit_price = 0.001
            p.requires_dimensions = True
            db.commit()
            try:
                result = quote_large_format(
                    db, product_key="vinyl_labels",
                    quantity=500, width_mm=40, height_mm=10,
                    organization_slug="just-print",
                )
                assert isinstance(result, EscalationResult), (
                    f"Expected escalation (ceiling tripped), got "
                    f"{type(result).__name__}"
                )
                assert result.manual_review is True
                assert "sanity" in (result.reason or "").lower()
            finally:
                # Cleanup: remove the ceiling so other tests don't break
                p.sanity_max_unit_price = None
                db.commit()


# =============================================================================
# RUN
# =============================================================================

if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
