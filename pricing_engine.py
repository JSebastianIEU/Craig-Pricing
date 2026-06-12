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

    # v34 — distinguishes "engine refused to price BY POLICY" (a per-sq/m
    # or POA product is configured manual_review_required=True on the
    # Product row) from "engine couldn't find a tier for this qty"
    # (the legacy escalation path). The LLM shell handles them
    # differently:
    #   - manual_review=True → auto-create a Quote(status='needs_revision'),
    #     fire the operator notification, ask the customer for the missing
    #     detail (dimensions, etc.) without ever quoting a number.
    #   - manual_review=False → the LLM may either retry with a known qty
    #     OR call escalate_to_justin (legacy path).
    manual_review: bool = False

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
    Return `(amount, kind)` for a named surcharge — backwards-compatible
    helper used by call sites that don't have a Product handy (e.g.
    artwork hours). Does NOT honor scope filtering. Prefer
    `_resolve_surcharge_for_product` for product-aware pricing.

    Kind is either 'multiplier' (fraction, e.g. 0.20 for +20%) or
    'additive' (flat euro amount added once per job, e.g. 15.0 for
    +€15). Returns `(0.0, 'multiplier')` when the rule isn't
    configured for this tenant — applying a zero multiplier is a
    no-op.
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


def _resolve_surcharge_for_product(
    db: Session,
    name: str,
    product: Product,
    organization_slug: str = DEFAULT_ORG_SLUG,
) -> tuple[float, str]:
    """
    v34 — resolve a surcharge with product-aware scope. Most-specific
    scope wins:

      1. `applies_to_product_keys` non-empty → applies only when
         product.key is in the list.
      2. `applies_to_category` non-null → applies only when
         product.category matches.
      3. Both null → global, applies to everyone.

    Returns `(0.0, 'multiplier')` when the rule isn't configured OR
    when the configured scope doesn't include this product. Same
    return shape as `_get_surcharge_rule` so call sites can swap
    them with no other changes.

    NOTE: A surcharge with product_keys but the current product
    isn't in the list returns zeros — the surcharge is intentionally
    OFF for that product. This is what fixes the v32-era bug where
    e.g. soft_touch was nominally scoped to small_format but actually
    fired on every product because applies_to_category was ignored.
    """
    rule = (
        db.query(SurchargeRule)
        .filter_by(organization_slug=organization_slug, name=name)
        .first()
    )
    if not rule:
        return (0.0, "multiplier")

    # Most specific scope: applies_to_product_keys
    keys = rule.applies_to_product_keys
    if keys:
        # JSON column comes back as list on Postgres, may be a JSON-
        # encoded string on a fresh SQLite row depending on driver
        # behavior — normalize.
        if isinstance(keys, str):
            try:
                keys = json.loads(keys)
            except json.JSONDecodeError:
                keys = []
        if isinstance(keys, list) and len(keys) > 0:
            if product.key not in keys:
                return (0.0, "multiplier")
            # else: fall through to amount/kind read below

    # Next: applies_to_category (only if no product-key list narrowed scope)
    elif rule.applies_to_category:
        if product.category != rule.applies_to_category:
            return (0.0, "multiplier")

    # Else: global — no narrowing, all products receive it.

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


def _parse_size_mm(s: Optional[str]) -> Optional[tuple[int, int]]:
    """v36 — parse a 'WxH' or 'W x H' string (mm) into (width, height).

    Used for `Product.default_unit_size_mm` and `Product.sheet_size_mm`,
    both of which are stored as strings ('50x30', '2400x1200',
    '8 x 4 ft' would NOT work — use mm) for human readability.

    Returns None on any parse error so the caller can fall back gracefully
    to escalation rather than crashing on a typo in the catalog.
    """
    if not s:
        return None
    cleaned = s.strip().lower().replace(" ", "")
    # Accept both 'x' and '×' separators
    sep = "x" if "x" in cleaned else ("×" if "×" in cleaned else None)
    if not sep:
        return None
    try:
        w_str, h_str = cleaned.split(sep, 1)
        w = int(float(w_str))
        h = int(float(h_str))
        if w <= 0 or h <= 0:
            return None
        return (w, h)
    except (ValueError, TypeError):
        return None


def _units_per_sheet(panel_w: int, panel_h: int, sheet_w: int, sheet_h: int) -> int:
    """v36 — greedy axis-aligned packing of identical rectangles. Tries
    both panel orientations on the sheet and returns the larger yield.

    Pure integer math — does NOT account for rotated mixed-orientation
    layouts (a known suboptimal case), but matches how Justin actually
    cuts panels on the saw. Returns 0 if the panel doesn't fit at all.
    """
    if panel_w <= 0 or panel_h <= 0 or sheet_w <= 0 or sheet_h <= 0:
        return 0
    # Orientation A: panel oriented as-is
    cols_a = sheet_w // panel_w
    rows_a = sheet_h // panel_h
    yield_a = cols_a * rows_a
    # Orientation B: panel rotated 90 degrees
    cols_b = sheet_w // panel_h
    rows_b = sheet_h // panel_w
    yield_b = cols_b * rows_b
    return max(yield_a, yield_b)


# ---------------------------------------------------------------------------
# v34 — stack-tier helper
# ---------------------------------------------------------------------------
#
# When the customer asks for a quantity that doesn't exactly match any
# PriceTier (e.g. 530 business cards on a sheet with [100, 250, 500, 1000,
# 2500] tiers), this helper builds the cheapest *combination* of available
# tiers that totals >= requested qty. The customer pays for the combined
# bin (e.g. 500 + 100 = 600 cards) at the sum of those tiers' prices.
#
# Confirmed by Justin (May 2026): 530 cards should NOT escalate to a
# manual quote — Craig should bill it as 500 + 100 = 600 cards. Same
# rule applies to flyers, brochures, NCR books — every tiered product.
#
# Algorithm (greedy, largest-first):
#   1. Sort available tiers descending by qty.
#   2. While remaining > 0: take the largest tier ≤ remaining, decrement.
#   3. If remaining > 0 (i.e. requested overflows the smallest tier):
#      take the smallest tier ≥ remaining as a top-up.
#   4. If still no fit (requested > max_oversize_factor × largest tier):
#      return None — too far off, escalate to a manual quote.
#
# Greedy is naturally cost-optimal here because the per-unit price
# decreases (or stays flat) with larger tiers — so picking larger
# tiers first minimizes the bill. Tested against Justin's sheets.
# ---------------------------------------------------------------------------


def _stack_tiers(
    db: Session,
    product_id: int,
    spec_key: str,
    requested_qty: int,
    unit_base: int = 1,
    *,
    per_job: bool = False,
    max_oversize_factor: int = 5,
) -> Optional[tuple[int, float, list[tuple[int, float]]]]:
    """Build the cheapest combination of available tiers that totals
    >= requested_qty.

    Args:
      unit_base: how the `tier.price` column is denominated for
        per-base products. Small-format tiered products store per-base
        prices (e.g. €38 = "per 100 business cards"), so the actual
        line-item cost for a tier is
        `tier.price * tier.quantity / unit_base`.
      per_job: when True, treat `tier.price` as the FULL line-item cost
        for `tier.quantity` items (no multiplication). Booklets are
        priced this way — the 100-tier of an 8pp self-cover A5 booklet
        IS €110, not "€110 per 100 booklets". Pass per_job=True from
        quote_booklet.

    Returns:
      (billed_qty, total_price, breakdown)
      where breakdown is a list of (tier_qty, line_item_price) entries
      — note line_item_price is the FULL cost of using that tier.
    Returns None when:
      - no tiers exist for this product+spec_key
      - requested_qty exceeds max_oversize_factor × largest tier qty
        (we'd be guessing too much — escalate instead).

    Edge cases:
      - requested_qty <= smallest tier → the smallest tier is used
        once. Customer pays for the smallest tier's full qty.
        (e.g. 50 cards billed as 100 cards.)
      - requested_qty exactly matches a tier → returns that single
        tier (callers usually short-circuit to the single-tier path
        BEFORE calling here, but it's still correct).
    """
    rows = (
        db.query(PriceTier)
        .filter_by(product_id=product_id, spec_key=spec_key)
        .order_by(PriceTier.quantity.desc())
        .all()
    )
    if not rows:
        return None

    base = max(1, int(unit_base or 1))
    if per_job:
        # tier.price IS the line-item cost — no multiplication.
        tiers_desc: list[tuple[int, float]] = [
            (int(t.quantity), float(t.price)) for t in rows
        ]
    else:
        # tier.price is per-base; line-item cost = price * qty / base.
        tiers_desc = [
            (int(t.quantity), round(float(t.price) * int(t.quantity) / base, 2))
            for t in rows
        ]
    smallest_qty, _ = tiers_desc[-1]
    largest_qty, _ = tiers_desc[0]

    if requested_qty > max_oversize_factor * largest_qty:
        return None

    # Special case: requested under the smallest tier → bill the smallest
    # tier once. Customer pays for the smallest bin.
    if requested_qty <= smallest_qty:
        return (smallest_qty, tiers_desc[-1][1], [tiers_desc[-1]])

    # Greedy fill (largest-first)
    remaining = requested_qty
    breakdown: list[tuple[int, float]] = []
    while remaining > 0:
        # Largest tier ≤ remaining
        chosen = next(((q, p) for q, p in tiers_desc if q <= remaining), None)
        if chosen is None:
            # Remaining smaller than the smallest tier → top up with smallest
            chosen = tiers_desc[-1]
            breakdown.append(chosen)
            remaining = 0
            break
        breakdown.append(chosen)
        remaining -= chosen[0]

    billed_qty = sum(q for q, _ in breakdown)
    total_price = round(sum(p for _, p in breakdown), 2)
    return (billed_qty, total_price, breakdown)


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


# Standard Irish service-rate VAT — applied to shipping (a service line item).
# Goods follow their own product-category rate; shipping doesn't.
_SHIPPING_VAT_RATE = 0.23


def apply_shipping_to_quote(
    db: Session,
    quote,
    delivery_method: str | None,
    organization_slug: str = DEFAULT_ORG_SLUG,
) -> dict:
    """
    Phase F — compute and persist shipping costs on a Quote based on the
    chosen `delivery_method`. Mutates `quote.shipping_cost_ex_vat`,
    `quote.shipping_cost_inc_vat`, and `quote.total` in place. Caller
    is responsible for committing.

    Policy (per Justin / Roi):
      - delivery_method != 'delivery'  -> €0 (collection or anything else)
      - goods inc VAT >= threshold     -> €0 (free over the threshold)
      - else                           -> flat fee inc VAT (default €15)

    The threshold compares against `quote.final_price_inc_vat` (goods
    only — does NOT include shipping itself, so adding shipping doesn't
    push a €99 quote over €100 retroactively).

    Both fee + threshold come from settings so Justin / future tenants
    can tweak via the Settings tab without a code change:
      - shipping_fee_inc_vat            (default 15.00)
      - free_shipping_threshold_inc_vat (default 100.00)

    Returns:
      {
        "shipping_ex_vat":  float,
        "shipping_inc_vat": float,
        "free_shipping":    bool,    # true iff the threshold was met
        "applies":          bool,    # true iff delivery_method == 'delivery'
      }
    """
    method = (delivery_method or "").strip().lower()
    fee_inc = float(_get_setting(
        db, "shipping_fee_inc_vat", 15.00, organization_slug=organization_slug,
    ))
    threshold_inc = float(_get_setting(
        db, "free_shipping_threshold_inc_vat", 100.00, organization_slug=organization_slug,
    ))

    goods_inc = float(quote.final_price_inc_vat or 0.0)
    applies = method == "delivery"
    free_shipping = goods_inc >= threshold_inc

    if applies and not free_shipping:
        shipping_inc = round(fee_inc, 2)
        shipping_ex = round(shipping_inc / (1 + _SHIPPING_VAT_RATE), 2)
    else:
        shipping_inc = 0.0
        shipping_ex = 0.0

    quote.shipping_cost_ex_vat = shipping_ex
    quote.shipping_cost_inc_vat = shipping_inc
    # Recompute total. `quote.total` historically equals
    # `final_price_inc_vat + artwork_inc_vat`. Now we add shipping too.
    artwork_inc_vat_implicit = max(
        0.0,
        float(quote.total or 0.0)
        - float(quote.final_price_inc_vat or 0.0)
        - float(getattr(quote, "shipping_cost_inc_vat", 0.0) or 0.0)
        # Heuristic: artwork_cost stored ex VAT in our schema
        # (see quote_*_format functions). The "old" total embedded
        # artwork_cost_inc. We re-derive it as artwork_ex × 1.23.
    )
    artwork_ex = float(quote.artwork_cost or 0.0)
    artwork_inc = round(artwork_ex * (1 + _SHIPPING_VAT_RATE), 2)

    quote.total = round(
        float(quote.final_price_inc_vat or 0.0) + artwork_inc + shipping_inc,
        2,
    )

    return {
        "shipping_ex_vat":  shipping_ex,
        "shipping_inc_vat": shipping_inc,
        "free_shipping":    bool(applies and free_shipping),
        "applies":          applies,
    }


# =============================================================================
# v41 — per-product floor + auto-quote ceiling helpers
# =============================================================================


def _check_quantity_bounds(product, quantity: int) -> Optional["EscalationResult"]:
    """Quantity bounds guard at the top of every quote_* path. Returns an
    EscalationResult when the quantity is out of bounds, else None.

    FLOOR (v41.1) — a non-positive quantity is invalid input. The engine
    must NEVER run pricing math on it: a negative qty would otherwise flow
    through the ``quantity / unit_base`` multiplier and produce a NEGATIVE
    price (e.g. -50 → -€25). Justin flagged this directly. We return a
    *soft* escalation (``manual_review=False``) so the LLM shell simply
    re-asks the customer for a real quantity instead of pinging Justin or
    quoting a number. Belt-and-suspenders with the API-layer
    ``Field(gt=0)`` and the tool-schema ``minimum: 1`` — this one also
    covers the in-process LLM tool path, which never touches Pydantic.

    CEILING (v41) — when ``product.max_qty_for_auto_quote`` is set and
    ``quantity`` exceeds it, escalate BEFORE any pricing math with
    ``manual_review=True`` so the LLM auto-creates a
    ``Quote(status='needs_revision')`` and pings Justin via the v33
    notification pipeline (customer sees "let me get Justin to confirm",
    no number quoted). Returns None for legacy products (max_qty unset).
    """
    # FLOOR — reject non-positive (or missing) quantities everywhere.
    if quantity is None or quantity <= 0:
        return EscalationResult(
            reason=(
                f"invalid quantity {quantity!r} on {product.name} — "
                "must be a positive whole number"
            ),
            product_name=product.name,
            manual_review=False,
            message="That quantity doesn't look right — how many did you need?",
        )

    cap = getattr(product, "max_qty_for_auto_quote", None)
    if not cap:
        return None
    try:
        cap_int = int(cap)
    except (TypeError, ValueError):
        return None
    if cap_int <= 0 or quantity <= cap_int:
        return None
    return EscalationResult(
        reason=(
            f"quantity {quantity} exceeds the auto-quote ceiling "
            f"({cap_int}) on {product.name}"
        ),
        product_name=product.name,
        manual_review=True,
        message=(
            "Quantity is above the auto-quote limit for this product — "
            "Justin to price manually from the dashboard."
        ),
    )


def _apply_min_order_floor(
    product,
    final_ex: float,
    surcharges_applied: list[str],
) -> float:
    """v41 — per-product minimum order floor. If
    ``product.min_order_value_eur`` is set and ``final_ex`` (ex-VAT
    subtotal AFTER surcharges + client multiplier, BEFORE VAT) falls
    below it, this returns the floor value AND appends a human-
    readable line to ``surcharges_applied`` so the LLM mentions the
    minimum to the customer (\"Minimum order €25\"). No-op for legacy
    products (min unset) — returns the original ``final_ex``.

    Centralised so the same logic fires in every quote_* path
    (small_format / per_sqm / per_sheet / booklet) and tier-stack
    helpers can share the call site rather than reimplementing the
    branch.
    """
    floor = getattr(product, "min_order_value_eur", None)
    if not floor:
        return final_ex
    try:
        floor_f = float(floor)
    except (TypeError, ValueError):
        return final_ex
    if floor_f <= 0 or final_ex >= floor_f:
        return final_ex
    surcharges_applied.append(
        f"Minimum order €{floor_f:.2f} applied "
        f"(computed was €{final_ex:.2f})"
    )
    return round(floor_f, 2)


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

    # v34 — manual-review escalation. Some products are flagged as
    # "always escalate" (per-sq/m, POA, custom-cut). Refuse to price
    # them at runtime regardless of whether a tier exists; the LLM
    # shell creates a Quote(status='needs_revision') and notifies
    # Justin so he prices manually from the dashboard.
    if product.manual_review_required:
        return EscalationResult(
            reason=product.manual_review_reason or "manual review required",
            product_name=product.name,
            manual_review=True,
            message=(
                "This product needs Justin's eyes — engine refused to "
                "auto-quote. Customer should be told 'let me check' and "
                "asked for the missing detail (dimensions in mm, etc.)."
            ),
        )

    # v41 — qty ceiling check. If the product has a max_qty_for_auto_quote
    # set and the requested quantity is above it, escalate BEFORE any
    # pricing math (don't quote a huge job Justin needs to confirm).
    _ceil = _check_quantity_bounds(product, quantity)
    if _ceil is not None:
        return _ceil

    tier = db.query(PriceTier).filter_by(
        product_id=product.id, spec_key="", quantity=quantity,
    ).first()

    # v34 — stack-tier fallback. When the requested qty doesn't match a
    # tier exactly (e.g. 530 business cards on [100, 250, 500, 1000,
    # 2500]), bill it as the cheapest combination of available tiers
    # whose sum ≥ requested. So 530 → 500 + 100 = 600 cards bin, cost =
    # tier(500).price + tier(100).price. Confirmed by Justin: this is
    # how the print shop actually charges off-tier qtys, not as an
    # escalation. See _stack_tiers docstring for the algorithm.
    unit_base = _parse_unit_base(product.price_per)
    base_price = 0.0
    surcharges_applied: list[str] = []
    if tier is not None:
        # Exact match — original behaviour. Tier prices are per-base
        # (e.g. "per 100 cards"), so multiply by qty/base.
        qty_multiplier = quantity / unit_base
        base_price = round(tier.price * qty_multiplier, 2)
    else:
        stacked = _stack_tiers(db, product.id, "", quantity, unit_base)
        if stacked is None:
            available = sorted(
                t.quantity for t in db.query(PriceTier)
                .filter_by(product_id=product.id, spec_key="").all()
            )
            return EscalationResult(
                reason=f"Quantity {quantity} is too far off the pricing sheet for {product.name}.",
                product_name=product.name,
                message=(
                    f"Available quantities: {available}. {quantity} would "
                    f"require >5x the largest tier — Justin needs to quote "
                    f"this directly."
                ),
            )
        billed_qty, total_price, breakdown = stacked
        base_price = total_price
        # Render a human-readable note about the stacking — Craig will
        # repeat this in the customer reply (and Justin sees it in the
        # quote breakdown).
        tier_summary = " + ".join(str(q) for q, _ in breakdown)
        if billed_qty == quantity:
            surcharges_applied.append(
                f"Tier combination: {tier_summary} = {quantity}"
            )
        else:
            surcharges_applied.append(
                f"Tier combination: {quantity} billed as {tier_summary} = {billed_qty}"
            )
    multiplier = 1.0   # collects all multiplier-kind surcharges (e.g. +20%)
    additive = 0.0     # collects all additive-kind surcharges (e.g. +€15 flat)

    def _apply(name: str, label_pct: str, label_flat: str) -> None:
        """Look up a surcharge by name and fold it into the right accumulator
        based on its kind. Centralised so every surcharge (double_sided,
        soft_touch, triplicate, ...) shares the same branch logic.

        v34 — uses `_resolve_surcharge_for_product` so per-product
        and per-category scoping are honored. Surcharges that don't
        target this product return zeros and are silently skipped."""
        nonlocal multiplier, additive
        amount, kind = _resolve_surcharge_for_product(
            db, name, product, organization_slug=organization_slug,
        )
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
            if product.key in ("ncr_books_a5", "ncr_books_a4"):
                _apply(
                    "triplicate",
                    label_pct="Triplicate: +{pct}%",
                    label_flat="Triplicate: +\u20ac{amount:.2f}",
                )
            else:
                return EscalationResult(
                    reason="Triplicate finish only applies to NCR books.",
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

    # v41 — minimum order floor. Applied AFTER client multiplier so the
    # multiplier can't dodge the floor; BEFORE VAT because the floor is
    # an ex-VAT business rule (Justin's "minimum €25 ex VAT").
    final_ex = _apply_min_order_floor(product, final_ex, surcharges_applied)

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
# LARGE FORMAT — per-sq/m + per-sheet helpers (v36)
# =============================================================================


def _quote_per_sqm(
    db: Session,
    product: Product,
    quantity: int,
    *,
    width_mm: Optional[int] = None,
    height_mm: Optional[int] = None,
    area_sqm: Optional[float] = None,
    needs_artwork: bool = False,
    artwork_hours: float = 0.0,
    organization_slug: str = DEFAULT_ORG_SLUG,
) -> "QuoteResult | EscalationResult":
    """v36 — price a per-square-meter product (vinyl labels, banners,
    graphics, fabric displays).

    Computation flow (first non-None wins):
      1. `area_sqm` passed explicitly → use as total area.
      2. `width_mm` + `height_mm` provided:
           per_unit_area = (w × h) / 1_000_000
           total_m² = quantity × per_unit_area  (for area-based products)
                  OR total_m² = quantity / yield_per_sqm  (for items-cut-from-sheet products)
      3. Fall back to `product.default_unit_size_mm` (parsed) using the same logic.
      4. Fall back to `product.yield_per_sqm` alone (uses qty-based formula).
      5. Out of options → EscalationResult(manual_review=True).

    Then:
      total_ex_vat = total_m² × unit_price (or bulk_price if total_m² ≥ bulk_threshold)
      + client multiplier + VAT + artwork.
    """
    if quantity <= 0:
        return EscalationResult(
            reason="quantity must be positive",
            product_name=product.name,
        )

    # v41 — qty ceiling check at the top of per_sqm path.
    _ceil = _check_quantity_bounds(product, quantity)
    if _ceil is not None:
        return _ceil

    # 1. Resolve total area
    total_m2: Optional[float] = None
    breakdown_note: str = ""
    yield_value = product.yield_per_sqm  # may be null

    if area_sqm is not None and area_sqm > 0:
        total_m2 = float(area_sqm)
        breakdown_note = f"customer-supplied area: {total_m2:.2f} m²"
    else:
        # If width+height passed, prefer them
        if width_mm and height_mm:
            unit_area = (float(width_mm) * float(height_mm)) / 1_000_000.0
            if yield_value and yield_value > 0:
                # Items-cut-from-sheet: prefer the explicit yield over
                # multiplying area×qty (the customer might have given
                # a label size for visual reference, but the catalog
                # configuration says "we cut N per m²" — trust the
                # catalog).
                # Actually, items-cut-from-sheet uses qty/yield. We
                # only multiply by qty when there's NO yield and the
                # product is treated as area-based (banners).
                pass
            if yield_value is None:
                # Area-based (banners): qty × per-unit area
                total_m2 = round(quantity * unit_area, 4)
                breakdown_note = (
                    f"{quantity} × ({width_mm}×{height_mm} mm) "
                    f"= {total_m2:.2f} m²"
                )
            else:
                # Items-cut-from-sheet: derive yield from the SUPPLIED
                # dimensions if we have them — that's the most
                # accurate. Otherwise fall back to the catalog yield.
                if unit_area > 0:
                    derived_yield = 1.0 / unit_area
                    total_m2 = round(quantity / derived_yield, 4)
                    breakdown_note = (
                        f"{quantity} units × {width_mm}×{height_mm} mm "
                        f"({derived_yield:.0f} per m²) "
                        f"= {total_m2:.2f} m²"
                    )
                else:
                    total_m2 = round(quantity / yield_value, 4)
                    breakdown_note = (
                        f"{quantity} / {yield_value:.0f} per m² "
                        f"= {total_m2:.2f} m²"
                    )

        # No width+height passed — try product defaults
        if total_m2 is None and product.default_unit_size_mm:
            parsed = _parse_size_mm(product.default_unit_size_mm)
            if parsed:
                w, h = parsed
                unit_area = (w * h) / 1_000_000.0
                if yield_value is None:
                    total_m2 = round(quantity * unit_area, 4)
                    breakdown_note = (
                        f"{quantity} × default {w}×{h} mm "
                        f"= {total_m2:.2f} m²"
                    )
                elif unit_area > 0:
                    derived_yield = 1.0 / unit_area
                    total_m2 = round(quantity / derived_yield, 4)
                    breakdown_note = (
                        f"{quantity} units × default {w}×{h} mm "
                        f"({derived_yield:.0f} per m²) "
                        f"= {total_m2:.2f} m²"
                    )

        # Last resort: pure yield-based math (no per-item area).
        # v38 — products flagged `requires_dimensions=True` MUST NOT
        # fall back to yield-only. Their item sizes vary too widely
        # (vinyl labels: 40x10mm to 200x200mm — same yield gives a
        # 30x wrong price). When the LLM forgot to pass dims, escalate
        # instead so the customer never sees a "crazy" price.
        if (
            total_m2 is None
            and yield_value and yield_value > 0
            and not getattr(product, "requires_dimensions", False)
        ):
            total_m2 = round(quantity / yield_value, 4)
            breakdown_note = (
                f"{quantity} / {yield_value:.0f} per m² "
                f"= {total_m2:.2f} m²"
            )

    if total_m2 is None or total_m2 <= 0:
        # Couldn't compute area from any input. Escalate.
        # v38 — distinct reason when the product requires_dimensions
        # so the LLM (and operator dashboard) sees WHY the yield-only
        # fallback was refused.
        if getattr(product, "requires_dimensions", False):
            reason = (
                f"{product.key} requires per-unit dimensions (width_mm "
                f"+ height_mm). Item sizes vary too widely for the "
                f"yield-only fallback to be safe. Refusing to quote "
                f"without dims."
            )
            message = (
                "Ask the customer for the size of each item in mm "
                "(width × height). Don't quote without it — small "
                "labels priced via yield-only get billed as if they "
                "were 10× bigger."
            )
        else:
            reason = (
                f"Per-sq/m product needs dimensions or area. Pass "
                f"width_mm + height_mm, or area_sqm directly. "
                f"Configure default_unit_size_mm / yield_per_sqm on "
                f"{product.key} in the catalog if you want a fallback."
            )
            message = (
                "Ask the customer for the size of each item in mm "
                "(width × height), then re-quote with width_mm + "
                "height_mm. If the customer gave overall area instead, "
                "pass area_sqm."
            )
        return EscalationResult(
            reason=reason,
            product_name=product.name,
            manual_review=True,
            message=message,
        )

    # v39 — minimum billable area. If the order's computed area falls
    # below the product's configured floor, bill it as the floor. Done
    # BEFORE bulk-price selection (so a floored area can legitimately
    # cross a bulk threshold) and the note is mutated so the customer
    # sees why the price is what it is. No-op when the column is unset
    # (getattr guard keeps legacy products + non-per_sqm paths safe).
    _min_sqm = getattr(product, "min_billable_sqm", None)
    if _min_sqm and total_m2 < float(_min_sqm):
        _actual_m2 = total_m2
        total_m2 = float(_min_sqm)
        breakdown_note = (
            f"{breakdown_note} (min {float(_min_sqm):g} m² applied; "
            f"actual {_actual_m2:.2f} m²)"
        )

    # 2. Pick price per m² (bulk if total_m² ≥ threshold)
    surcharges_applied: list[str] = [breakdown_note]
    threshold = product.bulk_threshold or 0
    if threshold and total_m2 >= threshold and product.bulk_price is not None:
        unit_price = float(product.bulk_price)
        surcharges_applied.append(
            f"Bulk pricing applied ({total_m2:.2f} m² ≥ {threshold} m²)"
        )
    else:
        unit_price = float(product.unit_price or 0.0)

    if unit_price <= 0:
        return EscalationResult(
            reason=f"unit_price not configured on {product.key}",
            product_name=product.name,
            manual_review=True,
        )

    total_ex = round(total_m2 * unit_price, 2)

    # v38 — sanity ceiling. If a per-unit price exceeds the
    # `sanity_max_unit_price` column, the catalog config is probably
    # mis-set OR the engine took a wrong-path branch (e.g. yield
    # fallback for a product that shouldn't have one). Escalate
    # instead of returning a customer-facing nonsense quote. Catches
    # the JP-0086 €24,600 vinyl-labels / yield-runaway class of bug.
    if (
        getattr(product, "sanity_max_unit_price", None)
        and quantity > 0
    ):
        per_unit_ex = total_ex / quantity
        max_unit = float(product.sanity_max_unit_price)
        if per_unit_ex > max_unit:
            return EscalationResult(
                reason=(
                    f"sanity-ceiling tripped: computed €{per_unit_ex:.2f}/unit "
                    f"on {product.key} (cap €{max_unit:.2f}/unit). "
                    f"Total ex VAT would have been €{total_ex:.2f}. "
                    f"Escalating instead of sending a probably-wrong quote."
                ),
                product_name=product.name,
                manual_review=True,
                message=(
                    "The per-unit price came out higher than the catalog's "
                    "sanity-max ceiling — Justin to verify the inputs / "
                    "catalog config before this goes to the customer."
                ),
            )

    # Client multiplier (after surcharges, before VAT)
    client_mult = _get_client_multiplier(db, organization_slug=organization_slug)
    if abs(client_mult - 1.0) > 1e-6:
        total_ex = round(total_ex * client_mult, 2)
        pct = int(round((client_mult - 1.0) * 100))
        sign = "+" if pct >= 0 else ""
        surcharges_applied.append(f"Client adjustment: {sign}{pct}%")

    # v41 — minimum order floor. AFTER client multiplier, BEFORE VAT.
    # Critical for vinyl_labels and per-sqm products where small areas
    # would otherwise price below Justin's €45 minimum.
    total_ex = _apply_min_order_floor(product, total_ex, surcharges_applied)

    # VAT
    vat_rate = _get_vat_rate_for_product(db, product, organization_slug)
    vat = round(total_ex * vat_rate, 2)

    # Artwork (service line item)
    artwork_ex = None
    artwork_inc = None
    total = round(total_ex + vat, 2)
    if needs_artwork and artwork_hours > 0:
        artwork_rate = _get_setting(
            db, "artwork_rate_eur", 65.0, organization_slug=organization_slug,
        )
        artwork_ex = round(artwork_rate * artwork_hours, 2)
        artwork_inc = round(artwork_ex * (1 + _STANDARD_VAT_RATE), 2)
        total = round(total + artwork_inc, 2)

    turnaround = _get_setting(
        db, "standard_turnaround", "3-5 working days",
        organization_slug=organization_slug,
    )

    return QuoteResult(
        success=True,
        product_name=product.name,
        category="large_format",
        quantity=quantity,
        quantity_unit=product.pricing_unit or "per sq/m",
        base_price=unit_price,
        surcharges_applied=surcharges_applied,
        surcharge_amount=0.0,
        final_price_ex_vat=total_ex,
        vat_amount=vat,
        final_price_inc_vat=round(total_ex + vat, 2),
        artwork_cost_ex_vat=artwork_ex,
        artwork_cost_inc_vat=artwork_inc,
        total_inc_everything=total,
        turnaround=turnaround,
        notes=[product.notes] if product.notes else [],
        pricing_unit=product.pricing_unit or "per sq/m",
    )


def _quote_per_sheet(
    db: Session,
    product: Product,
    quantity: int,
    *,
    width_mm: Optional[int] = None,
    height_mm: Optional[int] = None,
    needs_artwork: bool = False,
    artwork_hours: float = 0.0,
    organization_slug: str = DEFAULT_ORG_SLUG,
) -> "QuoteResult | EscalationResult":
    """v36 — price a per-sheet product (foamex / dibond / corri panels).

    Customer specifies panel size (`width_mm`, `height_mm`); engine
    computes how many panels fit on a sheet of `product.sheet_size_mm`
    (axis-aligned, with rotation), then bills:
        sheets_needed = ceil(quantity / units_per_sheet)
        total_ex_vat  = sheets_needed × product.sheet_price
    + client multiplier + VAT + artwork.

    If panel dimensions or sheet config are missing, escalates with
    `manual_review=True` so the LLM falls back to the v34 ask-for-info
    flow.
    """
    if quantity <= 0:
        return EscalationResult(
            reason="quantity must be positive",
            product_name=product.name,
        )

    # v41 — qty ceiling check at the top of per_sheet path.
    _ceil = _check_quantity_bounds(product, quantity)
    if _ceil is not None:
        return _ceil

    # 1. Need panel dimensions — no sensible default for panels
    if not (width_mm and height_mm):
        return EscalationResult(
            reason=(
                f"{product.name} is priced per sheet — need panel "
                f"dimensions in mm to calculate yield."
            ),
            product_name=product.name,
            manual_review=True,
            message=(
                "Ask the customer for the size of each panel in mm "
                "(width × height), then re-quote with width_mm + "
                "height_mm."
            ),
        )

    # 2. Need sheet config
    sheet_parsed = _parse_size_mm(product.sheet_size_mm)
    sheet_price = product.sheet_price or 0.0
    if not sheet_parsed or sheet_price <= 0:
        return EscalationResult(
            reason=(
                f"{product.key} is missing sheet config "
                f"(sheet_size_mm={product.sheet_size_mm!r}, "
                f"sheet_price={product.sheet_price!r}). Configure in "
                f"the dashboard catalog."
            ),
            product_name=product.name,
            manual_review=True,
        )
    sheet_w, sheet_h = sheet_parsed

    # 3. Compute panels per sheet
    units_per_sheet = _units_per_sheet(int(width_mm), int(height_mm), sheet_w, sheet_h)
    if units_per_sheet <= 0:
        return EscalationResult(
            reason=(
                f"Panel size {width_mm}×{height_mm} mm exceeds sheet "
                f"size {sheet_w}×{sheet_h} mm — can't cut from a "
                f"single sheet."
            ),
            product_name=product.name,
            manual_review=True,
        )

    import math
    sheets_needed = math.ceil(quantity / units_per_sheet)
    total_ex = round(sheets_needed * sheet_price, 2)

    surcharges_applied: list[str] = [
        f"{units_per_sheet} per sheet × {sheets_needed} sheet(s) "
        f"(panel {width_mm}×{height_mm} mm on sheet {sheet_w}×{sheet_h} mm) "
        f"= {sheets_needed * units_per_sheet} panels billed"
    ]

    # Client multiplier
    client_mult = _get_client_multiplier(db, organization_slug=organization_slug)
    if abs(client_mult - 1.0) > 1e-6:
        total_ex = round(total_ex * client_mult, 2)
        pct = int(round((client_mult - 1.0) * 100))
        sign = "+" if pct >= 0 else ""
        surcharges_applied.append(f"Client adjustment: {sign}{pct}%")

    # v41 — minimum order floor on per_sheet path. Catches "1 small
    # panel = €5" style requests below the €25 large_format minimum.
    total_ex = _apply_min_order_floor(product, total_ex, surcharges_applied)

    # VAT
    vat_rate = _get_vat_rate_for_product(db, product, organization_slug)
    vat = round(total_ex * vat_rate, 2)

    # Artwork
    artwork_ex = None
    artwork_inc = None
    total = round(total_ex + vat, 2)
    if needs_artwork and artwork_hours > 0:
        artwork_rate = _get_setting(
            db, "artwork_rate_eur", 65.0, organization_slug=organization_slug,
        )
        artwork_ex = round(artwork_rate * artwork_hours, 2)
        artwork_inc = round(artwork_ex * (1 + _STANDARD_VAT_RATE), 2)
        total = round(total + artwork_inc, 2)

    turnaround = _get_setting(
        db, "standard_turnaround", "3-5 working days",
        organization_slug=organization_slug,
    )

    return QuoteResult(
        success=True,
        product_name=product.name,
        category="large_format",
        quantity=quantity,
        quantity_unit=product.pricing_unit or "per sheet",
        base_price=sheet_price,
        surcharges_applied=surcharges_applied,
        surcharge_amount=0.0,
        final_price_ex_vat=total_ex,
        vat_amount=vat,
        final_price_inc_vat=round(total_ex + vat, 2),
        artwork_cost_ex_vat=artwork_ex,
        artwork_cost_inc_vat=artwork_inc,
        total_inc_everything=total,
        turnaround=turnaround,
        notes=[product.notes] if product.notes else [],
        pricing_unit=product.pricing_unit or "per sheet",
    )


# =============================================================================
# v40.7 — large_format TIERED + spec_key engine paths
# =============================================================================
#
# Justin's REAL board pricing is a 2-D table: (size, quantity) → price for
# 7 standard sizes (A4, A3, A2, A1, A0, 2440x1220 full sheet, 1220x1220
# half sheet) across 200 qty rows. That doesn't fit `per_sheet` (single
# sheet_price × ceil(qty / yield)) — that strategy was producing wrong
# numbers (e.g. 5 A3 corri quoted as €140 when his sheet says €70).
#
# Two new dispatch paths kick in when a large_format product is
# configured with `pricing_strategy='tiered'`:
#
#   1. Standard size  (size kwarg given, matches one of the 7 sizes)
#      → exact-tier lookup, spec_key=size. Falls back to _stack_tiers
#        on off-tier quantities (parity with quote_booklet).
#
#   2. Custom size    (width_mm + height_mm given, no `size` kwarg)
#      → laydown calculator: derive how many panels fit on the full
#        sheet considering bleed (per side) + grip area (top/bottom/
#        left/right). Constants live in Settings (v43 seed). Bills the
#        derived sheets_needed at the `2440x1220` tier price.
#
# Both helpers reuse the existing _units_per_sheet (no duplicate
# packing math) and apply min_order_floor + client multiplier + VAT +
# artwork in the same order as the other quote_* entry points so the
# QuoteResult shape stays identical for downstream consumers.

_STANDARD_BOARD_SIZES = ("A4", "A3", "A2", "A1", "A0", "2440x1220", "1220x1220")


def _quote_large_format_tiered_by_size(
    db: Session,
    product: "Product",
    quantity: int,
    size: str,
    needs_artwork: bool,
    artwork_hours: float,
    organization_slug: str,
) -> "QuoteResult | EscalationResult":
    """v40.7 — board pricing by exact (size, qty) tier table.

    Mirrors `quote_booklet` exactly: spec_key encodes the variant
    (here the size string), tier lookup is on (product_id, spec_key,
    quantity). Off-tier quantities fall back to _stack_tiers with
    per_job=True so we sum absolute tier prices, not unit prices.
    """
    # Defensive ceiling check (the caller — quote_large_format — also
    # checks, but keep this here for tests that hit the helper directly).
    _ceil = _check_quantity_bounds(product, quantity)
    if _ceil is not None:
        return _ceil

    # Normalize size — Justin's input is "A3", "2440x1220" etc.; allow
    # lowercase + spaces too so customer-friendly text routes correctly.
    spec_key = size.strip().upper().replace(" ", "")
    # Detect "WxH" form and keep the digits lowercased
    if "X" in spec_key and not spec_key.startswith("A"):
        spec_key = spec_key.lower().replace(" ", "")

    tier = db.query(PriceTier).filter_by(
        product_id=product.id, spec_key=spec_key, quantity=quantity,
    ).first()

    base_price = 0.0
    applied: list[str] = []
    if tier is not None:
        base_price = float(tier.price)
    else:
        stacked = _stack_tiers(db, product.id, spec_key, quantity, per_job=True)
        if stacked is None:
            available_specs = db.query(PriceTier.spec_key, PriceTier.quantity).filter_by(
                product_id=product.id,
            ).all()
            available_sizes = sorted({s for s, _ in available_specs})
            available_qtys = sorted({q for _, q in available_specs})
            return EscalationResult(
                reason=f"No matching price for {product.name} / size {spec_key} / qty {quantity}.",
                product_name=product.name,
                message=(
                    f"Available sizes: {available_sizes}. "
                    f"Available quantities: {available_qtys}. "
                    f"Justin needs to quote this directly."
                ),
            )
        billed_qty, total_price, breakdown = stacked
        base_price = total_price
        tier_summary = " + ".join(str(q) for q, _ in breakdown)
        if billed_qty == quantity:
            applied.append(f"Tier combination: {tier_summary} = {quantity}")
        else:
            applied.append(
                f"Tier combination: {quantity} billed as {tier_summary} = {billed_qty}"
            )

    # Client multiplier — applied BEFORE VAT (parity with booklet path).
    final_ex = base_price
    client_mult = _get_client_multiplier(db, organization_slug=organization_slug)
    if abs(client_mult - 1.0) > 1e-6:
        final_ex = round(base_price * client_mult, 2)
        pct = int(round((client_mult - 1.0) * 100))
        sign = "+" if pct >= 0 else ""
        applied.append(f"Client adjustment: {sign}{pct}%")

    # v41 — min order floor (parity).
    final_ex = _apply_min_order_floor(product, final_ex, applied)

    vat_rate = _get_vat_rate_for_product(db, product, organization_slug)
    vat = round(final_ex * vat_rate, 2)

    artwork_ex = None
    artwork_inc = None
    total = round(final_ex + vat, 2)
    if needs_artwork and artwork_hours > 0:
        artwork_rate = _get_setting(db, "artwork_rate_eur", 65.0, organization_slug=organization_slug)
        artwork_ex = round(artwork_rate * artwork_hours, 2)
        artwork_inc = round(artwork_ex * (1 + _STANDARD_VAT_RATE), 2)
        total = round(total + artwork_inc, 2)

    turnaround = _get_setting(
        db, "standard_turnaround", "3-5 working days",
        organization_slug=organization_slug,
    )

    return QuoteResult(
        success=True,
        product_name=f"{product.name} — {spec_key}",
        category="large_format",
        quantity=quantity,
        quantity_unit="panels",
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
        notes=[product.notes] if product.notes else [],
        pricing_unit=product.pricing_unit or f"per panel ({spec_key})",
    )


def _quote_large_format_with_laydown(
    db: Session,
    product: "Product",
    quantity: int,
    custom_width_mm: int,
    custom_height_mm: int,
    needs_artwork: bool,
    artwork_hours: float,
    organization_slug: str,
) -> "QuoteResult | EscalationResult":
    """v40.7 — custom-size board pricing via Justin's laydown calculator.

    Math (from `Laydown1.xls` Sheet-Fit calculator):

        bleed       = laydown_bleed_mm        (default 6)
        grip_front  = laydown_grip_front_mm   (default 15)
        grip_back   = laydown_grip_back_mm    (default  5)
        grip_side   = laydown_grip_side_mm    (default  5)   (each side)

        effective_panel_w = panel_w + 2 * bleed
        effective_panel_h = panel_h + 2 * bleed
        effective_sheet_w = sheet_w - 2 * grip_side
        effective_sheet_h = sheet_h - grip_front - grip_back

        units_per_sheet   = _units_per_sheet(eff_panel_w, eff_panel_h,
                                              eff_sheet_w,  eff_sheet_h)
        sheets_needed     = ceil(qty / units_per_sheet)

        price = exact-tier or _stack_tiers lookup at the full-sheet
                spec_key for qty=sheets_needed.

    The full-sheet spec_key is read from `product.sheet_size_mm`
    normalized to "WxH" (e.g. "2440x1220") — same format the seeded
    tier rows use.

    Escalates if:
      - product.sheet_size_mm is unset or unparsable
      - the effective panel doesn't fit on the effective sheet at all
      - no tier exists at the full-sheet spec_key
    """
    # Defensive ceiling (same as the by-size helper).
    _ceil = _check_quantity_bounds(product, quantity)
    if _ceil is not None:
        return _ceil

    sheet_dims = _parse_size_mm(product.sheet_size_mm)
    if sheet_dims is None:
        return EscalationResult(
            reason=f"{product.name} has no sheet_size_mm configured.",
            product_name=product.name,
            message="Sheet size not set — Justin needs to fill it on the dashboard.",
        )
    sheet_w, sheet_h = sheet_dims

    # Read laydown constants from Settings (v43 seed). Fall back to
    # Just Print's confirmed defaults if a tenant somehow misses the
    # seed — keeps the helper safe for multi-tenant catalogs that
    # adopt board pricing later.
    bleed = int(_get_setting(db, "laydown_bleed_mm", 6, organization_slug=organization_slug))
    grip_front = int(_get_setting(db, "laydown_grip_front_mm", 15, organization_slug=organization_slug))
    grip_back = int(_get_setting(db, "laydown_grip_back_mm", 5, organization_slug=organization_slug))
    grip_side = int(_get_setting(db, "laydown_grip_side_mm", 5, organization_slug=organization_slug))

    eff_panel_w = custom_width_mm + 2 * bleed
    eff_panel_h = custom_height_mm + 2 * bleed
    eff_sheet_w = sheet_w - 2 * grip_side
    eff_sheet_h = sheet_h - grip_front - grip_back

    units_per_sheet = _units_per_sheet(eff_panel_w, eff_panel_h, eff_sheet_w, eff_sheet_h)
    if units_per_sheet <= 0:
        return EscalationResult(
            reason=(
                f"{custom_width_mm}×{custom_height_mm}mm panel doesn't fit on "
                f"{sheet_w}×{sheet_h}mm sheet (with bleed + grip)."
            ),
            product_name=product.name,
            message=(
                "Panel is too big for a single sheet — Justin would have to "
                "split it or use a larger substrate. Escalating for manual quote."
            ),
        )

    import math
    sheets_needed = math.ceil(quantity / units_per_sheet)

    # Full-sheet spec_key — must match the tier rows seeded for the
    # standard "2440x1220" size. Lowercase + no spaces so the key matches
    # exactly whether stored as "2440x1220" or "2440 x 1220".
    full_sheet_spec = f"{sheet_w}x{sheet_h}"

    tier = db.query(PriceTier).filter_by(
        product_id=product.id, spec_key=full_sheet_spec, quantity=sheets_needed,
    ).first()

    base_price = 0.0
    applied: list[str] = [
        f"Laydown: {units_per_sheet} panels/sheet ({eff_panel_w}×{eff_panel_h}mm "
        f"on {eff_sheet_w}×{eff_sheet_h}mm useable) → {sheets_needed} sheet(s)"
    ]
    if tier is not None:
        base_price = float(tier.price)
    else:
        stacked = _stack_tiers(db, product.id, full_sheet_spec, sheets_needed, per_job=True)
        if stacked is None:
            return EscalationResult(
                reason=(
                    f"{product.name}: no tier for {full_sheet_spec} at "
                    f"qty {sheets_needed}."
                ),
                product_name=product.name,
                message="Sheet price ladder missing this quantity. Justin to quote manually.",
            )
        billed_qty, total_price, breakdown = stacked
        base_price = total_price
        tier_summary = " + ".join(str(q) for q, _ in breakdown)
        if billed_qty == sheets_needed:
            applied.append(f"Sheet tier combination: {tier_summary} = {sheets_needed}")
        else:
            applied.append(
                f"Sheet tier combination: {sheets_needed} billed as "
                f"{tier_summary} = {billed_qty}"
            )

    # Client multiplier + min order floor + VAT + artwork (parity).
    final_ex = base_price
    client_mult = _get_client_multiplier(db, organization_slug=organization_slug)
    if abs(client_mult - 1.0) > 1e-6:
        final_ex = round(base_price * client_mult, 2)
        pct = int(round((client_mult - 1.0) * 100))
        sign = "+" if pct >= 0 else ""
        applied.append(f"Client adjustment: {sign}{pct}%")

    final_ex = _apply_min_order_floor(product, final_ex, applied)

    vat_rate = _get_vat_rate_for_product(db, product, organization_slug)
    vat = round(final_ex * vat_rate, 2)

    artwork_ex = None
    artwork_inc = None
    total = round(final_ex + vat, 2)
    if needs_artwork and artwork_hours > 0:
        artwork_rate = _get_setting(db, "artwork_rate_eur", 65.0, organization_slug=organization_slug)
        artwork_ex = round(artwork_rate * artwork_hours, 2)
        artwork_inc = round(artwork_ex * (1 + _STANDARD_VAT_RATE), 2)
        total = round(total + artwork_inc, 2)

    turnaround = _get_setting(
        db, "standard_turnaround", "3-5 working days",
        organization_slug=organization_slug,
    )

    return QuoteResult(
        success=True,
        product_name=(
            f"{product.name} — custom {custom_width_mm}×{custom_height_mm}mm"
        ),
        category="large_format",
        quantity=quantity,
        quantity_unit="panels",
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
        notes=[product.notes] if product.notes else [],
        pricing_unit=product.pricing_unit or "per panel (custom)",
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
    # v34 — optional dimensions. Stamped onto the quote's `specs` so
    # Justin sees them when manually pricing a per-sq/m product. NOT
    # used to compute a price in v34 (the engine still escalates).
    width_mm: Optional[int] = None,
    height_mm: Optional[int] = None,
    area_sqm: Optional[float] = None,
    # v40.7 — board pricing by standard size. When set + product is
    # `pricing_strategy=tiered`, dispatch to _quote_large_format_tiered_by_size
    # instead of the legacy bulk_break path. width_mm + height_mm (no size)
    # routes to the laydown calculator for custom sizes.
    size: Optional[str] = None,
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

    # v34 — manual-review escalation. Per-sq/m + POA products are
    # configured manual_review_required=True; refuse to auto-price them
    # so a 500-qty vinyl-labels request never produces another €24,600
    # quote like JP-0086. The LLM shell creates a Quote with
    # status='needs_revision' and notifies Justin so he prices manually.
    if product.manual_review_required:
        return EscalationResult(
            reason=product.manual_review_reason or "manual review required",
            product_name=product.name,
            manual_review=True,
            message=(
                "This product needs Justin's eyes — engine refused to "
                "auto-quote. Customer should be told 'let me check' and "
                "asked for the missing detail (dimensions in mm, etc.)."
            ),
        )

    # v41 — qty ceiling check at the top of quote_large_format. Covers
    # ALL 3 dispatch paths (per_sqm, per_sheet, legacy bulk_break) in
    # one place so we don't have to repeat the guard inside each helper.
    # (The helpers also call _check_quantity_bounds defensively in case
    # they're invoked directly from a test or future code path.)
    _ceil = _check_quantity_bounds(product, quantity)
    if _ceil is not None:
        return _ceil

    # v36 — per-sq/m + per-sheet pricing strategies. These short-circuit
    # the legacy bulk_break path because their inputs (dimensions, sheet
    # config) require completely different math. Both helpers return
    # either a successful QuoteResult or an EscalationResult — the LLM
    # shell already handles either.
    strategy = (product.pricing_strategy or "").lower()
    if strategy in ("per_sqm", "per_unit_metric"):
        return _quote_per_sqm(
            db, product, quantity,
            width_mm=width_mm, height_mm=height_mm, area_sqm=area_sqm,
            needs_artwork=needs_artwork, artwork_hours=artwork_hours,
            organization_slug=organization_slug,
        )
    if strategy == "per_sheet":
        return _quote_per_sheet(
            db, product, quantity,
            width_mm=width_mm, height_mm=height_mm,
            needs_artwork=needs_artwork, artwork_hours=artwork_hours,
            organization_slug=organization_slug,
        )

    # v40.7 — large_format tiered dispatch. Two branches:
    #   (a) `size` given → 2-D (size, qty) tier lookup. Matches the way
    #       Justin's actual board price sheet is structured.
    #   (b) `width_mm` + `height_mm` given → laydown calculator. Computes
    #       how many panels fit on the full sheet (with bleed + grip) and
    #       bills the derived sheets_needed at the full-sheet tier.
    #   Neither → escalate asking the LLM to fetch one or the other.
    if strategy == "tiered":
        if size:
            return _quote_large_format_tiered_by_size(
                db, product, quantity, size,
                needs_artwork=needs_artwork, artwork_hours=artwork_hours,
                organization_slug=organization_slug,
            )
        if width_mm and height_mm:
            return _quote_large_format_with_laydown(
                db, product, quantity,
                custom_width_mm=int(width_mm), custom_height_mm=int(height_mm),
                needs_artwork=needs_artwork, artwork_hours=artwork_hours,
                organization_slug=organization_slug,
            )
        # v40.8.9 — Craig-facing escalation. The previous wording said
        # "Customer should be asked... what custom dimensions in mm?"
        # which Craig was repeating verbatim to the customer even when
        # the customer had already named a standard A-series size in
        # their message ("5 corri boards A3" → Craig: "what size in
        # mm?"). The new wording is INSTRUCTIONAL FOR THE LLM, not for
        # the customer: it tells Craig to RETRY the tool call with the
        # right `size` arg, and only ask the customer for mm if the
        # customer genuinely wanted a custom non-standard size.
        return EscalationResult(
            reason=(
                f"{product.name} was called without `size` and without "
                f"`width_mm`+`height_mm`."
            ),
            product_name=product.name,
            message=(
                "INSTRUCTION FOR CRAIG (do NOT repeat to the customer): "
                "if the customer named a standard size in their original "
                "message — A4, A3, A2, A1, A0, 2440x1220, or 1220x1220 "
                "(even just 'A3 boards' or 'full sheet') — RETRY this "
                "tool call immediately with `size` set to that value. "
                "Only ask the customer about millimetre dimensions if "
                "they explicitly want a custom panel size that is NOT "
                "one of the seven standards (e.g. '800x600mm' or "
                "'500mm by 500mm')."
            ),
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

    # v41 — minimum order floor on legacy bulk_break path. AFTER
    # client multiplier, BEFORE VAT. Note: `applied` is the local
    # surcharges list in this path (named `applied` here, not
    # `surcharges_applied`).
    total_ex = _apply_min_order_floor(product, total_ex, applied)

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

    # v34 — manual-review escalation (parity with small/large format).
    # Booklets aren't currently flagged manual_review_required in the
    # catalog, but the guard keeps the three pricing entry points
    # symmetric so a future POA booklet category lands cleanly.
    if product.manual_review_required:
        return EscalationResult(
            reason=product.manual_review_reason or "manual review required",
            product_name=product.name,
            manual_review=True,
            message=(
                "This product needs Justin's eyes — engine refused to "
                "auto-quote. Customer should be told 'let me check' and "
                "asked for the missing detail."
            ),
        )

    # v41 — qty ceiling check on booklets (symmetric with the other
    # two quote_* entry points).
    _ceil = _check_quantity_bounds(product, quantity)
    if _ceil is not None:
        return _ceil

    spec_key = f"{pages}pp|{cover_type}"
    tier = db.query(PriceTier).filter_by(
        product_id=product.id, spec_key=spec_key, quantity=quantity,
    ).first()

    # v34 — stack-tier fallback. Booklets are priced per-job (one tier =
    # one full price for the whole batch), so unit_base=1 and the
    # breakdown items ARE the line-item costs. If a customer asks for
    # 75 of an 8pp spec on a [25, 50, 100, 250, 500] sheet, we bill
    # 50 + 25 = 75 exact, cost = tier(50).price + tier(25).price.
    base_price = 0.0
    applied: list[str] = []
    if tier is not None:
        base_price = float(tier.price)
    else:
        stacked = _stack_tiers(db, product.id, spec_key, quantity, per_job=True)
        if stacked is None:
            available_specs = db.query(PriceTier.spec_key, PriceTier.quantity).filter_by(
                product_id=product.id,
            ).all()
            available_pages = sorted({int(s.split("pp")[0]) for s, _ in available_specs})
            available_qtys = sorted({q for _, q in available_specs})
            # Spec missing entirely (page/cover combo not on sheet) is
            # different from "qty too far off": surface both cases the
            # same way for the LLM.
            return EscalationResult(
                reason=f"No matching price for {pages}pp / {cover_type} / qty {quantity}.",
                product_name=product.name,
                message=(
                    f"Available page counts: {available_pages}. "
                    f"Available quantities: {available_qtys}. "
                    f"Justin needs to quote this directly."
                ),
            )
        billed_qty, total_price, breakdown = stacked
        base_price = total_price
        tier_summary = " + ".join(str(q) for q, _ in breakdown)
        if billed_qty == quantity:
            applied.append(f"Tier combination: {tier_summary} = {quantity}")
        else:
            applied.append(
                f"Tier combination: {quantity} billed as {tier_summary} = {billed_qty}"
            )

    # Client multiplier — applied BEFORE VAT like the other product families.
    final_ex = base_price
    client_mult = _get_client_multiplier(db, organization_slug=organization_slug)
    if abs(client_mult - 1.0) > 1e-6:
        final_ex = round(base_price * client_mult, 2)
        pct = int(round((client_mult - 1.0) * 100))
        sign = "+" if pct >= 0 else ""
        applied.append(f"Client adjustment: {sign}{pct}%")

    # v41 — minimum order floor on booklets. AFTER client multiplier,
    # BEFORE VAT (parity with the other three product families).
    final_ex = _apply_min_order_floor(product, final_ex, applied)

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
