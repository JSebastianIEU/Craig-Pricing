"""
Rich PrintLogic create_order payload builders.

Why this module exists
======================
PrintLogic's `create_order` accepts MANY more fields than we used to
populate. The probe (scripts/probe_printlogic_order_shape.py) revealed
the per-item shape PrintLogic actually stores:

    width_mm, height_mm, finished_size_text, pages, colors,
    paper_description, finishing_description, service,
    parent_substrate_*_mm, ws_item_type

…plus order-level `order_date_due`, `contact_email`, `contact_phone`.

IMPORTANT — empirical finding (live probe, see commit notes)
------------------------------------------------------------
`create_order` does NOT consume the rich per-item fields. We probed
20 name variations (width_mm, item_width, paper_description, paper,
stock, substrate, finishing, pages, colors, colours, …) — every one
silently dropped. PrintLogic populates those fields via a different
workflow (UI, OnPrintShop sync, or an API action we haven't located).
`order_date_due` DOES land. `contact_*` does NOT.

Until Alex confirms the right path, this module:
  1. Still SENDS the rich item fields (zero cost; the moment they're
     enabled, the code Just Works).
  2. Packs the same info as a multi-line jobsheet into `item_detail`,
     which IS stored — Justin sees paper, dimensions, finish, colours,
     pages when he opens the order.
  3. Always sets `order_date_due` (today + N working days).
  4. Sets `contact_*` (currently dropped but cheap to keep).

Design principles
=================
1. Pure functions. No DB access here — caller resolves Quote/Conversation
   and passes the data in.
2. Defensive: every field has a sensible default if the quote's specs
   don't include it. We never raise.
3. Lookups are conservative: we ONLY claim a paper / dimension when
   we're confident (e.g. business_cards has a globally standard size).
   When unsure, we leave the field empty so Justin's PrintLogic UI
   shows a blank rather than a wrong value.
"""

from __future__ import annotations

import datetime as _dt
import json
from typing import Any


# ---------------------------------------------------------------------------
# Lookup tables — derived from Justin's pricing sheet + standard print sizes.
# Update these when adding / renaming products in the catalog.
# ---------------------------------------------------------------------------

# Standard finished-piece dimensions in mm. Tuple is (width, height).
# Only fill in what's GLOBALLY standard or what Justin's sheet declares.
KNOWN_DIMENSIONS_MM: dict[str, tuple[int, int]] = {
    # Small format
    "business_cards": (85, 55),
    "compliments_slip": (210, 99),
    "letterheads": (210, 297),
    "flyers_a7": (74, 105),
    "flyers_a6": (105, 148),
    "flyers_a5": (148, 210),
    "flyers_a4": (210, 297),
    "flyers_a3": (297, 420),
    "flyers_dl": (99, 210),
    "folded_leaflets_a5_to_a6": (148, 210),
    "folded_leaflets_a4_to_dl": (210, 297),
    "folded_leaflets_a4_to_a5": (210, 297),
    "folded_leaflets_a3_to_a4": (297, 420),
    "presentation_folders_a4": (220, 310),
    # Booklets — flat trim size before fold/bind
    "booklets_a4": (210, 297),
    "booklets_a5": (148, 210),
}

# Default paper / stock description per product_key. Pulled from Justin's
# pricing sheet. Update when the source-of-truth JSON in data/ changes.
PAPER_DESCRIPTIONS: dict[str, str] = {
    "business_cards": "400gsm silk",
    "compliments_slip": "120gsm uncoated",
    "letterheads": "100gsm uncoated",
    "flyers_a7": "170gsm gloss",
    "flyers_a6": "170gsm gloss",
    "flyers_a5": "170gsm gloss",
    "flyers_a4": "170gsm gloss",
    "flyers_a3": "170gsm gloss",
    "flyers_dl": "170gsm gloss",
    "folded_leaflets_a5_to_a6": "150gsm gloss",
    "folded_leaflets_a4_to_dl": "150gsm gloss",
    "folded_leaflets_a4_to_a5": "150gsm gloss",
    "folded_leaflets_a3_to_a4": "150gsm gloss",
    "presentation_folders_a4": "350gsm silk",
    "booklets_a4": "150gsm gloss inner",
    "booklets_a5": "150gsm gloss inner",
}


def known_dimensions_mm(product_key: str | None) -> tuple[int, int] | None:
    """Return (w, h) in mm if we have a confident standard for this product."""
    if not product_key:
        return None
    return KNOWN_DIMENSIONS_MM.get(product_key)


def paper_description_for(product_key: str | None) -> str:
    """Return the default paper description, or '' if unknown."""
    if not product_key:
        return ""
    return PAPER_DESCRIPTIONS.get(product_key, "")


# ---------------------------------------------------------------------------
# Per-item helpers
# ---------------------------------------------------------------------------


def finished_size_text(specs: dict, product_key: str | None) -> str:
    """
    Build the human-readable size string PrintLogic stores in
    `finished_size_text`. Examples:
      - "85 x 55 mm"
      - "A4 (210 x 297 mm)"
      - "1.5 x 2 m"  (large format)
    """
    # Large format dims are stored in specs as width_m / height_m
    if specs.get("width_m") or specs.get("height_m"):
        w = specs.get("width_m") or "?"
        h = specs.get("height_m") or "?"
        return f"{w} x {h} m"

    # If specs have explicit mm, prefer them
    w_mm = specs.get("width_mm") or specs.get("size_w_mm")
    h_mm = specs.get("height_mm") or specs.get("size_h_mm")
    if w_mm and h_mm:
        return f"{int(w_mm)} x {int(h_mm)} mm"

    # Otherwise fall back to the known-product table
    dims = known_dimensions_mm(product_key)
    if dims:
        size_label = _size_label(product_key, dims)
        return f"{size_label} ({dims[0]} x {dims[1]} mm)"
    return ""


def _size_label(product_key: str, dims: tuple[int, int]) -> str:
    """Cosmetic label like 'A4' or '85x55' for the size_text prefix."""
    # Match common ISO sizes
    iso_map = {
        (85, 55): "Business card",
        (74, 105): "A7",
        (105, 148): "A6",
        (148, 210): "A5",
        (210, 297): "A4",
        (297, 420): "A3",
        (99, 210): "DL",
        (220, 310): "A4 folder",
    }
    return iso_map.get(dims, f"{dims[0]}x{dims[1]}mm")


def width_height_mm(specs: dict, product_key: str | None) -> tuple[str, str]:
    """
    Return (width_mm_str, height_mm_str) for the create_order item.
    Strings — PrintLogic stores them as strings. Empty if no confident value.
    """
    # Large format → convert m → mm
    if specs.get("width_m") or specs.get("height_m"):
        try:
            w_m = float(specs.get("width_m") or 0)
            h_m = float(specs.get("height_m") or 0)
            if w_m and h_m:
                return (str(int(w_m * 1000)), str(int(h_m * 1000)))
        except (TypeError, ValueError):
            pass

    # Explicit mm in specs
    w = specs.get("width_mm") or specs.get("size_w_mm")
    h = specs.get("height_mm") or specs.get("size_h_mm")
    if w and h:
        return (str(int(w)), str(int(h)))

    # Known table
    dims = known_dimensions_mm(product_key)
    if dims:
        return (str(dims[0]), str(dims[1]))
    return ("", "")


def colors_spec(specs: dict, double_sided: bool) -> str:
    """
    Print-trade convention for colors: '4/4' = full colour both sides,
    '4/0' = full colour one side, '1/0' = single-ink one side. We default
    to 4/4 or 4/0 because Justin's catalogue is full colour throughout.
    """
    explicit = specs.get("colors") or specs.get("colours")
    if explicit:
        return str(explicit)
    return "4/4" if double_sided else "4/0"


def pages_spec(specs: dict, double_sided: bool) -> str:
    """
    `pages` for booklets is the page count from cover-to-cover (incl. cover).
    For non-booklets, '2' if double-sided, '1' if single-sided.
    Returned as a string — PrintLogic stores it that way.
    """
    if specs.get("pages"):
        return str(specs["pages"])
    return "2" if double_sided else "1"


def finishing_description(specs: dict, double_sided: bool) -> str:
    """
    Human-readable line for `finishing_description`. Concatenates the
    finishes Craig collected (lamination, soft-touch, double-sided,
    binding for booklets, etc.).
    """
    bits: list[str] = []
    finish = (specs.get("finish") or "").strip()
    if finish and finish.lower() != "none":
        bits.append(finish)
    if specs.get("soft_touch"):
        bits.append("soft-touch laminate")
    bits.append("double-sided" if double_sided else "single-sided")
    if specs.get("rounded_corners"):
        bits.append("rounded corners")
    if specs.get("binding"):
        bits.append(str(specs["binding"]).replace("_", " "))
    if specs.get("cover_type"):
        bits.append(f"{specs['cover_type'].replace('_', ' ')} cover")
    return ", ".join(bits)


# ---------------------------------------------------------------------------
# Order-level helpers
# ---------------------------------------------------------------------------


def due_date(turnaround_days: int = 5, *, today: _dt.date | None = None) -> str:
    """
    Compute order_date_due in YYYY-MM-DD = today + N working days.
    Skips weekends — Sat/Sun aren't counted toward the turnaround.
    """
    today = today or _dt.date.today()
    d = today
    added = 0
    while added < turnaround_days:
        d += _dt.timedelta(days=1)
        if d.weekday() < 5:  # Mon-Fri
            added += 1
    return d.isoformat()


# ---------------------------------------------------------------------------
# Public payload builders
# ---------------------------------------------------------------------------


def build_payload_from_quote(
    quote,
    conv,
    *,
    turnaround_days: int = 5,
    customer_uid: str = "",
    delivery_address: dict[str, str] | None = None,
) -> dict[str, Any]:
    """
    Construct a full PrintLogic create_order body from a Craig Quote +
    its linked Conversation.

    Args:
      quote:            Quote ORM row (must have .product_key, .specs,
                        .final_price_ex_vat, .vat_amount, .id)
      conv:             Conversation ORM row (or None — customer fields
                        will be blank if missing)
      turnaround_days:  used to compute order_date_due (default 5 working)
      customer_uid:     PrintLogic customer_uid if we resolved an existing
                        one via find_customer (avoids duplicate customers)
      delivery_address: optional dict with keys
                        delivery_address1..4, delivery_postcode

    Returns the body dict ready for printlogic.create_order(payload, ...).
    """
    specs = quote.specs or {}
    qty = int(specs.get("quantity", 1)) if specs.get("quantity") else 1
    double_sided = bool(specs.get("double_sided"))

    cust_name = (getattr(conv, "customer_name", None) or "").strip()
    cust_email = (getattr(conv, "customer_email", None) or "").strip()
    cust_phone = (getattr(conv, "customer_phone", None) or "").strip()

    # Resolve VAT rate from the quote's stored amounts
    try:
        vat_rate_pct = round(
            (float(quote.vat_amount) / float(quote.final_price_ex_vat or 1)) * 100, 1
        )
    except (TypeError, ZeroDivisionError):
        vat_rate_pct = 23.0

    short = _short_item_desc(quote, qty)
    detail = _long_item_detail(quote, double_sided)
    w_mm, h_mm = width_height_mm(specs, quote.product_key)
    paper = paper_description_for(quote.product_key)
    finishing = finishing_description(specs, double_sided)
    size_text = finished_size_text(specs, quote.product_key)

    item: dict[str, Any] = {
        "item_quantity": str(qty),
        "item_desc": short,
        "item_detail": detail,
        "item_price": f"{float(quote.final_price_ex_vat or 0):.2f}",
        "item_vat": f"{vat_rate_pct}",
        "item_code": (quote.product_key or "")[:80],
        "item_part_number": "",
        # Rich fields PrintLogic supports — populated when we know them.
        "item_width_mm": w_mm,
        "item_height_mm": h_mm,
        "item_finished_size_text": size_text,
        "item_pages": pages_spec(specs, double_sided),
        "item_colors": colors_spec(specs, double_sided),
        "item_paper_description": paper,
        "item_finishing_description": finishing,
        # Trace back to Craig — keeps a forward-link from PrintLogic to the
        # quote, even though `item_custom_data` appears to be silently
        # dropped by the PrintLogic backend (see probe). Storing it here
        # is harmless (server ignores) and gives us a paper trail in our
        # own audit log.
        "item_custom_data": json.dumps({
            "craig_quote_id": quote.id,
            "craig_specs": specs,
        }),
    }

    delivery = delivery_address or {}

    payload: dict[str, Any] = {
        "customer_uid": customer_uid,
        "customer_name": cust_name or "Craig customer",
        "customer_email": cust_email,
        "customer_phone": cust_phone,
        "customer_address1": "",
        "customer_address2": "",
        "customer_address3": "",
        "customer_address4": "",
        "customer_postcode": "",
        "order_description": f"[CRAIG-PUSH qid={quote.id}] {short}",
        # NOTE: `contact_*` fields are how PrintLogic populates
        # `order_contact_email` / `order_contact_phone` on the order
        # itself (vs. customer_*, which only land on the customer record).
        # The probe confirmed customer_email did NOT appear at order
        # level — we fix that here.
        "contact_name": cust_name,
        "contact_email": cust_email,
        "contact_phone": cust_phone,
        "order_po": "",
        "order_date_due": due_date(turnaround_days),
        "delivery_address1": delivery.get("delivery_address1", ""),
        "delivery_address2": delivery.get("delivery_address2", ""),
        "delivery_address3": delivery.get("delivery_address3", ""),
        "delivery_address4": delivery.get("delivery_address4", ""),
        "delivery_postcode": delivery.get("delivery_postcode", ""),
        "order_items": [item],
    }
    return payload


def build_demo_payload(*, quote_id_marker: str | int = "DEMO") -> dict[str, Any]:
    """
    Payload for the dashboard "Create test order" button. Designed so the
    order in PrintLogic LOOKS like a real Craig push (rich fields, demo
    customer + sample product) — Justin can spot it via the marker and
    cancel it after the demo.

    The shape mirrors `build_payload_from_quote` so anyone debugging can
    trust both paths produce the same PrintLogic-side result.
    """
    import time
    ts = int(time.time())
    marker = f"[CRAIG-PROBE-DELETE-ME-{ts}]"

    # Demo product: 250 business cards, single-sided, soft-touch.
    # Picked because it exercises every rich field with confident values.
    qty = 250
    double_sided = False
    product_key = "business_cards"
    specs = {
        "quantity": qty,
        "finish": "soft-touch",
        "soft_touch": True,
        "double_sided": double_sided,
    }

    w_mm, h_mm = width_height_mm(specs, product_key)
    paper = paper_description_for(product_key)
    finishing = finishing_description(specs, double_sided)
    size_text = finished_size_text(specs, product_key)

    return {
        "customer_uid": "",
        "customer_name": f"CRAIG-PROBE-DO-NOT-PROCESS-{ts}",
        "customer_email": "probe@strategos-ai.com",
        "customer_phone": "+353 1 000 0000",
        "customer_address1": "",
        "customer_address2": "",
        "customer_address3": "",
        "customer_address4": "",
        "customer_postcode": "",
        "order_description": f"{marker} dashboard test order — DO NOT PRODUCE",
        "contact_name": f"CRAIG-PROBE-DO-NOT-PROCESS-{ts}",
        "contact_email": "probe@strategos-ai.com",
        "contact_phone": "+353 1 000 0000",
        "order_po": "",
        "order_date_due": due_date(5),
        "delivery_address1": "",
        "delivery_address2": "",
        "delivery_address3": "",
        "delivery_address4": "",
        "delivery_postcode": "",
        "order_items": [{
            "item_quantity": str(qty),
            "item_desc": f"[PROBE] {qty} business cards",
            # Multi-line jobsheet so Justin sees the rich spec when he
            # opens the order in PrintLogic — these facts don't land in
            # the dedicated columns (silently dropped) but DO land here.
            "item_detail": (
                f"{marker} Dashboard test order — DO NOT PRODUCE\n"
                f"Paper:       {paper}\n"
                f"Size:        {size_text}\n"
                f"Pages:       {pages_spec(specs, double_sided)} (SS, "
                f"{colors_spec(specs, double_sided)})\n"
                f"Finishing:   {finishing}\n"
                f"Quote:       Craig demo (dashboard button)"
            ),
            "item_price": "0.01",
            "item_vat": "23",
            "item_code": product_key,
            "item_part_number": "",
            "item_width_mm": w_mm,
            "item_height_mm": h_mm,
            "item_finished_size_text": size_text,
            "item_pages": pages_spec(specs, double_sided),
            "item_colors": colors_spec(specs, double_sided),
            "item_paper_description": paper,
            "item_finishing_description": finishing,
            "item_custom_data": json.dumps({
                "craig_probe": True,
                "ts": ts,
                "source": "dashboard",
                "marker": marker,
                "quote_id": quote_id_marker,
            }),
        }],
        # Pass marker out separately so the caller can persist it for cancel
        "_marker": marker,
        "_ts": ts,
    }


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _short_item_desc(quote, qty: int) -> str:
    """Short, human line for item_desc / order_description."""
    specs = quote.specs or {}
    parts: list[str] = [str(qty)]
    if quote.product_key:
        parts.append(quote.product_key.replace("_", " "))
    if specs.get("finish"):
        parts.append(specs["finish"])
    parts.append("DS" if specs.get("double_sided") else "SS")
    return " ".join(parts) if parts else "Craig quote"


def _long_item_detail(quote, double_sided: bool) -> str:
    """
    Build the multi-line jobsheet that lands in PrintLogic's `detail`
    column. We pack EVERYTHING here because the rich per-item columns
    (width_mm, paper_description, etc.) are silently dropped by
    create_order — `detail` is the only field that survives and that
    Justin actually reads when he opens the order.

    Format (one fact per line):
        Paper:        400gsm silk
        Size:         85 x 55 mm (Business card)
        Pages:        2 (DS, 4/4)
        Finishing:    soft-touch laminate, rounded corners
        Cover:        soft cover
        Binding:      saddle stitch
        Quote:        Craig qid=42
    """
    specs = quote.specs or {}
    lines: list[str] = []

    paper = paper_description_for(quote.product_key)
    if paper:
        lines.append(f"Paper:       {paper}")

    size_text = finished_size_text(specs, quote.product_key)
    if size_text:
        lines.append(f"Size:        {size_text}")

    pages = pages_spec(specs, double_sided)
    colors = colors_spec(specs, double_sided)
    sides = "DS" if double_sided else "SS"
    lines.append(f"Pages:       {pages} ({sides}, {colors})")

    finishing = finishing_description(specs, double_sided)
    if finishing:
        lines.append(f"Finishing:   {finishing}")

    if specs.get("cover_type"):
        lines.append(f"Cover:       {str(specs['cover_type']).replace('_', ' ')}")
    if specs.get("binding"):
        lines.append(f"Binding:     {str(specs['binding']).replace('_', ' ')}")

    if getattr(quote, "id", None):
        lines.append(f"Quote:       Craig qid={quote.id}")

    return "\n".join(lines)
