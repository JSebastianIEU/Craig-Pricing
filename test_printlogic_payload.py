"""
Unit tests for printlogic_payload — the rich create_order body builder.

Covers:
  * The lookup tables (KNOWN_DIMENSIONS_MM, PAPER_DESCRIPTIONS) return
    the values we expect for known products and empty for unknown ones.
  * `build_payload_from_quote` produces the new rich item fields
    (width_mm, height_mm, paper_description, finishing_description,
    finished_size_text, pages, colors) when it can.
  * Order-level fields (`order_date_due`, `contact_email`, `contact_phone`,
    `customer_uid`) are populated correctly.
  * `build_demo_payload` returns a payload with the marker side-channel
    + every rich field set.
  * `due_date` skips weekends correctly.
"""

from __future__ import annotations

import datetime as _dt
import json
import os

os.environ.setdefault(
    "STRATEGOS_JWT_SECRET",
    "test-secret-32-bytes-long-padding-enough-now",
)

import printlogic_payload as plp


class _FakeQuote:
    """Mimics a Quote ORM row with just the attrs the builder reads."""
    def __init__(
        self, *, id=1, product_key="business_cards", specs=None,
        final_price_ex_vat=100.0, vat_amount=23.0,
    ):
        self.id = id
        self.product_key = product_key
        self.specs = specs or {}
        self.final_price_ex_vat = final_price_ex_vat
        self.vat_amount = vat_amount


class _FakeConv:
    def __init__(self, name="", email="", phone=""):
        self.customer_name = name
        self.customer_email = email
        self.customer_phone = phone


# ---------------------------------------------------------------------------
# Lookup tables
# ---------------------------------------------------------------------------


def test_known_dimensions_for_business_cards():
    assert plp.known_dimensions_mm("business_cards") == (85, 55)


def test_known_dimensions_for_a4_flyer():
    assert plp.known_dimensions_mm("flyers_a4") == (210, 297)


def test_unknown_product_returns_none():
    assert plp.known_dimensions_mm("xyz_unknown") is None
    assert plp.known_dimensions_mm(None) is None


def test_paper_description_for_business_cards():
    assert plp.paper_description_for("business_cards") == "400gsm silk"


def test_paper_description_for_unknown_returns_empty():
    assert plp.paper_description_for("xyz") == ""
    assert plp.paper_description_for(None) == ""


# ---------------------------------------------------------------------------
# Per-item helpers
# ---------------------------------------------------------------------------


def test_finished_size_text_for_known_product():
    text = plp.finished_size_text({}, "business_cards")
    assert text == "Business card (85 x 55 mm)"


def test_finished_size_text_for_large_format_in_meters():
    text = plp.finished_size_text({"width_m": 1.5, "height_m": 2}, "banner_pvc")
    assert text == "1.5 x 2 m"


def test_finished_size_text_for_explicit_mm():
    text = plp.finished_size_text({"width_mm": 100, "height_mm": 200}, "custom")
    assert text == "100 x 200 mm"


def test_finished_size_text_unknown_returns_empty():
    assert plp.finished_size_text({}, "no_known_product") == ""


def test_width_height_mm_business_cards():
    w, h = plp.width_height_mm({}, "business_cards")
    assert (w, h) == ("85", "55")


def test_width_height_mm_large_format_meters_to_mm():
    w, h = plp.width_height_mm({"width_m": 1.5, "height_m": 2}, "banner")
    # 1.5m -> 1500mm, 2m -> 2000mm
    assert (w, h) == ("1500", "2000")


def test_width_height_mm_unknown_returns_empty():
    w, h = plp.width_height_mm({}, "xyz")
    assert (w, h) == ("", "")


def test_colors_double_sided_default():
    assert plp.colors_spec({}, double_sided=True) == "4/4"


def test_colors_single_sided_default():
    assert plp.colors_spec({}, double_sided=False) == "4/0"


def test_colors_explicit_override():
    assert plp.colors_spec({"colors": "1/0"}, double_sided=False) == "1/0"


def test_pages_default_double_sided():
    assert plp.pages_spec({}, double_sided=True) == "2"


def test_pages_default_single_sided():
    assert plp.pages_spec({}, double_sided=False) == "1"


def test_pages_explicit_for_booklet():
    assert plp.pages_spec({"pages": 32}, double_sided=False) == "32"


def test_finishing_description_combines_finishes():
    text = plp.finishing_description(
        {"finish": "gloss", "soft_touch": True}, double_sided=True,
    )
    assert "gloss" in text
    assert "soft-touch laminate" in text
    assert "double-sided" in text


def test_finishing_description_skips_none_finish():
    text = plp.finishing_description({"finish": "none"}, double_sided=False)
    assert "none" not in text.lower().replace("single-sided", "")
    assert "single-sided" in text


# ---------------------------------------------------------------------------
# Order-level: due_date
# ---------------------------------------------------------------------------


def test_due_date_skips_weekend():
    # Friday + 1 working day = Monday, not Saturday
    friday = _dt.date(2026, 5, 1)  # 2026-05-01 is a Friday
    assert _dt.date(2026, 5, 1).weekday() == 4
    due = plp.due_date(turnaround_days=1, today=friday)
    assert due == "2026-05-04"  # Monday


def test_due_date_default_5_working_days_from_monday():
    monday = _dt.date(2026, 4, 27)  # Monday
    assert monday.weekday() == 0
    due = plp.due_date(turnaround_days=5, today=monday)
    # Mon -> next Monday is 7 calendar days but 5 working days
    assert due == "2026-05-04"


# ---------------------------------------------------------------------------
# build_payload_from_quote
# ---------------------------------------------------------------------------


def test_build_payload_business_cards_full_shape():
    q = _FakeQuote(
        id=42, product_key="business_cards",
        specs={"quantity": 250, "finish": "soft-touch", "soft_touch": True,
               "double_sided": True},
        final_price_ex_vat=100.0, vat_amount=23.0,
    )
    conv = _FakeConv(name="ACME Ltd", email="orders@acme.ie", phone="+353 1 555")
    payload = plp.build_payload_from_quote(q, conv)

    # Order-level
    assert payload["customer_name"] == "ACME Ltd"
    assert payload["customer_email"] == "orders@acme.ie"
    assert payload["customer_phone"] == "+353 1 555"
    assert payload["contact_name"] == "ACME Ltd"
    assert payload["contact_email"] == "orders@acme.ie"  # CRITICAL: was missing before
    assert payload["contact_phone"] == "+353 1 555"
    assert payload["order_description"].startswith("[CRAIG-PUSH qid=42]")
    # order_date_due is a real ISO date, not "0000-00-00"
    _dt.date.fromisoformat(payload["order_date_due"])

    # Item-level
    assert len(payload["order_items"]) == 1
    item = payload["order_items"][0]
    assert item["item_quantity"] == "250"
    assert item["item_price"] == "100.00"
    assert item["item_vat"] == "23.0"
    assert item["item_code"] == "business_cards"
    # Rich fields all populated
    assert item["item_width_mm"] == "85"
    assert item["item_height_mm"] == "55"
    assert "Business card" in item["item_finished_size_text"]
    assert item["item_paper_description"] == "400gsm silk"
    assert "soft-touch" in item["item_finishing_description"]
    assert "double-sided" in item["item_finishing_description"]
    assert item["item_pages"] == "2"  # double-sided -> 2
    assert item["item_colors"] == "4/4"  # full colour both sides
    # Custom data audit trail
    custom = json.loads(item["item_custom_data"])
    assert custom["craig_quote_id"] == 42
    assert custom["craig_specs"]["quantity"] == 250


def test_build_payload_with_customer_uid_skips_dedup():
    q = _FakeQuote()
    conv = _FakeConv(name="X")
    payload = plp.build_payload_from_quote(q, conv, customer_uid="557495")
    assert payload["customer_uid"] == "557495"


def test_build_payload_no_conv_uses_safe_defaults():
    q = _FakeQuote()
    payload = plp.build_payload_from_quote(q, None)
    assert payload["customer_name"] == "Craig customer"
    assert payload["customer_email"] == ""
    assert payload["contact_email"] == ""


def test_build_payload_unknown_product_leaves_dimensions_blank():
    q = _FakeQuote(product_key="custom_thing", specs={"quantity": 1})
    payload = plp.build_payload_from_quote(q, _FakeConv())
    item = payload["order_items"][0]
    assert item["item_width_mm"] == ""
    assert item["item_height_mm"] == ""
    assert item["item_paper_description"] == ""


def test_build_payload_large_format_meters_become_mm():
    q = _FakeQuote(
        product_key="banner_pvc",
        specs={"quantity": 1, "width_m": 1.5, "height_m": 2.0},
    )
    payload = plp.build_payload_from_quote(q, _FakeConv())
    item = payload["order_items"][0]
    assert item["item_width_mm"] == "1500"
    assert item["item_height_mm"] == "2000"
    assert item["item_finished_size_text"] == "1.5 x 2.0 m"


# ---------------------------------------------------------------------------
# build_demo_payload (dashboard test-order button)
# ---------------------------------------------------------------------------


def test_build_demo_payload_has_marker_and_ts_side_channels():
    payload = plp.build_demo_payload()
    assert "_marker" in payload
    assert payload["_marker"].startswith("[CRAIG-PROBE-DELETE-ME-")
    assert "_ts" in payload
    assert isinstance(payload["_ts"], int)


def test_build_demo_payload_rich_item_shape():
    payload = plp.build_demo_payload()
    item = payload["order_items"][0]
    # Every rich field set with confident values
    assert item["item_width_mm"] == "85"
    assert item["item_height_mm"] == "55"
    assert item["item_paper_description"] == "400gsm silk"
    assert "Business card" in item["item_finished_size_text"]
    assert "soft-touch" in item["item_finishing_description"]
    assert item["item_quantity"] == "250"


def test_build_demo_payload_includes_contact_fields():
    payload = plp.build_demo_payload()
    assert payload["contact_email"] == "probe@strategos-ai.com"
    assert payload["contact_phone"]
    assert payload["contact_name"].startswith("CRAIG-PROBE-DO-NOT-PROCESS-")
    # Real due_date, not 0000-00-00
    _dt.date.fromisoformat(payload["order_date_due"])
