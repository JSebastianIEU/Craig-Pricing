"""
Pricing Engine — pure business logic.
Reads from SQLite, applies surcharge rules, returns structured quote or escalation.

The LLM NEVER talks to this module directly — it goes through FastAPI endpoints.
This module contains no HTTP concerns, just math and lookups.
"""

import json
from dataclasses import dataclass, asdict, field
from typing import Optional
from sqlalchemy.orm import Session

from db.models import (
    Product, PriceTier, SurchargeRule, Setting,
    TaxRate, CategoryTaxMap, DEFAULT_ORG_SLUG,
)


# =============================================================================
# DATA CLASSES (plain, no Pydantic — easier to reuse across API + LLM tool calls)
# =============================================================================


@dataclass
class QuoteResult:
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
    artwork_cost_ex_vat: Optional[float]
    artwork_cost_inc_vat: Optional[float]
    total_inc_everything: float
    turnaround: str
    notes: list[str]
    pricing_unit: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class EscalationResult:
    success: bool = False
    escalate: bool = True
    reason: str = ""
    product_name: Optional[str] = None
    message: str = "This needs to be quoted by Justin directly."

    def to_dict(self) -> dict:
        return asdict(self)


# =============================================================================
# SETTINGS HELPERS
# =============================================================================


def _get_setting(
    db: Session,
    key: str,
    default=None,
    organization_slug: str = DEFAULT_ORG_SLUG,
):
    """Read a setting from the DB, scoped to a tenant, parsed to its declared type."""
    row = (
        db.query(Setting)
        .filter_by(organization_slug=organization_slug, key=key)
        .first()
    )
    if row is None:
        return default
    # Decrypt if needed. Non-secret rows pass through unchanged (no `enc::v1::`
    # prefix → no-op). Secret rows get decrypted before any type cast below.
    from secrets_crypto import decrypt
    raw = decrypt(row.value)
    if row.value_type == "float":
        return float(raw)
    if row.value_type == "int":
        return int(raw)
    if row.value_type == "json":
        return json.loads(raw)
    return raw


def _get_surcharge(
    db: Session,
    name: str,
    organization_slug: str = DEFAULT_ORG_SLUG,
) -> float:
    """
    Return the amount for a named surcharge.

    Kept for backwards compat — callers that don't care about the kind
    just get the raw multiplier/amount. New code should prefer
    `_get_surcharge_rule` which returns (amount, kind) so the caller can
    apply it correctly:

        - kind='multiplier' → price_after = price_before * (1 + amount)
        - kind='additive'   → price_after = price_before + amount (per job)
    """
    rule = (
        db.query(SurchargeRule)
        .filter_by(organization_slug=organization_slug, name=name)
        .first()
    )
    return rule.multiplier if rule else 0.0


def _get_client_multiplier(
    db: Session,
    organization_slug: str = DEFAULT_ORG_SLUG,
) -> float:
    """
    Return the tenant's client multiplier (default 1.0 = no adjustment).

    This is the "base price + per-client percentage multiplier" Roi
    asked for: a single tenant-wide scalar applied AFTER surcharges and
    BEFORE VAT. Used so Justin can mark up (or down) prices per client
    without editing every product tier.

    Stored as a string in the `settings` table under key
    `pricing_client_multiplier`. Parses to a float; silently falls back
    to 1.0 on any parse error so a typo in the dashboard never breaks
    a live quote.
    """
    # Read the raw string directly — bypass `_get_setting`'s value_type
    # cast, because a dashboard typo ("not-a-number" on a value_type=float
    # row) would raise inside float() before we ever saw it.
    row = (
        db.query(Setting)
        .filter_by(organization_slug=organization_slug, key="pricing_client_multiplier")
        .first()
    )
    if row is None:
        return 1.0
    try:
        mult = float(row.value)
    except (TypeError, ValueError):
        return 1.0
    # Sanity clamp — reject negative or absurdly large multipliers outright
    # rather than scaling prices to zero or infinity.
    if mult <= 0 or mult > 10.0:
        return 1.0
    return mult


def _get_surcharge_rule(
    db: Session,
    name: str,
    organization_slug: str = DEFAULT_ORG_SLUG,
) -> tuple[float, str]:
    """
    Return `(amount, kind)` for a named surcharge. Kind is either
    'multiplier' (fraction, e.g. 0.20 for +20%) or 'additive' (flat
    euro amount added once per job, e.g. 15.0 for +€15). Returns
    `(0.0, 'multiplier')` when the rule isn't configured for this tenant
    — i.e. applying a zero multiplier is a no-op.
    """
    rule = (
        db.query(SurchargeRule)
        .filter_by(organization_slug=organization_slug, name=name)
        .first()
    )
    if not rule:
        return (0.0, "multiplier")
    kind = (rule.kind or "multiplier").strip().lower()
    if kind not in ("multiplier", "additive"):
        kind = "multiplier"
    return (float(rule.multiplier or 0.0), kind)


def _parse_unit_base(price_per: str) -> int:
    """Extract the unit base from price_per string. '100 cards' → 100, '5 pads' → 5."""
    if not price_per:
        return 1
    parts = price_per.strip().split()
    try:
        return int(parts[0])
    except (ValueError, IndexError):
        return 1


# Fallback constants if a tenant has no tax_rates seeded yet
_STANDARD_VAT_RATE = 0.23
_REDUCED_VAT_RATE = 0.135


def _get_vat_rate_for_category(
    db: Session, category: str, organization_slug: str = DEFAULT_ORG_SLUG,
) -> float:
    """
    Look up the VAT rate for a product category, scoped to the tenant.

    Resolution order:
      1) category_tax_map row → tax_rates.rate
      2) the tenant's default tax rate (is_default=true)
      3) standard VAT fallback
    """
    mapping = (
        db.query(CategoryTaxMap)
        .filter_by(organization_slug=organization_slug, category=category)
        .first()
    )
    if mapping and mapping.tax_rate:
        return mapping.tax_rate.rate

    default = (
        db.query(TaxRate)
        .filter_by(organization_slug=organization_slug, is_default=True)
        .first()
    )
    if default:
        return default.rate

    return _STANDARD_VAT_RATE


def _get_vat_rate_for_product(
    db: Session, product: Product, organization_slug: str = DEFAULT_ORG_SLUG,
) -> float:
    """Convenience: VAT rate for a Product instance."""
    return _get_vat_rate_for_category(db, product.category, organization_slug)


# =============================================================================
# SMALL FORMAT
# =============================================================================


def quote_small_format(
    db: Session,
    product_key: str,
    quantity: int,
    double_sided: bool = False,
    finish: Optional[str] = None,
    needs_artwork: bool = False,
    artwork_hours: float = 0.0,
    organization_slug: str = DEFAULT_ORG_SLUG,
) -> QuoteResult | EscalationResult:
    """Look up a small-format price and apply surcharges (scoped per tenant)."""

    product = (
        db.query(Product)
        .filter_by(
            organization_slug=organization_slug,
            key=product_key,
            category="small_format",
        )
        .first()
    )
    if product is None:
        return EscalationResult(
            reason=f"Product '{product_key}' not found in small-format catalog.",
            product_name=product_key,
        )

    tier = db.query(PriceTier).filter_by(
        product_id=product.id, spec_key="", quantity=quantity,
    ).first()
    if tier is None:
        available = sorted(
            t.quantity for t in db.query(PriceTier).filter_by(product_id=product.id, spec_key="").all()
        )
        return EscalationResult(
            reason=f"Quantity {quantity} is not on the pricing sheet for {product.name}.",
            product_name=product.name,
            message=f"Available quantities: {available}. Justin needs to quote {quantity} directly.",
        )

    # Price on sheet is per unit base (e.g. per 100 cards, per 5 pads)
    unit_price = tier.price
    unit_base = _parse_unit_base(product.price_per)
    qty_multiplier = quantity / unit_base
    base_price = round(unit_price * qty_multiplier, 2)

    surcharges_applied: list[str] = []
    multiplier = 1.0   # collects all multiplier-kind surcharges (e.g. +20%)
    additive = 0.0     # collects all additive-kind surcharges (e.g. +€15 flat)

    def _apply(name: str, label_pct: str, label_flat: str) -> None:
        """Look up a surcharge by name and fold it into the right accumulator
        based on its kind. Centralised so every surcharge (double_sided,
        soft_touch, triplicate, ...) shares the same branch logic."""
        nonlocal multiplier, additive
        amount, kind = _get_surcharge_rule(db, name, organization_slug=organization_slug)
        if amount == 0.0:
            return
        if kind == "additive":
            additive += amount
            surcharges_applied.append(label_flat.format(amount=amount))
        else:
            multiplier *= (1 + amount)
            surcharges_applied.append(label_pct.format(pct=int(amount * 100)))

    # Double-sided surcharge (skipped for products flagged as no-surcharge, e.g. business cards)
    if double_sided and product.double_sided_surcharge:
        _apply(
            "double_sided",
            label_pct="Double-sided: +{pct}%",
            label_flat="Double-sided: +\u20ac{amount:.2f}",
        )

    # Finish surcharges (soft-touch, triplicate)
    if finish:
        normalized = finish.strip().lower().replace("-", "_").replace(" ", "_")

        if normalized == "soft_touch":
            _apply(
                "soft_touch",
                label_pct="Soft-touch finish: +{pct}%",
                label_flat="Soft-touch finish: +\u20ac{amount:.2f}",
            )

        elif normalized == "triplicate":
            if product.key in ("ncr_pads_a5", "ncr_pads_a4"):
                _apply(
                    "triplicate",
                    label_pct="Triplicate: +{pct}%",
                    label_flat="Triplicate: +\u20ac{amount:.2f}",
                )
            else:
                return EscalationResult(
                    reason="Triplicate finish only applies to NCR pads.",
                    product_name=product.name,
                )

        else:
            # Validate against product's listed finishes.
            #
            # Special case: if the product has NO finishes configured at all
            # (`finishes` is null / empty list), the LLM should not have
            # passed a finish in the first place — but DeepSeek often
            # auto-fills `finish="uncoated"` for small_format products as a
            # default. Treat that as a no-op rather than escalating: the
            # product simply has no finish dimension to apply, so any finish
            # the LLM names is irrelevant. This keeps "spec-less" sentinel
            # products (like `secretest` for demos) quotable end-to-end.
            valid = [f.lower().replace("-", "_").replace(" ", "_") for f in (product.finishes or [])]
            if not valid:
                # No finishes configured — silently ignore whatever was passed
                pass
            elif normalized not in valid:
                return EscalationResult(
                    reason=f"Finish '{finish}' is not available for {product.name}.",
                    product_name=product.name,
                    message=f"Available finishes: {product.finishes}. Escalate if the customer needs something else.",
                )

    # Apply multiplier first, then add the flat adjustments. The order
    # matters because additive surcharges are fixed euros — they should
    # NOT scale with the multiplier (otherwise €15 flat on a double-sided
    # job becomes €18).
    final_ex = round(base_price * multiplier + additive, 2)

    # Client multiplier: tenant-wide markup applied AFTER surcharges,
    # BEFORE VAT. Default 1.0 = no-op. Roi + Justin's pricing lever.
    client_mult = _get_client_multiplier(db, organization_slug=organization_slug)
    if abs(client_mult - 1.0) > 1e-6:
        final_ex = round(final_ex * client_mult, 2)
        pct = int(round((client_mult - 1.0) * 100))
        sign = "+" if pct >= 0 else ""
        surcharges_applied.append(f"Client adjustment: {sign}{pct}%")

    surcharge_amount = round(final_ex - base_price, 2)

    vat_rate = _get_vat_rate_for_product(db, product, organization_slug)
    vat = round(final_ex * vat_rate, 2)

    # Artwork (always standard VAT rate — it's a service, not printed matter)
    artwork_ex = None
    artwork_inc = None
    total = round(final_ex + vat, 2)
    if needs_artwork and artwork_hours > 0:
        artwork_rate = _get_setting(db, "artwork_rate_eur", 65.0, organization_slug=organization_slug)
        artwork_ex = round(artwork_rate * artwork_hours, 2)
        artwork_inc = round(artwork_ex * (1 + _STANDARD_VAT_RATE), 2)
        total = round(total + artwork_inc, 2)

    turnaround = _get_setting(db, "standard_turnaround", "3-5 working days", organization_slug=organization_slug)
    notes = [product.notes] if product.notes else []

    return QuoteResult(
        success=True,
        product_name=product.name,
        category="small_format",
        quantity=quantity,
        quantity_unit=product.price_per or "",
        base_price=base_price,
        surcharges_applied=surcharges_applied,
        surcharge_amount=surcharge_amount,
        final_price_ex_vat=final_ex,
        vat_amount=vat,
        final_price_inc_vat=round(final_ex + vat, 2),
        artwork_cost_ex_vat=artwork_ex,
        artwork_cost_inc_vat=artwork_inc,
        total_inc_everything=total,
        turnaround=turnaround,
        notes=notes,
        pricing_unit=product.price_per or "",
    )


# =============================================================================
# LARGE FORMAT
# =============================================================================


def quote_large_format(
    db: Session,
    product_key: str,
    quantity: int,
    needs_artwork: bool = False,
    artwork_hours: float = 0.0,
    organization_slug: str = DEFAULT_ORG_SLUG,
) -> QuoteResult | EscalationResult:
    """Look up a large-format price (scoped per tenant). Applies unit or bulk pricing based on quantity."""

    product = (
        db.query(Product)
        .filter_by(
            organization_slug=organization_slug,
            key=product_key,
            category="large_format",
        )
        .first()
    )
    if product is None:
        return EscalationResult(
            reason=f"Product '{product_key}' not found in large-format catalog.",
            product_name=product_key,
        )

    if quantity < (product.min_qty or 1):
        return EscalationResult(
            reason=f"Minimum quantity for {product.name} is {product.min_qty}.",
            product_name=product.name,
        )

    if quantity >= (product.bulk_threshold or 1):
        unit_price = product.bulk_price
        applied = [f"Bulk pricing applied ({quantity} >= {product.bulk_threshold})"]
    else:
        unit_price = product.unit_price
        applied = []

    total_ex = round(unit_price * quantity, 2)

    # Client multiplier — applied AFTER surcharges, BEFORE VAT. See
    # `_get_client_multiplier` for the full rationale.
    client_mult = _get_client_multiplier(db, organization_slug=organization_slug)
    if abs(client_mult - 1.0) > 1e-6:
        total_ex = round(total_ex * client_mult, 2)
        pct = int(round((client_mult - 1.0) * 100))
        sign = "+" if pct >= 0 else ""
        applied.append(f"Client adjustment: {sign}{pct}%")

    vat_rate = _get_vat_rate_for_product(db, product, organization_slug)
    vat = round(total_ex * vat_rate, 2)

    artwork_ex = None
    artwork_inc = None
    total = round(total_ex + vat, 2)
    if needs_artwork and artwork_hours > 0:
        artwork_rate = _get_setting(db, "artwork_rate_eur", 65.0, organization_slug=organization_slug)
        artwork_ex = round(artwork_rate * artwork_hours, 2)
        # Artwork = service → standard rate
        artwork_inc = round(artwork_ex * (1 + _STANDARD_VAT_RATE), 2)
        total = round(total + artwork_inc, 2)

    turnaround = _get_setting(db, "standard_turnaround", "3-5 working days", organization_slug=organization_slug)
    notes = [product.notes] if product.notes else []

    return QuoteResult(
        success=True,
        product_name=product.name,
        category="large_format",
        quantity=quantity,
        quantity_unit=product.pricing_unit or "",
        base_price=unit_price,
        surcharges_applied=applied,
        surcharge_amount=0.0,
        final_price_ex_vat=total_ex,
        vat_amount=vat,
        final_price_inc_vat=round(total_ex + vat, 2),
        artwork_cost_ex_vat=artwork_ex,
        artwork_cost_inc_vat=artwork_inc,
        total_inc_everything=total,
        turnaround=turnaround,
        notes=notes,
        pricing_unit=product.pricing_unit or "",
    )


# =============================================================================
# BOOKLET
# =============================================================================


def quote_booklet(
    db: Session,
    format: str,              # 'a5' | 'a4'
    binding: str,             # 'saddle_stitch' | 'perfect_bound'
    pages: int,
    cover_type: str,          # 'self_cover' | 'card_cover' | 'card_cover_lam'
    quantity: int,
    needs_artwork: bool = False,
    artwork_hours: float = 0.0,
    organization_slug: str = DEFAULT_ORG_SLUG,
) -> QuoteResult | EscalationResult:
    """Look up a booklet price by format + binding + pages + cover + quantity (per tenant)."""

    product_key = f"booklet_{format}_{binding}"
    product = (
        db.query(Product)
        .filter_by(
            organization_slug=organization_slug,
            key=product_key,
            category="booklet",
        )
        .first()
    )
    if product is None:
        return EscalationResult(
            reason=f"Booklet type '{format.upper()} {binding.replace('_', ' ')}' not found.",
        )

    spec_key = f"{pages}pp|{cover_type}"
    tier = db.query(PriceTier).filter_by(
        product_id=product.id, spec_key=spec_key, quantity=quantity,
    ).first()

    if tier is None:
        # Gather available specs for a helpful escalation message
        available_specs = db.query(PriceTier.spec_key, PriceTier.quantity).filter_by(
            product_id=product.id,
        ).all()
        available_pages = sorted({int(s.split("pp")[0]) for s, _ in available_specs})
        available_qtys = sorted({q for _, q in available_specs})
        return EscalationResult(
            reason=f"No matching price for {pages}pp / {cover_type} / qty {quantity}.",
            product_name=product.name,
            message=(
                f"Available page counts: {available_pages}. "
                f"Available quantities: {available_qtys}. "
                f"Justin needs to quote this directly."
            ),
        )

    base_price = tier.price  # booklets are "per job" — no unit multiplier

    # Client multiplier — applied BEFORE VAT like the other product families.
    final_ex = base_price
    applied: list[str] = []
    client_mult = _get_client_multiplier(db, organization_slug=organization_slug)
    if abs(client_mult - 1.0) > 1e-6:
        final_ex = round(base_price * client_mult, 2)
        pct = int(round((client_mult - 1.0) * 100))
        sign = "+" if pct >= 0 else ""
        applied.append(f"Client adjustment: {sign}{pct}%")

    vat_rate = _get_vat_rate_for_product(db, product, organization_slug)
    vat = round(final_ex * vat_rate, 2)

    artwork_ex = None
    artwork_inc = None
    total = round(final_ex + vat, 2)
    if needs_artwork and artwork_hours > 0:
        artwork_rate = _get_setting(db, "artwork_rate_eur", 65.0, organization_slug=organization_slug)
        artwork_ex = round(artwork_rate * artwork_hours, 2)
        artwork_inc = round(artwork_ex * (1 + _STANDARD_VAT_RATE), 2)  # artwork = service, 23%
        total = round(total + artwork_inc, 2)

    # Descriptive name for the quote line
    cover_labels = {
        "self_cover": "Self Cover (150gsm silk)",
        "card_cover": "Card Cover (300gsm/150gsm silk)",
        "card_cover_lam": "Card Cover + Matt/Gloss Lam",
    }
    product_name = (
        f"Booklet {format.upper()} — {pages}pp — "
        f"{binding.replace('_', ' ').title()} — "
        f"{cover_labels.get(cover_type, cover_type)}"
    )

    notes = []
    if binding == "saddle_stitch" and pages > 48:
        notes.append("Saddle stitch is typically suitable up to 48pp.")
    if binding == "perfect_bound" and pages < 24:
        notes.append("Perfect bound typically starts from 24pp.")
    if quantity > 500:
        notes.append("For quantities above 500, contact Justin for a formal quotation.")

    turnaround = _get_setting(db, "standard_turnaround", "3-5 working days", organization_slug=organization_slug)

    return QuoteResult(
        success=True,
        product_name=product_name,
        category="booklet",
        quantity=quantity,
        quantity_unit="copies",
        base_price=base_price,
        surcharges_applied=applied,
        surcharge_amount=round(final_ex - base_price, 2),
        final_price_ex_vat=final_ex,
        vat_amount=vat,
        final_price_inc_vat=round(final_ex + vat, 2),
        artwork_cost_ex_vat=artwork_ex,
        artwork_cost_inc_vat=artwork_inc,
        total_inc_everything=total,
        turnaround=turnaround,
        notes=notes,
        pricing_unit="per job",
    )


# =============================================================================
# CATALOG BROWSING (used by LLM to know what's available)
# =============================================================================


def list_products(
    db: Session,
    category: Optional[str] = None,
    organization_slug: str = DEFAULT_ORG_SLUG,
) -> list[dict]:
    """List all products for a tenant, optionally filtered by category."""
    q = db.query(Product).filter_by(organization_slug=organization_slug)
    if category:
        q = q.filter_by(category=category)
    products = q.order_by(Product.category, Product.name).all()

    result = []
    for p in products:
        row = {
            "key": p.key,
            "name": p.name,
            "category": p.category,
            "sizes": p.sizes,
            "finishes": p.finishes,
        }
        if p.category == "small_format":
            row["available_quantities"] = sorted(
                t.quantity for t in db.query(PriceTier).filter_by(product_id=p.id, spec_key="").all()
            )
            row["price_per"] = p.price_per
        elif p.category == "large_format":
            row["unit_price"] = p.unit_price
            row["bulk_price"] = p.bulk_price
            row["bulk_threshold"] = p.bulk_threshold
            row["pricing_unit"] = p.pricing_unit
        elif p.category == "booklet":
            tiers = db.query(PriceTier).filter_by(product_id=p.id).all()
            row["available_page_counts"] = sorted({int(t.spec_key.split("pp")[0]) for t in tiers})
            row["cover_types"] = sorted({t.spec_key.split("|")[1] for t in tiers})
            row["available_quantities"] = sorted({t.quantity for t in tiers})
        result.append(row)
    return result
