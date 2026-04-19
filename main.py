"""
Craig Pricing Microservice — Just Print
FastAPI service that receives product specs and returns accurate quotes.

Rules:
- NEVER invent a price. If it's not on the sheet, escalate.
- NEVER guess a quantity tier. If qty doesn't match, escalate.
- All prices are retail (quote directly to customer).
- All prices exclude VAT.
"""

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field, field_validator
from typing import Optional, Literal
from enum import Enum

from pricing_data import (
    SMALL_FORMAT,
    LARGE_FORMAT,
    BOOKLETS,
    SURCHARGES,
    ARTWORK_RATE_EUR,
    VAT_RATE,
    STANDARD_TURNAROUND,
    POA_ITEMS,
)

app = FastAPI(
    title="Craig Pricing Service",
    description="Quoting engine for Just Print. Returns accurate prices from Justin's pricing sheets.",
    version="1.0.0",
)


# =============================================================================
# ENUMS & MODELS
# =============================================================================


class ProductCategory(str, Enum):
    small_format = "small_format"
    large_format = "large_format"
    booklet = "booklet"


class SmallFormatProduct(str, Enum):
    business_cards = "business_cards"
    flyers_a6 = "flyers_a6"
    flyers_a5 = "flyers_a5"
    flyers_a4 = "flyers_a4"
    flyers_dl = "flyers_dl"
    brochures_a4 = "brochures_a4"
    compliment_slips = "compliment_slips"
    letterheads = "letterheads"
    ncr_pads_a5 = "ncr_pads_a5"
    ncr_pads_a4 = "ncr_pads_a4"


class LargeFormatProduct(str, Enum):
    roller_banners = "roller_banners"
    foamex_boards = "foamex_boards"
    dibond_boards = "dibond_boards"
    corri_boards = "corri_boards"
    pvc_banners = "pvc_banners"
    canvas_prints = "canvas_prints"
    window_graphics = "window_graphics"
    floor_graphics = "floor_graphics"
    mesh_banners = "mesh_banners"
    fabric_displays = "fabric_displays"
    vehicle_magnetics = "vehicle_magnetics"
    vinyl_labels = "vinyl_labels"


class BookletFormat(str, Enum):
    a5 = "a5"
    a4 = "a4"


class BookletBinding(str, Enum):
    saddle_stitch = "saddle_stitch"
    perfect_bound = "perfect_bound"


class BookletCoverType(str, Enum):
    self_cover = "self_cover"
    card_cover = "card_cover"
    card_cover_lam = "card_cover_lam"


# --- Request Models ---


class SmallFormatRequest(BaseModel):
    product: SmallFormatProduct
    quantity: int = Field(..., gt=0, description="Number of items (must match a tier on the pricing sheet)")
    double_sided: bool = Field(False, description="True if double-sided printing is needed")
    finish: Optional[str] = Field(None, description="Finish option: gloss, matte, soft-touch, uncoated, duplicate, triplicate")
    needs_artwork: bool = Field(False, description="True if customer needs design work")
    artwork_hours: Optional[float] = Field(None, ge=0, description="Estimated artwork hours (if needs_artwork=True)")

    @field_validator("finish")
    @classmethod
    def normalize_finish(cls, v: Optional[str]) -> Optional[str]:
        if v is not None:
            return v.strip().lower().replace("-", "_").replace(" ", "_")
        return v


class LargeFormatRequest(BaseModel):
    product: LargeFormatProduct
    quantity: int = Field(..., gt=0, description="Number of units or sq/m")
    needs_artwork: bool = Field(False, description="True if customer needs design work")
    artwork_hours: Optional[float] = Field(None, ge=0, description="Estimated artwork hours")


class BookletRequest(BaseModel):
    format: BookletFormat = Field(..., description="A5 or A4")
    binding: BookletBinding = Field(..., description="Saddle stitch or perfect bound")
    pages: int = Field(..., gt=0, description="Number of pages (must be a multiple of 4)")
    cover_type: BookletCoverType = Field(..., description="self_cover, card_cover, or card_cover_lam")
    quantity: int = Field(..., gt=0, description="Must match a tier: 25, 50, 100, 250, or 500")
    needs_artwork: bool = Field(False, description="True if customer needs design work")
    artwork_hours: Optional[float] = Field(None, ge=0, description="Estimated artwork hours")


# --- Response Model ---


class QuoteResponse(BaseModel):
    success: bool
    product_name: str
    category: str
    quantity: int
    quantity_unit: str
    base_price: float
    surcharges_applied: list[str]
    surcharge_amount: float
    final_price_ex_vat: float
    vat_amount: float
    final_price_inc_vat: float
    artwork_cost_ex_vat: Optional[float] = None
    artwork_cost_inc_vat: Optional[float] = None
    total_inc_everything: float
    turnaround: str
    notes: list[str]
    pricing_unit: str


class EscalationResponse(BaseModel):
    success: bool = False
    escalate: bool = True
    reason: str
    product_name: Optional[str] = None
    message: str = "This needs to be quoted by Justin directly."


class ProductListResponse(BaseModel):
    category: str
    products: list[dict]


# =============================================================================
# PRICING ENGINE
# =============================================================================


def calculate_small_format(req: SmallFormatRequest) -> QuoteResponse | EscalationResponse:
    """Look up small format price and apply surcharges. Never invent."""

    product_data = SMALL_FORMAT.get(req.product.value)
    if not product_data:
        return EscalationResponse(
            reason=f"Product '{req.product.value}' not found in pricing sheet.",
            product_name=req.product.value,
        )

    # Check if quantity exists on the sheet
    if req.quantity not in product_data["prices"]:
        available = sorted(product_data["prices"].keys())
        return EscalationResponse(
            reason=f"Quantity {req.quantity} is not on the pricing sheet.",
            product_name=product_data["name"],
            message=f"Available quantities: {available}. For {req.quantity}, Justin needs to quote directly.",
        )

    base_price = product_data["prices"][req.quantity]
    surcharges_applied = []
    multiplier = 1.0

    # Double-sided surcharge
    if req.double_sided and product_data.get("double_sided_surcharge", False):
        multiplier *= (1 + SURCHARGES["double_sided"])
        surcharges_applied.append(f"Double-sided: +{int(SURCHARGES['double_sided'] * 100)}%")

    # Finish surcharges
    # NOTE: Soft-touch is a surcharge that applies to ANY product (Justin confirmed Apr 10).
    # It is NOT limited to products that list it as a finish option on the sheet.
    # Triplicate is NCR-only.
    if req.finish:
        finish = req.finish.lower().replace("-", "_").replace(" ", "_")

        if finish == "soft_touch":
            # Soft-touch can be applied to any product — +25% across the board
            multiplier *= (1 + SURCHARGES["soft_touch"])
            surcharges_applied.append(f"Soft-touch finish: +{int(SURCHARGES['soft_touch'] * 100)}%")

        elif finish == "triplicate":
            if req.product.value in ("ncr_pads_a5", "ncr_pads_a4"):
                multiplier *= (1 + SURCHARGES["triplicate"])
                surcharges_applied.append(f"Triplicate: +{int(SURCHARGES['triplicate'] * 100)}%")
            else:
                return EscalationResponse(
                    reason="Triplicate finish only applies to NCR pads.",
                    product_name=product_data["name"],
                )

        else:
            # For non-surcharge finishes, validate against product's listed options
            valid_finishes = [f.lower().replace("-", "_").replace(" ", "_") for f in product_data["finishes"]]
            if finish not in valid_finishes:
                return EscalationResponse(
                    reason=f"Finish '{req.finish}' is not available for {product_data['name']}.",
                    product_name=product_data["name"],
                    message=f"Available finishes: {product_data['finishes']}. Escalate to Justin if customer needs something else.",
                )

    final_price = round(base_price * multiplier, 2)
    surcharge_amount = round(final_price - base_price, 2)
    vat = round(final_price * VAT_RATE, 2)

    # Artwork
    artwork_ex = None
    artwork_inc = None
    total = round(final_price + vat, 2)

    if req.needs_artwork and req.artwork_hours and req.artwork_hours > 0:
        artwork_ex = round(ARTWORK_RATE_EUR * req.artwork_hours, 2)
        artwork_inc = round(artwork_ex * (1 + VAT_RATE), 2)
        total = round(total + artwork_inc, 2)

    notes = []
    if product_data.get("notes"):
        notes.append(product_data["notes"])

    return QuoteResponse(
        success=True,
        product_name=product_data["name"],
        category="small_format",
        quantity=req.quantity,
        quantity_unit=product_data["price_per"],
        base_price=base_price,
        surcharges_applied=surcharges_applied,
        surcharge_amount=surcharge_amount,
        final_price_ex_vat=final_price,
        vat_amount=vat,
        final_price_inc_vat=round(final_price + vat, 2),
        artwork_cost_ex_vat=artwork_ex,
        artwork_cost_inc_vat=artwork_inc,
        total_inc_everything=total,
        turnaround=STANDARD_TURNAROUND,
        notes=notes,
        pricing_unit=product_data["price_per"],
    )


def calculate_large_format(req: LargeFormatRequest) -> QuoteResponse | EscalationResponse:
    """Look up large format price. Unit or bulk pricing."""

    product_data = LARGE_FORMAT.get(req.product.value)
    if not product_data:
        return EscalationResponse(
            reason=f"Product '{req.product.value}' not found in pricing sheet.",
            product_name=req.product.value,
        )

    if req.quantity < product_data["min_qty"]:
        return EscalationResponse(
            reason=f"Minimum quantity for {product_data['name']} is {product_data['min_qty']}.",
            product_name=product_data["name"],
        )

    # Determine unit price (bulk vs standard)
    if req.quantity >= product_data["bulk_threshold"]:
        unit_price = product_data["bulk_price"]
        applied = [f"Bulk pricing applied ({req.quantity} >= {product_data['bulk_threshold']})"]
    else:
        unit_price = product_data["unit_price"]
        applied = []

    total_price = round(unit_price * req.quantity, 2)
    surcharge_amount = 0.0  # Large format has no surcharges — just unit/bulk
    vat = round(total_price * VAT_RATE, 2)

    artwork_ex = None
    artwork_inc = None
    grand_total = round(total_price + vat, 2)

    if req.needs_artwork and req.artwork_hours and req.artwork_hours > 0:
        artwork_ex = round(ARTWORK_RATE_EUR * req.artwork_hours, 2)
        artwork_inc = round(artwork_ex * (1 + VAT_RATE), 2)
        grand_total = round(grand_total + artwork_inc, 2)

    notes = []
    if product_data.get("notes"):
        notes.append(product_data["notes"])

    # Flag POA sub-items
    poa_keywords = ["installation", "die-cut", "frame hardware"]
    for kw in poa_keywords:
        if kw in product_data.get("notes", "").lower():
            notes.append(f"NOTE: {kw.title()} is POA — escalate to Justin if requested.")

    return QuoteResponse(
        success=True,
        product_name=product_data["name"],
        category="large_format",
        quantity=req.quantity,
        quantity_unit=product_data["pricing_unit"],
        base_price=unit_price,
        surcharges_applied=applied,
        surcharge_amount=surcharge_amount,
        final_price_ex_vat=total_price,
        vat_amount=vat,
        final_price_inc_vat=round(total_price + vat, 2),
        artwork_cost_ex_vat=artwork_ex,
        artwork_cost_inc_vat=artwork_inc,
        total_inc_everything=grand_total,
        turnaround=STANDARD_TURNAROUND,
        notes=notes,
        pricing_unit=product_data["pricing_unit"],
    )


def calculate_booklet(req: BookletRequest) -> QuoteResponse | EscalationResponse:
    """Look up booklet price by format, binding, pages, cover type, qty."""

    format_data = BOOKLETS.get(req.format.value)
    if not format_data:
        return EscalationResponse(
            reason=f"Booklet format '{req.format.value}' not found.",
        )

    binding_data = format_data.get(req.binding.value)
    if not binding_data:
        return EscalationResponse(
            reason=f"Binding type '{req.binding.value}' not available for {req.format.value.upper()} booklets.",
        )

    pages_data = binding_data.get(req.pages)
    if not pages_data:
        available_pages = sorted(binding_data.keys())
        return EscalationResponse(
            reason=f"{req.pages}pp is not on the pricing sheet for {req.format.value.upper()} {req.binding.value.replace('_', ' ')}.",
            message=f"Available page counts: {available_pages}. Justin needs to quote {req.pages}pp directly.",
        )

    cover_data = pages_data.get(req.cover_type.value)
    if not cover_data:
        available_covers = list(pages_data.keys())
        return EscalationResponse(
            reason=f"Cover type '{req.cover_type.value}' not available for {req.pages}pp {req.format.value.upper()} {req.binding.value.replace('_', ' ')}.",
            message=f"Available cover types: {available_covers}.",
        )

    if req.quantity not in cover_data:
        available_qtys = sorted(cover_data.keys())
        return EscalationResponse(
            reason=f"Quantity {req.quantity} is not on the pricing sheet for this booklet.",
            message=f"Available quantities: {available_qtys}. Justin needs to quote {req.quantity} directly.",
        )

    base_price = float(cover_data[req.quantity])
    vat = round(base_price * VAT_RATE, 2)

    artwork_ex = None
    artwork_inc = None
    grand_total = round(base_price + vat, 2)

    if req.needs_artwork and req.artwork_hours and req.artwork_hours > 0:
        artwork_ex = round(ARTWORK_RATE_EUR * req.artwork_hours, 2)
        artwork_inc = round(artwork_ex * (1 + VAT_RATE), 2)
        grand_total = round(grand_total + artwork_inc, 2)

    # Build descriptive name
    cover_labels = {
        "self_cover": "Self Cover (150gsm silk)",
        "card_cover": "Card Cover (300gsm/150gsm silk)",
        "card_cover_lam": "Card Cover + Matt/Gloss Lam",
    }
    product_name = (
        f"Booklet {req.format.value.upper()} — {req.pages}pp — "
        f"{req.binding.value.replace('_', ' ').title()} — "
        f"{cover_labels.get(req.cover_type.value, req.cover_type.value)}"
    )

    notes = []
    if req.binding == BookletBinding.saddle_stitch and req.pages > 48:
        notes.append("WARNING: Saddle stitch is typically suitable up to 48pp.")
    if req.binding == BookletBinding.perfect_bound and req.pages < 24:
        notes.append("WARNING: Perfect bound typically starts from 24pp.")
    if req.quantity > 500:
        notes.append("For quantities above 500, contact Justin for a formal quotation.")

    return QuoteResponse(
        success=True,
        product_name=product_name,
        category="booklet",
        quantity=req.quantity,
        quantity_unit="copies",
        base_price=base_price,
        surcharges_applied=[],
        surcharge_amount=0.0,
        final_price_ex_vat=base_price,
        vat_amount=vat,
        final_price_inc_vat=round(base_price + vat, 2),
        artwork_cost_ex_vat=artwork_ex,
        artwork_cost_inc_vat=artwork_inc,
        total_inc_everything=grand_total,
        turnaround=STANDARD_TURNAROUND,
        notes=notes,
        pricing_unit="per job",
    )


# =============================================================================
# API ENDPOINTS
# =============================================================================


@app.get("/", tags=["Health"])
def root():
    return {
        "service": "Craig Pricing Service",
        "version": "1.0.0",
        "status": "running",
        "description": "Quoting engine for Just Print. Send product specs, get accurate prices.",
    }


@app.get("/products", tags=["Catalog"], response_model=list[ProductListResponse])
def list_products():
    """List all available products by category."""
    result = []

    # Small format
    sf_products = []
    for key, data in SMALL_FORMAT.items():
        sf_products.append({
            "key": key,
            "name": data["name"],
            "sizes": data["sizes"],
            "finishes": data["finishes"],
            "available_quantities": sorted(data["prices"].keys()),
            "price_per": data["price_per"],
        })
    result.append(ProductListResponse(category="small_format", products=sf_products))

    # Large format
    lf_products = []
    for key, data in LARGE_FORMAT.items():
        lf_products.append({
            "key": key,
            "name": data["name"],
            "sizes": data["sizes"],
            "unit_price": data["unit_price"],
            "bulk_price": data["bulk_price"],
            "bulk_threshold": data["bulk_threshold"],
            "pricing_unit": data["pricing_unit"],
            "min_qty": data["min_qty"],
        })
    result.append(ProductListResponse(category="large_format", products=lf_products))

    # Booklets
    booklet_products = []
    for fmt in ["a5", "a4"]:
        for binding in BOOKLETS[fmt]:
            pages_available = sorted(BOOKLETS[fmt][binding].keys())
            for pages in pages_available:
                covers = list(BOOKLETS[fmt][binding][pages].keys())
                qtys = sorted(list(BOOKLETS[fmt][binding][pages][covers[0]].keys()))
                booklet_products.append({
                    "format": fmt.upper(),
                    "binding": binding.replace("_", " ").title(),
                    "pages": pages,
                    "cover_types": covers,
                    "available_quantities": qtys,
                })
    result.append(ProductListResponse(category="booklet", products=booklet_products))

    return result


@app.post("/quote/small-format", tags=["Quoting"])
def quote_small_format(req: SmallFormatRequest) -> QuoteResponse | EscalationResponse:
    """Get a quote for a small format product (business cards, flyers, etc.)."""
    return calculate_small_format(req)


@app.post("/quote/large-format", tags=["Quoting"])
def quote_large_format(req: LargeFormatRequest) -> QuoteResponse | EscalationResponse:
    """Get a quote for a large format product (banners, boards, signage, etc.)."""
    return calculate_large_format(req)


@app.post("/quote/booklet", tags=["Quoting"])
def quote_booklet(req: BookletRequest) -> QuoteResponse | EscalationResponse:
    """Get a quote for a booklet (saddle stitch or perfect bound)."""
    return calculate_booklet(req)


@app.get("/artwork-rate", tags=["Info"])
def artwork_rate():
    """Return the current artwork/design rate."""
    return {
        "rate_per_hour_ex_vat": ARTWORK_RATE_EUR,
        "vat_rate": VAT_RATE,
        "rate_per_hour_inc_vat": round(ARTWORK_RATE_EUR * (1 + VAT_RATE), 2),
        "note": "Artwork is charged separately from printing.",
    }


@app.get("/turnaround", tags=["Info"])
def turnaround():
    """Return standard turnaround time."""
    return {
        "standard": STANDARD_TURNAROUND,
        "rush": "Escalate to Justin — rush jobs quoted separately.",
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
