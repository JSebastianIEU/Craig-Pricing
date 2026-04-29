"""
Admin API — consumed by the Strategos Dashboard.

All endpoints are JWT-protected (see auth.jwt_auth). JWTs carry
{email, org_slug, role}. URL paths scoped under /orgs/{slug}/* are guarded
by access_guard() which rejects mismatched orgs (strategos_admin overrides).

Design principles:
  - Thin handlers — defer to SQLAlchemy models, no business logic here
  - Strict Pydantic models (extra='forbid') for all write bodies
  - Every write requires role >= client_owner (member for quote actions only)
  - Every query filtered by organization_slug from the JWT
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request

from rate_limiter import rate_limit
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func
from sqlalchemy.orm import Session

from db import get_db
from db.models import (
    PRICING_STRATEGIES,
    Category,
    CategoryTaxMap,
    Conversation,
    PriceTier,
    Product,
    Quote,
    Setting,
    SurchargeRule,
    TaxRate,
)
from auth.jwt_auth import (
    StrategosClaims,
    access_guard,
    require_claims,
    require_role,
)

router = APIRouter(prefix="/admin/api", tags=["Admin API"])


# ============================================================================
# Helpers
# ============================================================================


def _scope(query, model, claims: StrategosClaims, target_slug: str):
    """
    Filter a query by organization_slug. strategos_admin can read any org;
    everyone else is locked to their own.
    """
    if claims.role == "strategos_admin":
        return query.filter(model.organization_slug == target_slug)
    return query.filter(model.organization_slug == claims.org_slug)


def _humanize(s: str) -> str:
    return s.replace("_", " ").title()


def _slugify(s: str) -> str:
    s = s.strip().lower()
    s = re.sub(r"[^a-z0-9]+", "_", s)
    return s.strip("_")


# ============================================================================
# /me
# ============================================================================


@router.get("/me")
def me(claims: StrategosClaims = Depends(require_claims)) -> dict[str, Any]:
    return {"email": claims.email, "org_slug": claims.org_slug, "role": claims.role}


# ============================================================================
# Categories
# ============================================================================


def _category_to_dict(
    c: Category, product_count: int, tax_rate_name: str | None,
) -> dict[str, Any]:
    return {
        "slug": c.slug,
        "name": c.name,
        "description": c.description,
        "icon": c.icon,
        "sort_order": c.sort_order,
        "product_count": product_count,
        "tax_rate_name": tax_rate_name,
    }


@router.get("/orgs/{org_slug}/categories")
def list_categories(
    org_slug: str,
    claims: StrategosClaims = Depends(require_claims),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    access_guard(org_slug, claims)

    cats = (
        _scope(db.query(Category), Category, claims, org_slug)
        .order_by(Category.sort_order, Category.name)
        .all()
    )
    counts_rows = (
        _scope(db.query(Product.category, func.count(Product.id)), Product, claims, org_slug)
        .group_by(Product.category)
        .all()
    )
    counts = {cat: int(count) for cat, count in counts_rows}
    tax_map = {
        m.category: m.tax_rate.name if m.tax_rate else None
        for m in _scope(db.query(CategoryTaxMap), CategoryTaxMap, claims, org_slug).all()
    }

    return {
        "categories": [
            _category_to_dict(c, counts.get(c.slug, 0), tax_map.get(c.slug))
            for c in cats
        ]
    }


class CreateCategoryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    slug: Optional[str] = Field(default=None, max_length=80)
    name: str = Field(min_length=1, max_length=200)
    description: Optional[str] = None
    icon: Optional[str] = None
    sort_order: Optional[int] = 0


@router.post("/orgs/{org_slug}/categories", status_code=201)
def create_category(
    org_slug: str,
    body: CreateCategoryRequest,
    claims: StrategosClaims = Depends(require_claims),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    access_guard(org_slug, claims)
    require_role(claims, "client_owner")

    target_org = org_slug if claims.role == "strategos_admin" else claims.org_slug
    slug = body.slug or _slugify(body.name)
    existing = db.query(Category).filter_by(organization_slug=target_org, slug=slug).first()
    if existing:
        raise HTTPException(status_code=409, detail=f"Category '{slug}' already exists")

    c = Category(
        organization_slug=target_org,
        slug=slug,
        name=body.name,
        description=body.description,
        icon=body.icon,
        sort_order=body.sort_order or 0,
    )
    db.add(c)
    db.commit()
    db.refresh(c)
    return {"category": _category_to_dict(c, 0, None)}


class UpdateCategoryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: Optional[str] = None
    description: Optional[str] = None
    icon: Optional[str] = None
    sort_order: Optional[int] = None


@router.patch("/orgs/{org_slug}/categories/{cat_slug}")
def update_category(
    org_slug: str,
    cat_slug: str,
    body: UpdateCategoryRequest,
    claims: StrategosClaims = Depends(require_claims),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    access_guard(org_slug, claims)
    require_role(claims, "client_owner")

    c = _scope(db.query(Category), Category, claims, org_slug).filter(Category.slug == cat_slug).first()
    if not c:
        raise HTTPException(status_code=404, detail="Category not found")

    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(c, k, v)
    db.commit()
    db.refresh(c)

    count = (
        _scope(db.query(func.count(Product.id)), Product, claims, org_slug)
        .filter(Product.category == cat_slug)
        .scalar()
        or 0
    )
    tax_name = None
    mapping = (
        _scope(db.query(CategoryTaxMap), CategoryTaxMap, claims, org_slug)
        .filter(CategoryTaxMap.category == cat_slug)
        .first()
    )
    if mapping and mapping.tax_rate:
        tax_name = mapping.tax_rate.name
    return {"category": _category_to_dict(c, int(count), tax_name)}


@router.delete("/orgs/{org_slug}/categories/{cat_slug}", status_code=204)
def delete_category(
    org_slug: str,
    cat_slug: str,
    claims: StrategosClaims = Depends(require_claims),
    db: Session = Depends(get_db),
):
    access_guard(org_slug, claims)
    require_role(claims, "client_owner")

    c = _scope(db.query(Category), Category, claims, org_slug).filter(Category.slug == cat_slug).first()
    if not c:
        raise HTTPException(status_code=404, detail="Category not found")

    count = (
        _scope(db.query(func.count(Product.id)), Product, claims, org_slug)
        .filter(Product.category == cat_slug)
        .scalar()
        or 0
    )
    if count:
        raise HTTPException(
            status_code=400,
            detail=f"Category has {count} products. Move or delete them first.",
        )

    db.delete(c)
    db.commit()


# ============================================================================
# Products + tiers
# ============================================================================


def _product_to_dict(p: Product, tiers: list[PriceTier]) -> dict[str, Any]:
    return {
        "id": p.id,
        "key": p.key,
        "name": p.name,
        "category": p.category,
        "description": p.description,
        "notes": p.notes,
        "pricing_unit": p.pricing_unit,
        "price_per": p.price_per,
        "pricing_strategy": p.pricing_strategy,
        "metric_unit": p.metric_unit,
        "image_url": p.image_url,
        "double_sided_surcharge": bool(p.double_sided_surcharge),
        "unit_price": p.unit_price,
        "bulk_price": p.bulk_price,
        "bulk_threshold": p.bulk_threshold,
        "min_qty": p.min_qty,
        "tiers": [
            {"id": t.id, "spec_key": t.spec_key, "quantity": t.quantity, "price": t.price}
            for t in tiers
        ],
    }


def _load_product_or_404(db: Session, claims: StrategosClaims, org_slug: str, product_id: int) -> Product:
    p = _scope(db.query(Product), Product, claims, org_slug).filter(Product.id == product_id).first()
    if not p:
        raise HTTPException(status_code=404, detail="Product not found")
    return p


@router.get("/orgs/{org_slug}/products")
def list_products(
    org_slug: str,
    category: Optional[str] = None,
    claims: StrategosClaims = Depends(require_claims),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    access_guard(org_slug, claims)
    q = _scope(db.query(Product), Product, claims, org_slug)
    if category:
        q = q.filter(Product.category == category)
    products = q.order_by(Product.category, Product.name).all()

    out: list[dict[str, Any]] = []
    for p in products:
        tiers = (
            db.query(PriceTier)
            .filter_by(product_id=p.id)
            .order_by(PriceTier.spec_key, PriceTier.quantity)
            .all()
        )
        out.append(_product_to_dict(p, tiers))
    return {"products": out}


class CreateProductRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    key: Optional[str] = Field(default=None, max_length=80)
    category: str = Field(min_length=1, max_length=80)
    description: Optional[str] = None
    notes: Optional[str] = None
    pricing_strategy: str = Field(default="tiered")
    metric_unit: Optional[str] = None
    pricing_unit: Optional[str] = None
    price_per: Optional[str] = None
    image_url: Optional[str] = None
    double_sided_surcharge: bool = True
    unit_price: Optional[float] = None
    bulk_price: Optional[float] = None
    bulk_threshold: Optional[int] = None
    min_qty: Optional[int] = 1

    @field_validator("pricing_strategy")
    @classmethod
    def _check_strategy(cls, v: str) -> str:
        if v not in PRICING_STRATEGIES:
            raise ValueError(f"pricing_strategy must be one of {PRICING_STRATEGIES}")
        return v


@router.post("/orgs/{org_slug}/products", status_code=201)
def create_product(
    org_slug: str,
    body: CreateProductRequest,
    claims: StrategosClaims = Depends(require_claims),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    access_guard(org_slug, claims)
    require_role(claims, "client_owner")

    target_org = org_slug if claims.role == "strategos_admin" else claims.org_slug
    key = body.key or _slugify(body.name)

    existing = (
        db.query(Product)
        .filter_by(organization_slug=target_org, key=key)
        .first()
    )
    if existing:
        raise HTTPException(status_code=409, detail=f"Product with key '{key}' already exists")

    # Auto-create category row if missing so it shows up in the UI
    existing_cat = db.query(Category).filter_by(
        organization_slug=target_org, slug=body.category,
    ).first()
    if not existing_cat:
        db.add(Category(
            organization_slug=target_org,
            slug=body.category,
            name=_humanize(body.category),
        ))

    p = Product(
        organization_slug=target_org,
        key=key,
        name=body.name,
        category=body.category,
        description=body.description,
        notes=body.notes,
        pricing_strategy=body.pricing_strategy,
        metric_unit=body.metric_unit,
        pricing_unit=body.pricing_unit,
        price_per=body.price_per,
        image_url=body.image_url,
        double_sided_surcharge=body.double_sided_surcharge,
        unit_price=body.unit_price,
        bulk_price=body.bulk_price,
        bulk_threshold=body.bulk_threshold,
        min_qty=body.min_qty or 1,
    )
    db.add(p)
    db.commit()
    db.refresh(p)
    return {"product": _product_to_dict(p, [])}


class UpdateProductRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Optional[str] = None
    description: Optional[str] = None
    notes: Optional[str] = None
    category: Optional[str] = None
    pricing_strategy: Optional[str] = None
    metric_unit: Optional[str] = None
    image_url: Optional[str] = None
    double_sided_surcharge: Optional[bool] = None
    unit_price: Optional[float] = None
    bulk_price: Optional[float] = None
    bulk_threshold: Optional[int] = None
    min_qty: Optional[int] = None
    pricing_unit: Optional[str] = None
    price_per: Optional[str] = None

    @field_validator("pricing_strategy")
    @classmethod
    def _check_strategy(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in PRICING_STRATEGIES:
            raise ValueError(f"pricing_strategy must be one of {PRICING_STRATEGIES}")
        return v


@router.patch("/orgs/{org_slug}/products/{product_id}")
def update_product(
    org_slug: str,
    product_id: int,
    body: UpdateProductRequest,
    claims: StrategosClaims = Depends(require_claims),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    access_guard(org_slug, claims)
    require_role(claims, "client_owner")

    p = _load_product_or_404(db, claims, org_slug, product_id)
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(p, k, v)
    db.commit()
    db.refresh(p)

    tiers = db.query(PriceTier).filter_by(product_id=p.id).all()
    return {"product": _product_to_dict(p, tiers)}


@router.delete("/orgs/{org_slug}/products/{product_id}", status_code=204)
def delete_product(
    org_slug: str,
    product_id: int,
    claims: StrategosClaims = Depends(require_claims),
    db: Session = Depends(get_db),
):
    access_guard(org_slug, claims)
    require_role(claims, "client_owner")
    p = _load_product_or_404(db, claims, org_slug, product_id)
    db.delete(p)
    db.commit()


class CreateTierRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    spec_key: str = ""
    quantity: int = Field(gt=0)
    price: float = Field(ge=0)


@router.post("/orgs/{org_slug}/products/{product_id}/tiers", status_code=201)
def create_tier(
    org_slug: str,
    product_id: int,
    body: CreateTierRequest,
    claims: StrategosClaims = Depends(require_claims),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    access_guard(org_slug, claims)
    require_role(claims, "client_owner")
    p = _load_product_or_404(db, claims, org_slug, product_id)

    existing = (
        db.query(PriceTier)
        .filter_by(product_id=p.id, spec_key=body.spec_key, quantity=body.quantity)
        .first()
    )
    if existing:
        raise HTTPException(status_code=409, detail="Tier with that spec/quantity already exists")

    t = PriceTier(
        organization_slug=p.organization_slug,
        product_id=p.id,
        spec_key=body.spec_key,
        quantity=body.quantity,
        price=body.price,
    )
    db.add(t)
    db.commit()
    db.refresh(p)
    tiers = db.query(PriceTier).filter_by(product_id=p.id).all()
    return {"product": _product_to_dict(p, tiers)}


class UpdateTierRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    price: Optional[float] = Field(default=None, ge=0)
    quantity: Optional[int] = Field(default=None, gt=0)
    spec_key: Optional[str] = None


@router.patch("/orgs/{org_slug}/products/{product_id}/tiers/{tier_id}")
def update_tier(
    org_slug: str,
    product_id: int,
    tier_id: int,
    body: UpdateTierRequest,
    claims: StrategosClaims = Depends(require_claims),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    access_guard(org_slug, claims)
    require_role(claims, "client_owner")
    p = _load_product_or_404(db, claims, org_slug, product_id)
    tier = db.query(PriceTier).filter_by(id=tier_id, product_id=p.id).first()
    if not tier:
        raise HTTPException(status_code=404, detail="Tier not found")
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(tier, k, v)
    db.commit()
    tiers = db.query(PriceTier).filter_by(product_id=p.id).all()
    return {"product": _product_to_dict(p, tiers)}


@router.delete("/orgs/{org_slug}/products/{product_id}/tiers/{tier_id}", status_code=204)
def delete_tier(
    org_slug: str,
    product_id: int,
    tier_id: int,
    claims: StrategosClaims = Depends(require_claims),
    db: Session = Depends(get_db),
):
    access_guard(org_slug, claims)
    require_role(claims, "client_owner")
    p = _load_product_or_404(db, claims, org_slug, product_id)
    tier = db.query(PriceTier).filter_by(id=tier_id, product_id=p.id).first()
    if not tier:
        raise HTTPException(status_code=404, detail="Tier not found")
    db.delete(tier)
    db.commit()


# ============================================================================
# Tax rates + category map
# ============================================================================


def _tax_rate_to_dict(t: TaxRate) -> dict[str, Any]:
    return {
        "id": t.id,
        "name": t.name,
        "rate": t.rate,
        "description": t.description,
        "is_default": bool(t.is_default),
    }


@router.get("/orgs/{org_slug}/tax-rates")
def list_tax_rates(
    org_slug: str,
    claims: StrategosClaims = Depends(require_claims),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    access_guard(org_slug, claims)
    rates = _scope(db.query(TaxRate), TaxRate, claims, org_slug).order_by(TaxRate.name).all()
    mappings = _scope(db.query(CategoryTaxMap), CategoryTaxMap, claims, org_slug).all()
    return {
        "tax_rates": [_tax_rate_to_dict(r) for r in rates],
        "category_map": [
            {"category": m.category, "tax_rate_id": m.tax_rate_id} for m in mappings
        ],
    }


class CreateTaxRateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=60)
    rate: float = Field(ge=0, le=1)
    description: Optional[str] = None
    is_default: bool = False


@router.post("/orgs/{org_slug}/tax-rates", status_code=201)
def create_tax_rate(
    org_slug: str,
    body: CreateTaxRateRequest,
    claims: StrategosClaims = Depends(require_claims),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    access_guard(org_slug, claims)
    require_role(claims, "client_owner")
    target_org = org_slug if claims.role == "strategos_admin" else claims.org_slug

    if body.is_default:
        # ensure only one default
        for r in db.query(TaxRate).filter_by(organization_slug=target_org, is_default=True).all():
            r.is_default = False

    t = TaxRate(
        organization_slug=target_org,
        name=body.name,
        rate=body.rate,
        description=body.description,
        is_default=body.is_default,
    )
    db.add(t)
    db.commit()
    db.refresh(t)
    return {"tax_rate": _tax_rate_to_dict(t)}


class UpdateTaxRateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: Optional[str] = None
    rate: Optional[float] = Field(default=None, ge=0, le=1)
    description: Optional[str] = None
    is_default: Optional[bool] = None


@router.patch("/orgs/{org_slug}/tax-rates/{tax_id}")
def update_tax_rate(
    org_slug: str,
    tax_id: int,
    body: UpdateTaxRateRequest,
    claims: StrategosClaims = Depends(require_claims),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    access_guard(org_slug, claims)
    require_role(claims, "client_owner")
    t = _scope(db.query(TaxRate), TaxRate, claims, org_slug).filter(TaxRate.id == tax_id).first()
    if not t:
        raise HTTPException(status_code=404, detail="Tax rate not found")

    updates = body.model_dump(exclude_unset=True)
    if updates.get("is_default"):
        for r in db.query(TaxRate).filter_by(organization_slug=t.organization_slug, is_default=True).all():
            r.is_default = False
    for k, v in updates.items():
        setattr(t, k, v)
    db.commit()
    db.refresh(t)
    return {"tax_rate": _tax_rate_to_dict(t)}


@router.delete("/orgs/{org_slug}/tax-rates/{tax_id}", status_code=204)
def delete_tax_rate(
    org_slug: str,
    tax_id: int,
    claims: StrategosClaims = Depends(require_claims),
    db: Session = Depends(get_db),
):
    access_guard(org_slug, claims)
    require_role(claims, "client_owner")
    t = _scope(db.query(TaxRate), TaxRate, claims, org_slug).filter(TaxRate.id == tax_id).first()
    if not t:
        raise HTTPException(status_code=404, detail="Tax rate not found")
    if t.is_default:
        raise HTTPException(status_code=400, detail="Cannot delete the default tax rate")
    db.delete(t)
    db.commit()


class CategoryTaxMapEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    category: str = Field(min_length=1, max_length=80)
    tax_rate_id: int


class BulkCategoryTaxMapRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entries: list[CategoryTaxMapEntry]


@router.put("/orgs/{org_slug}/category-tax-map")
def bulk_set_category_tax_map(
    org_slug: str,
    body: BulkCategoryTaxMapRequest,
    claims: StrategosClaims = Depends(require_claims),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    access_guard(org_slug, claims)
    require_role(claims, "client_owner")
    target_org = org_slug if claims.role == "strategos_admin" else claims.org_slug

    valid_rate_ids = {
        r.id for r in db.query(TaxRate).filter_by(organization_slug=target_org).all()
    }

    for entry in body.entries:
        if entry.tax_rate_id not in valid_rate_ids:
            raise HTTPException(
                status_code=400,
                detail=f"tax_rate_id {entry.tax_rate_id} doesn't belong to org {target_org}",
            )
        existing = (
            db.query(CategoryTaxMap)
            .filter_by(organization_slug=target_org, category=entry.category)
            .first()
        )
        if existing:
            existing.tax_rate_id = entry.tax_rate_id
        else:
            db.add(CategoryTaxMap(
                organization_slug=target_org,
                category=entry.category,
                tax_rate_id=entry.tax_rate_id,
            ))
    db.commit()
    return {"ok": True}


# ============================================================================
# Surcharges
# ============================================================================


def _surcharge_to_dict(s: SurchargeRule) -> dict[str, Any]:
    return {
        "id": s.id,
        "name": s.name,
        "multiplier": s.multiplier,
        "kind": s.kind,
        "applies_to_category": s.applies_to_category,
        "description": s.description,
    }


@router.get("/orgs/{org_slug}/surcharges")
def list_surcharges(
    org_slug: str,
    claims: StrategosClaims = Depends(require_claims),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    access_guard(org_slug, claims)
    rows = _scope(db.query(SurchargeRule), SurchargeRule, claims, org_slug).order_by(SurchargeRule.name).all()
    return {"surcharges": [_surcharge_to_dict(s) for s in rows]}


class CreateSurchargeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=60)
    multiplier: float
    kind: str = Field(default="multiplier")
    applies_to_category: Optional[str] = None
    description: Optional[str] = None

    @field_validator("kind")
    @classmethod
    def _check_kind(cls, v: str) -> str:
        if v not in ("multiplier", "additive"):
            raise ValueError("kind must be 'multiplier' or 'additive'")
        return v


@router.post("/orgs/{org_slug}/surcharges", status_code=201)
def create_surcharge(
    org_slug: str,
    body: CreateSurchargeRequest,
    claims: StrategosClaims = Depends(require_claims),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    access_guard(org_slug, claims)
    require_role(claims, "client_owner")
    target_org = org_slug if claims.role == "strategos_admin" else claims.org_slug

    if db.query(SurchargeRule).filter_by(organization_slug=target_org, name=body.name).first():
        raise HTTPException(status_code=409, detail="Surcharge with that name already exists")

    s = SurchargeRule(
        organization_slug=target_org,
        name=body.name,
        multiplier=body.multiplier,
        kind=body.kind,
        applies_to_category=body.applies_to_category,
        description=body.description,
    )
    db.add(s)
    db.commit()
    db.refresh(s)
    return {"surcharge": _surcharge_to_dict(s)}


class UpdateSurchargeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: Optional[str] = None
    multiplier: Optional[float] = None
    kind: Optional[str] = None
    applies_to_category: Optional[str] = None
    description: Optional[str] = None


@router.patch("/orgs/{org_slug}/surcharges/{surcharge_id}")
def update_surcharge(
    org_slug: str,
    surcharge_id: int,
    body: UpdateSurchargeRequest,
    claims: StrategosClaims = Depends(require_claims),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    access_guard(org_slug, claims)
    require_role(claims, "client_owner")
    s = _scope(db.query(SurchargeRule), SurchargeRule, claims, org_slug).filter(SurchargeRule.id == surcharge_id).first()
    if not s:
        raise HTTPException(status_code=404, detail="Surcharge not found")
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(s, k, v)
    db.commit()
    db.refresh(s)
    return {"surcharge": _surcharge_to_dict(s)}


@router.delete("/orgs/{org_slug}/surcharges/{surcharge_id}", status_code=204)
def delete_surcharge(
    org_slug: str,
    surcharge_id: int,
    claims: StrategosClaims = Depends(require_claims),
    db: Session = Depends(get_db),
):
    access_guard(org_slug, claims)
    require_role(claims, "client_owner")
    s = _scope(db.query(SurchargeRule), SurchargeRule, claims, org_slug).filter(SurchargeRule.id == surcharge_id).first()
    if not s:
        raise HTTPException(status_code=404, detail="Surcharge not found")
    db.delete(s)
    db.commit()


# ============================================================================
# Settings
# ============================================================================


def _setting_to_dict(s: Setting) -> dict[str, Any]:
    """
    Serialize a Setting for the API response. SECRET KEYS are masked
    here — the raw value never leaves the server. See settings_security
    for the allowlist + rationale.
    """
    from settings_security import mask_value
    return {
        "key": s.key,
        "value": mask_value(s.key, s.value),
        "value_type": s.value_type,
        "description": s.description,
    }


@router.get("/orgs/{org_slug}/settings")
def list_settings(
    org_slug: str,
    claims: StrategosClaims = Depends(require_claims),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    access_guard(org_slug, claims)
    rows = _scope(db.query(Setting), Setting, claims, org_slug).order_by(Setting.key).all()
    return {"settings": [_setting_to_dict(r) for r in rows]}


class UpdateSettingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: str
    # Optional — only consulted when a fresh row is being created (upsert).
    # For existing rows the current value_type is preserved so we can keep
    # validating floats/ints against their declared type.
    value_type: str | None = None


@router.patch("/orgs/{org_slug}/settings/{key}")
def update_setting(
    org_slug: str,
    key: str,
    body: UpdateSettingRequest,
    claims: StrategosClaims = Depends(require_claims),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """
    Upsert a tenant-scoped setting.

    Existing rows keep their declared `value_type` and are type-validated.
    Missing rows are created — handy for new V5 keys like `widget_accents`
    or `widget_stripe_mode` that may not have been seeded yet.
    """
    access_guard(org_slug, claims)
    require_role(claims, "client_owner")
    # Block the round-trip footgun: if the dashboard fetched a masked secret
    # and saves the form without retyping, body.value would be "********".
    # We refuse to overwrite a real secret with the mask string.
    from settings_security import is_secret, is_mask
    if is_secret(key) and is_mask(body.value):
        # No-op: return current state without touching the row
        existing = (
            _scope(db.query(Setting), Setting, claims, org_slug)
            .filter(Setting.key == key)
            .first()
        )
        if existing is None:
            raise HTTPException(status_code=400, detail="cannot save mask as new secret")
        return {"setting": _setting_to_dict(existing)}

    s = _scope(db.query(Setting), Setting, claims, org_slug).filter(Setting.key == key).first()

    if s is None:
        # Create-on-write. Default to "string" if the caller didn't declare
        # a type. JSON values are validated below just like for existing rows.
        vt = body.value_type or "string"
        if vt not in ("string", "float", "int", "json"):
            raise HTTPException(status_code=400, detail=f"invalid value_type '{vt}'")
        s = Setting(
            organization_slug=org_slug,
            key=key,
            value=body.value,
            value_type=vt,
            description=None,
        )
        db.add(s)

    if s.value_type == "float":
        try:
            float(body.value)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"'{key}' must be a number")
    elif s.value_type == "int":
        try:
            int(body.value)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"'{key}' must be an integer")
    elif s.value_type == "json":
        import json
        try:
            json.loads(body.value)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"'{key}' must be valid JSON")

    # Encrypt at rest if this is a secret key. Non-secret values are
    # written as-is (no envelope) so JSON / float casts elsewhere keep
    # working. See secrets_crypto for the threat model.
    if is_secret(key):
        from secrets_crypto import encrypt
        s.value = encrypt(body.value)
    else:
        s.value = body.value
    db.commit()
    db.refresh(s)
    return {"setting": _setting_to_dict(s)}


# ============================================================================
# Quotes
# ============================================================================


def _quote_to_dict(q: Quote) -> dict[str, Any]:
    return {
        "id": q.id,
        "conversation_id": q.conversation_id,
        "product_key": q.product_key,
        "specs": q.specs,
        "base_price": q.base_price,
        "surcharges": q.surcharges,
        "final_price_ex_vat": q.final_price_ex_vat,
        "vat_amount": q.vat_amount,
        "final_price_inc_vat": q.final_price_inc_vat,
        "artwork_cost": q.artwork_cost,
        "total": q.total,
        "status": q.status,
        "approved_by": q.approved_by,
        "notes": q.notes,
        "created_at": q.created_at.isoformat() if q.created_at else None,
        # PrintLogic integration state — dashboard renders a badge per state
        "printlogic_order_id": getattr(q, "printlogic_order_id", None),
        "printlogic_customer_id": getattr(q, "printlogic_customer_id", None),
        "printlogic_pushed_at": (
            q.printlogic_pushed_at.isoformat()
            if getattr(q, "printlogic_pushed_at", None) else None
        ),
        "printlogic_last_error": getattr(q, "printlogic_last_error", None),
        "printlogic_push_attempts": getattr(q, "printlogic_push_attempts", 0) or 0,
        # Stripe payment link state — dashboard renders a badge per state
        "stripe_payment_link_id": getattr(q, "stripe_payment_link_id", None),
        "stripe_payment_link_url": getattr(q, "stripe_payment_link_url", None),
        "stripe_payment_status": getattr(q, "stripe_payment_status", None),
        "stripe_paid_at": (
            q.stripe_paid_at.isoformat()
            if getattr(q, "stripe_paid_at", None) else None
        ),
        "stripe_last_error": getattr(q, "stripe_last_error", None),
    }


@router.get("/orgs/{org_slug}/quotes")
def list_quotes(
    org_slug: str,
    status: Optional[str] = Query(None),
    channel: Optional[str] = Query(None),
    limit: int = Query(100, le=500),
    claims: StrategosClaims = Depends(require_claims),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    access_guard(org_slug, claims)
    q = _scope(db.query(Quote), Quote, claims, org_slug)
    if status:
        q = q.filter(Quote.status == status)
    if channel:
        q = q.join(Conversation, Quote.conversation_id == Conversation.id).filter(Conversation.channel == channel)
    rows = q.order_by(Quote.created_at.desc()).limit(limit).all()
    return {"quotes": [_quote_to_dict(r) for r in rows]}


class UpdateQuoteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: str = Field(..., pattern=r"^(pending_approval|approved|sent|accepted|rejected)$")
    notes: Optional[str] = None


@router.patch("/orgs/{org_slug}/quotes/{quote_id}")
def update_quote(
    org_slug: str,
    quote_id: int,
    body: UpdateQuoteRequest,
    claims: StrategosClaims = Depends(require_claims),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    access_guard(org_slug, claims)
    require_role(claims, "client_member")
    q = _scope(db.query(Quote), Quote, claims, org_slug).filter(Quote.id == quote_id).first()
    if not q:
        raise HTTPException(status_code=404, detail="Quote not found")
    q.status = body.status
    if body.status in ("approved", "rejected"):
        q.approved_by = claims.email
    if body.notes is not None:
        q.notes = body.notes
    db.commit()
    db.refresh(q)
    return {"quote": _quote_to_dict(q)}


@router.post("/orgs/{org_slug}/quotes/{quote_id}/push-to-printlogic")
def push_quote_to_printlogic(
    org_slug: str,
    quote_id: int,
    claims: StrategosClaims = Depends(require_claims),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """
    Push a quote to the tenant's PrintLogic account.

    Honors the tenant-level `printlogic_dry_run` Setting:
      - `"true"` (default)  → returns a synthetic `DRY-xxxx` order_id,
        zero real network traffic to PrintLogic.
      - `"false"`           → real POST `create_order`, updates the
        Quote row with the returned real `order_id`.

    Idempotent: calling twice on a Quote that already has a real
    PrintLogic order returns the existing id without re-pushing.

    Requires `client_owner` (same role gate as settings edits) — we
    don't want random viewers firing destructive pushes.
    """
    access_guard(org_slug, claims)
    require_role(claims, "client_owner")
    q = _scope(db.query(Quote), Quote, claims, org_slug).filter(Quote.id == quote_id).first()
    if not q:
        raise HTTPException(status_code=404, detail="Quote not found")

    from printlogic_push import push_quote
    result = push_quote(db, q, org_slug)
    db.commit()
    db.refresh(q)
    return {
        "quote": _quote_to_dict(q),
        "result": result,
    }


@router.post("/orgs/{org_slug}/quotes/{quote_id}/cancel-printlogic")
def cancel_printlogic_order(
    org_slug: str,
    quote_id: int,
    claims: StrategosClaims = Depends(require_claims),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """
    Rollback path — ask PrintLogic to mark a pushed order as Cancelled.
    Required when a real push happened by mistake. If PrintLogic refuses
    the cancellation (their UI may have already moved the order into
    production), Justin deletes manually from his side.

    If the order_id starts with `DRY-`, we just clear the local row —
    there's nothing upstream to cancel.
    """
    access_guard(org_slug, claims)
    require_role(claims, "client_owner")
    q = _scope(db.query(Quote), Quote, claims, org_slug).filter(Quote.id == quote_id).first()
    if not q:
        raise HTTPException(status_code=404, detail="Quote not found")

    from printlogic_push import cancel_pushed_order
    result = cancel_pushed_order(db, q, org_slug)
    if result.get("ok"):
        # Clear the local ids so the "Push" button becomes available again
        q.printlogic_order_id = None
        q.printlogic_customer_id = None
        q.printlogic_pushed_at = None
        q.printlogic_last_error = None
    db.commit()
    db.refresh(q)
    return {
        "quote": _quote_to_dict(q),
        "result": result,
    }


# ============================================================================
# Integrations health / status
# ============================================================================


@router.get("/orgs/{org_slug}/integrations/status")
def integrations_status(
    org_slug: str,
    claims: StrategosClaims = Depends(require_claims),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """
    Health summary per integration (Missive, PrintLogic, Stripe). Read-only.

    Returns one block per integration with `configured` / `enabled` /
    `health` (green|yellow|red|unknown) / `last_success_at` / `last_error`
    / `stats_30d`. The dashboard renders this as a card on the Overview
    tab plus colored pills inside each Connections sub-tab.

    Cheap to call (3-4 indexed COUNTs + 3 ORDER BY DESC LIMIT 1) — fine to
    poll every 30 seconds from the dashboard.
    """
    access_guard(org_slug, claims)
    require_role(claims, "client_member")
    from integrations_status import compute_integration_status
    return compute_integration_status(db, org_slug)


# ============================================================================
# Connection tests — validate stored credentials without side effects
# ============================================================================


@router.post("/orgs/{org_slug}/integrations/printlogic/test")
def test_printlogic_connection(
    org_slug: str,
    claims: StrategosClaims = Depends(require_claims),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """
    Read-only smoke test of the stored PrintLogic api_key. Calls
    `find_customer` with a sentinel email — if PrintLogic returns 200
    (whether or not the customer exists), auth + firm-binding are good.

    Returns `{ok, message, raw}`. Never writes to PrintLogic. Never
    crashes — wraps printlogic.find_customer's already-safe return.
    """
    access_guard(org_slug, claims)
    require_role(claims, "client_owner")

    from pricing_engine import _get_setting
    api_key = _get_setting(db, "printlogic_api_key", default="", organization_slug=org_slug)
    if not api_key:
        return {"ok": False, "message": "no api_key set", "raw": None}

    import asyncio
    import printlogic
    try:
        result = asyncio.run(printlogic.find_customer(
            api_key, email="ping-from-craig-test@example.invalid"
        ))
    except Exception as e:
        return {"ok": False, "message": f"crashed: {type(e).__name__}", "raw": None}

    if result.get("error"):
        # network / auth failure
        return {
            "ok": False,
            "message": f"PrintLogic rejected the key: {result['error']}",
            "raw": result.get("raw"),
        }
    # ok=True with customer=None is the expected "auth works, no such customer"
    # response — that's a green light.
    return {
        "ok": True,
        "message": "PrintLogic auth confirmed (api_key + firm binding valid)",
        "raw": None,
    }


@router.post("/orgs/{org_slug}/integrations/stripe/test")
def test_stripe_connection(
    org_slug: str,
    claims: StrategosClaims = Depends(require_claims),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """
    Validate the Connect linkage by calling the lightest Stripe endpoint
    (`GET /v1/account`) on behalf of the connected tenant.

    200 = the OAuth token still works AND the platform key is healthy.
    401 = Stripe revoked us OR the platform key was rotated / invalidated.

    Does NOT validate the platform-level webhook secret — that only
    proves itself when Stripe actually delivers a signed event. The
    Connect status endpoint reports whether it's set in env.
    """
    import stripe_connect
    access_guard(org_slug, claims)
    require_role(claims, "client_owner")

    from pricing_engine import _get_setting
    account_id = _get_setting(db, "stripe_account_id", default="", organization_slug=org_slug)

    if not account_id:
        return {"ok": False, "message": "Not connected. Click 'Connect with Stripe' to start the OAuth flow."}
    if not stripe_connect.is_configured():
        return {"ok": False, "message": "Server-side Stripe Connect platform credentials are missing."}

    # Lightweight whoami call against the connected account.
    import httpx
    try:
        r = httpx.get(
            "https://api.stripe.com/v1/account",
            auth=(stripe_connect.PLATFORM_KEY, ""),
            headers={"Stripe-Account": account_id},
            timeout=httpx.Timeout(10.0, connect=5.0),
        )
    except httpx.HTTPError as e:
        return {"ok": False, "message": f"network: {type(e).__name__}"}

    if r.status_code == 401:
        return {"ok": False, "message": "Stripe rejected the connection (401). The user may have revoked access — try Disconnect + Connect again."}
    if r.status_code >= 400:
        try:
            err = r.json().get("error", {}).get("message", "unknown")
        except Exception:
            err = r.text[:200]
        return {"ok": False, "message": f"Stripe error: {err}"}

    try:
        info = r.json()
        country = info.get("country")
    except Exception:
        return {"ok": False, "message": "malformed response"}

    msg = f"Connected to {account_id} ({country})"
    whsec = stripe_connect.PLATFORM_WEBHOOK_SECRET
    if not whsec:
        msg += " — platform webhook secret not yet configured (server-side env var)"
    return {"ok": True, "message": msg, "account_id": account_id, "webhook_secret_set": bool(whsec)}


@router.post("/orgs/{org_slug}/integrations/missive/test")
def test_missive_connection(
    org_slug: str,
    claims: StrategosClaims = Depends(require_claims),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """
    Validate the stored Missive API token by calling `GET /v1/users/me`
    (cheapest authed endpoint). 200 = token works.
    """
    access_guard(org_slug, claims)
    require_role(claims, "client_owner")

    from pricing_engine import _get_setting
    token = _get_setting(db, "missive_api_token", default="", organization_slug=org_slug)
    if not token:
        return {"ok": False, "message": "no missive_api_token set"}

    import httpx
    try:
        r = httpx.get(
            "https://public.missiveapp.com/v1/users",
            headers={"Authorization": f"Bearer {token}"},
            timeout=httpx.Timeout(10.0, connect=5.0),
        )
    except httpx.HTTPError as e:
        return {"ok": False, "message": f"network: {type(e).__name__}"}

    if r.status_code == 401:
        return {"ok": False, "message": "Missive rejected the token (401 unauthorized)"}
    if r.status_code >= 400:
        return {"ok": False, "message": f"Missive HTTP {r.status_code}: {r.text[:200]}"}

    return {"ok": True, "message": "Missive token validated"}


# ============================================================================
# Sentinel / smoke-test quote creation
# ============================================================================


class SentinelQuoteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    amount_inc_vat: float = Field(..., gt=0, le=100,
                                  description="Total inc VAT — small (1-100€) for safe testing")
    note: str = Field("[SENTINEL-TEST]",
                      description="Visible note so anyone can spot test quotes vs real ones")


@router.post("/orgs/{org_slug}/sentinel-quote")
def create_sentinel_quote(
    org_slug: str,
    body: SentinelQuoteRequest,
    claims: StrategosClaims = Depends(require_claims),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """
    Create a Quote with an arbitrary small amount, bypassing Craig's
    pricing engine. Strictly for smoke testing the Stripe Payment Link
    pipeline (or future end-to-end pipelines) when the real catalog
    minimums would force a higher amount.

    Auth: strategos_admin only — this endpoint sidesteps the pricing
    engine, so we don't want client_owners using it accidentally.

    Limits:
      - amount_inc_vat must be > 0 and ≤ €100 (safety cap)
      - Status starts as 'approved' so it's immediately Payment Link-able
      - Note prefix '[SENTINEL-TEST]' goes into both quote.notes AND the
        product description on Stripe — the customer sees that this is a
        test charge before paying.
    """
    access_guard(org_slug, claims)
    require_role(claims, "strategos_admin")

    # Compute price split (assume 23% VAT for safety — services/large format
    # rate; printed-matter is 13.5% but we don't know the product). Inc-VAT
    # is what the user asked for, so we work backwards.
    inc = float(body.amount_inc_vat)
    vat_rate = 0.23
    ex = round(inc / (1 + vat_rate), 2)
    vat = round(inc - ex, 2)

    q = Quote(
        organization_slug=org_slug,
        product_key="sentinel_test",
        specs={
            "sentinel": True,
            "note": body.note,
            "created_via": "POST /quotes/sentinel",
        },
        base_price=ex,
        surcharges=[],
        final_price_ex_vat=ex,
        vat_amount=vat,
        final_price_inc_vat=inc,
        artwork_cost=0.0,
        total=inc,
        status="approved",
        approved_by=claims.email,
        notes=body.note,
    )
    db.add(q)
    db.commit()
    db.refresh(q)
    return {"quote": _quote_to_dict(q)}


# ============================================================================
# Stripe payment links
# ============================================================================


@router.post("/orgs/{org_slug}/quotes/{quote_id}/create-payment-link")
def create_stripe_payment_link(
    org_slug: str,
    quote_id: int,
    claims: StrategosClaims = Depends(require_claims),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """
    Manually create a Stripe Payment Link for a quote. Mirrors the PrintLogic
    push endpoint's shape. Requires `client_owner` — only the client admin
    should be creating payment links.
    """
    access_guard(org_slug, claims)
    require_role(claims, "client_owner")
    q = _scope(db.query(Quote), Quote, claims, org_slug).filter(Quote.id == quote_id).first()
    if not q:
        raise HTTPException(status_code=404, detail="Quote not found")

    from stripe_push import create_link_for_quote
    result = create_link_for_quote(db, q, org_slug)
    db.commit()
    db.refresh(q)
    return {"quote": _quote_to_dict(q), "result": result}


@router.post("/orgs/{org_slug}/quotes/{quote_id}/cancel-payment-link")
def cancel_stripe_payment_link(
    org_slug: str,
    quote_id: int,
    claims: StrategosClaims = Depends(require_claims),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """
    Deactivate a previously created Payment Link (Stripe won't let you delete,
    only flip active=false). Clears the local link fields so the dashboard
    shows "Create" again. Does NOT affect any payment that already went
    through — refunds go through Stripe's UI directly.
    """
    access_guard(org_slug, claims)
    require_role(claims, "client_owner")
    q = _scope(db.query(Quote), Quote, claims, org_slug).filter(Quote.id == quote_id).first()
    if not q:
        raise HTTPException(status_code=404, detail="Quote not found")

    if not q.stripe_payment_link_id:
        return {"quote": _quote_to_dict(q), "result": {"ok": True, "error": "no_link"}}

    import asyncio
    import stripe_client
    import stripe_connect
    from pricing_engine import _get_setting

    # Connect mode: deactivate via platform key + Stripe-Account header.
    # Falls back gracefully when not configured (returns no_api_key error).
    account_id = _get_setting(db, "stripe_account_id", default="", organization_slug=org_slug)
    api_key = stripe_connect.PLATFORM_KEY
    result = asyncio.run(stripe_client.deactivate_payment_link(
        api_key, q.stripe_payment_link_id, account_id=account_id or None,
    ))
    if result.get("ok"):
        q.stripe_payment_link_id = None
        q.stripe_payment_link_url = None
        # Preserve payment_status — if it was "paid" we don't want to forget that.
        if q.stripe_payment_status == "unpaid":
            q.stripe_payment_status = None
    db.commit()
    db.refresh(q)
    return {"quote": _quote_to_dict(q), "result": result}


# ============================================================================
# Stripe Connect — OAuth flow + platform-level webhook
# ============================================================================


@router.post("/orgs/{org_slug}/oauth/stripe/authorize-url")
def oauth_stripe_authorize_url(
    org_slug: str,
    request: Request,
    claims: StrategosClaims = Depends(require_claims),
) -> dict[str, Any]:
    """
    Returns a one-shot Stripe OAuth authorize URL the dashboard navigates
    the user to. The URL embeds a signed `state` (HMAC-SHA256 over
    `{org, exp:now+5min, nonce}`) so the callback can verify we initiated.

    POST + JWT-protected because the URL is single-use-ish: each call
    mints a fresh state with a 5-min TTL. The dashboard fetches this
    URL and does `window.location = response.url` so the browser
    naturally lands on Stripe's consent screen.

    Why not 302-redirect from a GET endpoint? Because cross-origin
    redirects from the dashboard wouldn't carry our Authorization
    header, so the backend couldn't verify the user is allowed to start
    a flow for this tenant.
    """
    import stripe_connect

    access_guard(org_slug, claims)
    require_role(claims, "client_owner")

    if not stripe_connect.is_configured():
        raise HTTPException(
            status_code=503,
            detail="stripe_connect_not_configured: platform creds missing on the server",
        )

    # Cloud Run terminates TLS at the load balancer — internally the
    # request reports `http://` even though the public URL is https.
    # Stripe Connect rejects http:// redirect_uri values, so we honor the
    # X-Forwarded-Proto header that the load balancer passes through, and
    # in production also force https as a belt-and-suspenders.
    redirect_uri = str(request.url_for("oauth_stripe_callback"))
    forwarded_proto = request.headers.get("x-forwarded-proto", "").lower()
    if forwarded_proto == "https" and redirect_uri.startswith("http://"):
        redirect_uri = "https://" + redirect_uri[len("http://"):]
    # Hard guard for production hostnames — never let an http:// slip
    # through to Stripe even if the proxy header is missing.
    if redirect_uri.startswith("http://") and ".run.app" in redirect_uri:
        redirect_uri = "https://" + redirect_uri[len("http://"):]

    try:
        url = stripe_connect.build_authorize_url(org_slug, redirect_uri)
    except ValueError as e:
        raise HTTPException(status_code=503, detail=str(e))

    return {"url": url, "expires_in_seconds": stripe_connect.STATE_TTL_SECONDS}


@router.get("/oauth/stripe/callback", name="oauth_stripe_callback")
async def oauth_stripe_callback(
    request: Request,
    code: Optional[str] = Query(None),
    state: Optional[str] = Query(None),
    error: Optional[str] = Query(None),
    error_description: Optional[str] = Query(None),
    db: Session = Depends(get_db),
) -> Any:
    """
    Stripe redirects the user here after they authorize on the consent
    screen (or cancel). We:

      1. Verify `state` (HMAC + expiry) — if tampered, 400 immediately
      2. Exchange `code` for tokens via Stripe OAuth API
      3. Persist 5 setting rows for the tenant (account_id, access_token
         encrypted, publishable_key, connected_at, user_email)
      4. Redirect back to the dashboard with a success/error query param
         the dashboard can react to

    No JWT — this is a public callback Stripe hits. The state signature
    is the security boundary: only WE could have signed a state with
    our JWT_SECRET, so a state that verifies came from a flow WE started.
    """
    from fastapi.responses import RedirectResponse
    import datetime as _dt
    import stripe_connect

    # User-facing redirect destination after we finish processing.
    # The dashboard origin comes from env (so prod uses agents.strategos-ai.com,
    # dev uses localhost). Fallback to the canonical prod URL.
    dashboard_base = os.environ.get(
        "STRATEGOS_DASHBOARD_URL", "https://agents.strategos-ai.com"
    ).rstrip("/")

    def _redirect(org_slug: str, params: dict[str, str]) -> RedirectResponse:
        from urllib.parse import urlencode
        target = f"{dashboard_base}/c/{org_slug}/a/craig/settings?{urlencode(params)}"
        return RedirectResponse(target, status_code=302)

    # User cancelled on Stripe's consent screen
    if error:
        # We don't know org_slug yet (state verification failed or wasn't tried).
        # Best-effort: try to recover org from state for a nicer redirect.
        org_slug = "unknown"
        if state:
            try:
                org_slug = stripe_connect.verify_state(state).get("org", "unknown")
            except stripe_connect.InvalidState:
                pass
        return _redirect(org_slug, {
            "stripe": "error",
            "msg": (error_description or error)[:200],
        })

    if not code or not state:
        raise HTTPException(status_code=400, detail="missing_code_or_state")

    # Verify state (raises InvalidState on tamper/expiry)
    try:
        parsed = stripe_connect.verify_state(state)
    except stripe_connect.InvalidState as e:
        raise HTTPException(status_code=400, detail=f"invalid_state:{e.reason}")

    org_slug = parsed["org"]

    # Exchange code for tokens
    result = await stripe_connect.exchange_code(code)
    if not result.get("ok"):
        return _redirect(org_slug, {
            "stripe": "error",
            "msg": str(result.get("error", "exchange_failed"))[:200],
        })

    # Persist — 5 Setting rows. Use _set_setting helper that respects encryption.
    from settings_security import is_secret
    from secrets_crypto import encrypt as _encrypt_secret

    def _upsert(key: str, value: str) -> None:
        row = (
            db.query(Setting)
            .filter_by(organization_slug=org_slug, key=key)
            .first()
        )
        # Encrypt only secret-keyed values (stripe_access_token is in SECRET_KEYS)
        stored_value = _encrypt_secret(value) if is_secret(key) and value else value
        if row:
            row.value = stored_value
        else:
            db.add(Setting(
                organization_slug=org_slug,
                key=key,
                value=stored_value,
                value_type="string",
            ))

    _upsert("stripe_account_id", result["account_id"])
    _upsert("stripe_access_token", result["access_token"])
    _upsert("stripe_publishable_key", result.get("publishable_key", ""))
    _upsert("stripe_connected_at", _dt.datetime.utcnow().isoformat(timespec="seconds"))
    # We don't get the email from the basic OAuth response — fetch it
    # right now via a /v1/account call (best-effort; failure non-fatal).
    user_email = ""
    try:
        import httpx as _httpx
        async with _httpx.AsyncClient(timeout=_httpx.Timeout(10.0, connect=5.0)) as c:
            r = await c.get(
                "https://api.stripe.com/v1/account",
                headers={"Stripe-Account": result["account_id"]},
                auth=(stripe_connect.PLATFORM_KEY, ""),
            )
            if r.status_code == 200:
                acct = r.json()
                user_email = (acct.get("email") or "")[:200]
    except Exception:
        pass
    _upsert("stripe_user_email", user_email)

    db.commit()

    return _redirect(org_slug, {"stripe": "connected"})


@router.post("/orgs/{org_slug}/oauth/stripe/disconnect")
async def oauth_stripe_disconnect(
    org_slug: str,
    claims: StrategosClaims = Depends(require_claims),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """
    Revoke our access to the connected Stripe account + clear the local
    setting rows. Does NOT cancel pending Payment Links (those stay
    valid; the tenant can manage them in Stripe directly). Does NOT
    affect already-paid quotes.

    Auth: client_owner. Returns the disconnect result + the cleared
    quote/setting state.
    """
    import stripe_connect
    access_guard(org_slug, claims)
    require_role(claims, "client_owner")

    from pricing_engine import _get_setting
    account_id = _get_setting(db, "stripe_account_id", default="", organization_slug=org_slug)

    if account_id:
        # Revoke Stripe-side. We tolerate "already disconnected" as success.
        revoke = await stripe_connect.deauthorize(account_id)
    else:
        revoke = {"ok": True, "note": "nothing_to_revoke"}

    # Clear local rows regardless of Stripe-side success — keeping stale
    # state would be worse than orphaned tokens (which expire anyway).
    cleared_keys = (
        "stripe_account_id", "stripe_access_token", "stripe_publishable_key",
        "stripe_connected_at", "stripe_user_email",
    )
    for key in cleared_keys:
        row = (
            db.query(Setting)
            .filter_by(organization_slug=org_slug, key=key)
            .first()
        )
        if row:
            row.value = ""
    db.commit()

    return {"ok": True, "revoke": revoke}


@router.get("/orgs/{org_slug}/integrations/stripe/connect-status")
def stripe_connect_status(
    org_slug: str,
    claims: StrategosClaims = Depends(require_claims),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """
    Lightweight read of the tenant's Connect linkage. Returns a small dict
    the dashboard StripeTab uses to decide which view to render
    (connect-CTA vs connected-info).
    """
    access_guard(org_slug, claims)
    require_role(claims, "client_member")

    from pricing_engine import _get_setting
    account_id = _get_setting(db, "stripe_account_id", default="", organization_slug=org_slug)

    return {
        "connected": bool(account_id),
        "account_id": account_id or None,
        "user_email": _get_setting(db, "stripe_user_email", default="", organization_slug=org_slug) or None,
        "connected_at": _get_setting(db, "stripe_connected_at", default="", organization_slug=org_slug) or None,
        "publishable_key": _get_setting(db, "stripe_publishable_key", default="", organization_slug=org_slug) or None,
        "enabled": _get_setting(db, "stripe_enabled", default="false", organization_slug=org_slug) == "true",
    }


@router.post(
    "/webhooks/stripe-connect",
    dependencies=[Depends(rate_limit("stripe_webhook", 120))],
)
async def stripe_connect_webhook(
    request: Request,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """
    Platform-level webhook endpoint. Stripe POSTs every event from every
    connected tenant here. We:

      1. Verify HMAC against the platform whsec (env var, set by Roi)
      2. Read `event.account` to know which tenant this concerns
      3. DB lookup: which org has stripe_account_id == event.account?
      4. Hand to apply_webhook_event with the resolved org_slug

    Endpoint URL (one for the whole platform — register this once in
    Stripe Connect → Webhooks, mode "Connected accounts"):
        POST /admin/api/webhooks/stripe-connect

    Subscribe to:
      - checkout.session.completed
      - payment_intent.succeeded
      - payment_intent.payment_failed
      - charge.refunded
    """
    import stripe_client
    import stripe_connect
    from stripe_push import apply_webhook_event

    if not stripe_connect.PLATFORM_WEBHOOK_SECRET:
        # Webhook hit before platform whsec was provisioned. Fail loud
        # rather than silently accept — Stripe surfaces 503s in the UI.
        raise HTTPException(status_code=503, detail="platform_webhook_secret_not_configured")

    payload = await request.body()
    sig_header = request.headers.get("stripe-signature", "")

    try:
        stripe_client.verify_webhook_signature(
            payload, sig_header, stripe_connect.PLATFORM_WEBHOOK_SECRET,
        )
    except stripe_client.InvalidSignature as e:
        raise HTTPException(status_code=400, detail=f"invalid_signature:{e}")

    try:
        import json as _json
        event = _json.loads(payload.decode("utf-8"))
    except Exception:
        raise HTTPException(status_code=400, detail="malformed_json")

    # Connect events have `account` at the top level (not in data.object).
    # That tells us which tenant's account the event came from.
    account_id = event.get("account")
    if not account_id:
        # Could be a platform-level event (e.g. account.application.deauthorized).
        # Out of scope for v1 — just acknowledge so Stripe stops retrying.
        return {"received": True, "note": "platform_level_event_ignored", "type": event.get("type")}

    setting_row = (
        db.query(Setting)
        .filter(Setting.key == "stripe_account_id", Setting.value == account_id)
        .first()
    )
    if not setting_row:
        # Unknown account — could be a stale connection we lost track of.
        # Return 200 so Stripe doesn't retry forever.
        return {"received": True, "note": "unknown_account", "account_id": account_id}

    result = apply_webhook_event(db, event, setting_row.organization_slug)
    db.commit()
    return {"received": True, "result": result}


# ============================================================================
# Conversations
# ============================================================================


def _conv_summary(c: Conversation) -> dict[str, Any]:
    msgs = c.messages or []
    last_content = msgs[-1]["content"] if msgs else None
    return {
        "id": c.id,
        "external_id": c.external_id,
        "channel": c.channel,
        "customer_name": c.customer_name,
        "customer_email": c.customer_email,
        "customer_phone": c.customer_phone,
        "status": c.status,
        "message_count": len(msgs),
        "last_message_preview": (last_content[:140] if last_content else None),
        "last_message_at": c.updated_at.isoformat() if c.updated_at else None,
        "created_at": c.created_at.isoformat() if c.created_at else None,
    }


@router.get("/orgs/{org_slug}/conversations")
def list_conversations(
    org_slug: str,
    limit: int = Query(50, le=500),
    status: Optional[str] = None,
    channel: Optional[str] = None,
    search: Optional[str] = None,
    include_noise: bool = Query(False),
    claims: StrategosClaims = Depends(require_claims),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """
    List conversations for the org.

    By default, returns only "meaningful" conversations — those with at
    least one Quote attached, OR whose status has been promoted past the
    initial 'open' state (to quoted / awaiting_contact / escalated /
    order_placed). This hides the noise Craig generates when someone
    emails a non-pricing message (e.g. "hi, can you send me your address?"
    via Missive) — Craig still responds politely in the draft, but the
    Conversation row doesn't clog up the dashboard.

    Pass `?include_noise=true` to see everything (useful for debugging).
    Passing an explicit `?status=open` also implicitly shows all 'open'
    conversations regardless of whether quotes exist.
    """
    from sqlalchemy import exists, or_

    access_guard(org_slug, claims)
    q = _scope(db.query(Conversation), Conversation, claims, org_slug)
    if status:
        q = q.filter(Conversation.status == status)
    elif not include_noise:
        # Default filter: hide 'open' rows that have no quote attached.
        quote_exists = exists().where(Quote.conversation_id == Conversation.id)
        q = q.filter(or_(Conversation.status != "open", quote_exists))
    if channel:
        q = q.filter(Conversation.channel == channel)
    if search:
        like = f"%{search.lower()}%"
        q = q.filter(
            (func.lower(Conversation.customer_name).like(like))
            | (func.lower(Conversation.customer_email).like(like))
            | (Conversation.customer_phone.like(f"%{search}%"))
        )
    rows = q.order_by(Conversation.updated_at.desc()).limit(limit).all()
    return {"conversations": [_conv_summary(r) for r in rows]}


@router.get("/orgs/{org_slug}/conversations/{cid}")
def get_conversation(
    org_slug: str,
    cid: int,
    claims: StrategosClaims = Depends(require_claims),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    access_guard(org_slug, claims)
    c = _scope(db.query(Conversation), Conversation, claims, org_slug).filter(Conversation.id == cid).first()
    if not c:
        raise HTTPException(status_code=404, detail="Conversation not found")
    quotes = db.query(Quote).filter_by(conversation_id=c.id).all()
    # Attach a public PDF URL to each quote so the dashboard can link to it
    # without reconstructing the route.
    quote_dicts = []
    for q in quotes:
        d = _quote_to_dict(q)
        d["pdf_url"] = f"/quotes/{q.id}/pdf"
        quote_dicts.append(d)
    return {
        "conversation": {
            **_conv_summary(c),
            "messages": c.messages or [],
            "quotes": quote_dicts,
        }
    }


@router.delete("/orgs/{org_slug}/conversations/{cid}")
def delete_conversation(
    org_slug: str,
    cid: int,
    claims: StrategosClaims = Depends(require_claims),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """
    Delete a conversation + any quotes linked to it. Requires client_owner
    (same role needed to edit settings/catalog) so casual viewers can't nuke
    history. Cascading quote delete keeps the quotes table consistent with
    what the UI shows — deleting a conversation should remove its quote
    artifacts too (including their PDF endpoints' backing rows).
    """
    access_guard(org_slug, claims)
    require_role(claims, "client_owner")
    c = _scope(db.query(Conversation), Conversation, claims, org_slug).filter(Conversation.id == cid).first()
    if not c:
        raise HTTPException(status_code=404, detail="Conversation not found")
    # Delete linked quotes first
    deleted_quotes = db.query(Quote).filter_by(conversation_id=c.id).delete()
    db.delete(c)
    db.commit()
    return {"deleted": True, "id": cid, "quotes_deleted": deleted_quotes}


# ============================================================================
# Metrics
# ============================================================================


def _parse_iso(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid ISO date: {s}")


@router.get("/orgs/{org_slug}/metrics")
def get_metrics(
    org_slug: str,
    from_: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = Query(None),
    claims: StrategosClaims = Depends(require_claims),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    access_guard(org_slug, claims)

    end = _parse_iso(to) or datetime.now(timezone.utc)
    start = _parse_iso(from_) or (end - timedelta(days=30))

    quotes_q = (
        _scope(db.query(Quote), Quote, claims, org_slug)
        .filter(Quote.created_at >= start, Quote.created_at <= end)
    )
    convs_q = (
        _scope(db.query(Conversation), Conversation, claims, org_slug)
        .filter(Conversation.created_at >= start, Conversation.created_at <= end)
    )

    quotes_count = quotes_q.count()
    quotes_value = quotes_q.with_entities(func.coalesce(func.sum(Quote.total), 0.0)).scalar() or 0.0
    convs_count = convs_q.count()

    approved = quotes_q.filter(Quote.status.in_(("approved", "sent", "accepted"))).count()
    approval_rate = (approved / quotes_count) if quotes_count else 0.0

    by_channel = (
        quotes_q.join(Conversation, Quote.conversation_id == Conversation.id, isouter=True)
        .with_entities(
            func.coalesce(Conversation.channel, "unknown").label("channel"),
            func.count(Quote.id),
            func.coalesce(func.sum(Quote.total), 0.0),
        )
        .group_by("channel")
        .all()
    )
    by_status = (
        quotes_q.with_entities(
            Quote.status,
            func.count(Quote.id),
            func.coalesce(func.sum(Quote.total), 0.0),
        )
        .group_by(Quote.status)
        .all()
    )
    top_products = (
        quotes_q.with_entities(
            Quote.product_key,
            func.count(Quote.id),
            func.coalesce(func.sum(Quote.total), 0.0),
        )
        .group_by(Quote.product_key)
        .order_by(func.count(Quote.id).desc())
        .limit(10)
        .all()
    )

    # By-day series (SQLite: group by date(created_at))
    by_day_rows = (
        quotes_q.with_entities(
            func.date(Quote.created_at).label("day"),
            func.count(Quote.id),
            func.coalesce(func.sum(Quote.total), 0.0),
        )
        .group_by("day")
        .order_by("day")
        .all()
    )

    return {
        "from": start.isoformat(),
        "to": end.isoformat(),
        "totals": {
            "quotes_count": quotes_count,
            "quotes_value": round(float(quotes_value), 2),
            "conversations_count": convs_count,
            "approval_rate": round(approval_rate, 4),
        },
        "by_channel": [
            {"channel": ch, "count": int(cnt), "value": round(float(val), 2)}
            for ch, cnt, val in by_channel
        ],
        "by_status": [
            {"status": st, "count": int(cnt), "value": round(float(val), 2)}
            for st, cnt, val in by_status
        ],
        "top_products": [
            {"product_key": pk, "count": int(cnt), "value": round(float(val), 2)}
            for pk, cnt, val in top_products
        ],
        "by_day": [
            {"date": str(day), "count": int(cnt), "value": round(float(val), 2)}
            for day, cnt, val in by_day_rows
        ],
    }
