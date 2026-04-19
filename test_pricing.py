"""
Test suite for Craig Pricing Service.
Tests real scenarios against Justin's spreadsheet values.

PRICING MODEL (confirmed by Justin 16 Apr 2026):
- Small format prices are PER UNIT BASE (per 100 for cards/flyers, per 5 for NCR pads)
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
    assert len(data) == 26  # 10 small + 12 large + 4 booklet
    categories = set(d["category"] for d in data)
    assert "small_format" in categories
    assert "large_format" in categories
    assert "booklet" in categories


def test_products_endpoint():
    """Products endpoint returns all 26 products."""
    r = client.get("/products")
    assert r.status_code == 200


# =============================================================================
# SMALL FORMAT — UNIT-BASED PRICING (per 100 cards/flyers, per 5 NCR pads)
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
    """250 biz cards @ €60/100 = €150, +25% soft-touch = €187.50."""
    r = client.post("/quote/small-format", json={
        "product_key": "business_cards",
        "quantity": 250,
        "double_sided": False,
        "finish": "soft-touch",
    })
    data = r.json()
    assert data["success"] is True
    assert data["base_price"] == 150.00  # 60 * (250/100)
    assert data["final_price_ex_vat"] == 187.50  # 150 * 1.25
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
    """10 NCR A5 pads @ €90/5 pads = €180."""
    r = client.post("/quote/small-format", json={
        "product_key": "ncr_pads_a5",
        "quantity": 10,
        "double_sided": False,
        "finish": "duplicate",
    })
    data = r.json()
    assert data["success"] is True
    assert data["base_price"] == 180.00  # 90 * (10/5)
    assert data["final_price_ex_vat"] == 180.00


def test_ncr_a4_20_triplicate():
    """20 NCR A4 pads @ €85/5 pads = €340, +10% triplicate = €374."""
    r = client.post("/quote/small-format", json={
        "product_key": "ncr_pads_a4",
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


def test_invalid_quantity_escalates():
    r = client.post("/quote/small-format", json={
        "product_key": "flyers_a5",
        "quantity": 750,
        "double_sided": False,
        "finish": "gloss",
    })
    data = r.json()
    assert data["success"] is False
    assert data["escalate"] is True
    assert "750" in data["reason"]


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
    r = client.post("/quote/large-format", json={
        "product_key": "pvc_banners",
        "quantity": 8,
    })
    data = r.json()
    assert data["success"] is True
    assert data["base_price"] == 28.00
    assert data["final_price_ex_vat"] == 224.00


def test_pvc_banner_bulk():
    r = client.post("/quote/large-format", json={
        "product_key": "pvc_banners",
        "quantity": 12,
    })
    data = r.json()
    assert data["success"] is True
    assert data["base_price"] == 23.00
    assert data["final_price_ex_vat"] == 276.00


def test_foamex_3_boards():
    r = client.post("/quote/large-format", json={
        "product_key": "foamex_boards",
        "quantity": 3,
    })
    data = r.json()
    assert data["success"] is True
    assert data["base_price"] == 35.00
    assert data["final_price_ex_vat"] == 105.00


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
        "quantity": 75,
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
# RUN
# =============================================================================

if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
