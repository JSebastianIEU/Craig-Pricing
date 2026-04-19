"""
SQLAlchemy models for Craig's database.
SQLite for MVP — schema compatible with PostgreSQL when we move to production.

Multi-tenant: every catalog/conversation/quote row carries an `organization_slug`
identifying which client it belongs to. Defaults to 'just-print' for backwards
compatibility with the original single-tenant deployment.
"""

from datetime import datetime
from sqlalchemy import (
    Column, Integer, String, Float, Boolean, DateTime,
    ForeignKey, JSON, Text, UniqueConstraint, Index,
)
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()


# Default organization for backwards compatibility (the historical tenant).
DEFAULT_ORG_SLUG = "just-print"

# Pricing strategies a Product can use. Strings (not Enum) so SQLite is happy.
PRICING_STRATEGIES = ("tiered", "per_unit", "per_unit_metric", "bulk_break", "per_job")


# =============================================================================
# PRODUCT CATALOG
# =============================================================================


class Product(Base):
    """
    A single product in a tenant's catalog.

    `pricing_strategy` controls how the engine computes a price:
      - tiered           : look up qty in PriceTier table (default; legacy)
      - per_unit         : unit_price * quantity (large format / signage)
      - per_unit_metric  : unit_price * quantity in `metric_unit` (sq m, kg, hour)
      - bulk_break       : unit_price OR bulk_price beyond bulk_threshold
      - per_job          : single fixed price (booklets — base_price is on the tier)
    """
    __tablename__ = "products"

    id = Column(Integer, primary_key=True)

    organization_slug = Column(String(80), nullable=False, default=DEFAULT_ORG_SLUG, index=True)

    key = Column(String(80), nullable=False, index=True)
    name = Column(String(200), nullable=False)
    category = Column(String(80), nullable=False, index=True)
    description = Column(Text)
    sizes = Column(JSON)
    finishes = Column(JSON)
    price_per = Column(String(60))
    notes = Column(Text)

    pricing_strategy = Column(String(30), nullable=False, default="tiered")
    metric_unit = Column(String(30))  # only for per_unit_metric

    # Optional product image (URL — could be Supabase Storage, any CDN, etc.)
    image_url = Column(Text)

    double_sided_surcharge = Column(Boolean, default=True)

    unit_price = Column(Float)
    bulk_price = Column(Float)
    bulk_threshold = Column(Integer)
    pricing_unit = Column(String(60))
    min_qty = Column(Integer, default=1)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    price_tiers = relationship("PriceTier", back_populates="product", cascade="all, delete-orphan")
    aliases = relationship("ProductAlias", back_populates="product", cascade="all, delete-orphan")

    __table_args__ = (
        UniqueConstraint("organization_slug", "key", name="uq_product_org_key"),
        Index("ix_product_org_category", "organization_slug", "category"),
    )


class PriceTier(Base):
    """A price at a specific quantity tier for a product."""
    __tablename__ = "price_tiers"

    id = Column(Integer, primary_key=True)
    organization_slug = Column(String(80), nullable=False, default=DEFAULT_ORG_SLUG, index=True)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False, index=True)
    spec_key = Column(String(100), default="")
    quantity = Column(Integer, nullable=False)
    price = Column(Float, nullable=False)

    product = relationship("Product", back_populates="price_tiers")

    __table_args__ = (
        UniqueConstraint("product_id", "spec_key", "quantity", name="uq_price_tier"),
        Index("ix_price_lookup", "product_id", "spec_key", "quantity"),
    )


class ProductAlias(Base):
    """Free-text aliases used by the extractor to map customer language to products."""
    __tablename__ = "product_aliases"

    id = Column(Integer, primary_key=True)
    organization_slug = Column(String(80), nullable=False, default=DEFAULT_ORG_SLUG, index=True)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False, index=True)
    alias = Column(String(200), nullable=False, index=True)

    product = relationship("Product", back_populates="aliases")


class SurchargeRule(Base):
    """
    Named surcharge rules. Each tenant defines their own.
    `kind`:
      - multiplier : applies as price * (1 + multiplier)
      - additive   : adds a fixed amount per unit (multiplier holds the amount)
    `applies_to_category` is optional — null means applies to all products.
    """
    __tablename__ = "surcharge_rules"

    id = Column(Integer, primary_key=True)
    organization_slug = Column(String(80), nullable=False, default=DEFAULT_ORG_SLUG, index=True)
    name = Column(String(60), nullable=False)
    multiplier = Column(Float, nullable=False)
    kind = Column(String(20), nullable=False, default="multiplier")
    applies_to_category = Column(String(80))
    description = Column(Text)

    __table_args__ = (
        UniqueConstraint("organization_slug", "name", name="uq_surcharge_org_name"),
    )


class Setting(Base):
    """Key/value config — per tenant."""
    __tablename__ = "settings"

    id = Column(Integer, primary_key=True)
    organization_slug = Column(String(80), nullable=False, default=DEFAULT_ORG_SLUG, index=True)
    key = Column(String(60), nullable=False)
    value = Column(Text, nullable=False)
    value_type = Column(String(20), default="string")
    description = Column(Text)

    __table_args__ = (
        UniqueConstraint("organization_slug", "key", name="uq_setting_org_key"),
    )


# =============================================================================
# TAX RULES
# =============================================================================


class TaxRate(Base):
    """A named tax rate per tenant. e.g. 'standard' = 0.23, 'reduced' = 0.135."""
    __tablename__ = "tax_rates"

    id = Column(Integer, primary_key=True)
    organization_slug = Column(String(80), nullable=False, default=DEFAULT_ORG_SLUG, index=True)
    name = Column(String(60), nullable=False)
    rate = Column(Float, nullable=False)
    description = Column(Text)
    is_default = Column(Boolean, default=False)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("organization_slug", "name", name="uq_tax_org_name"),
    )


class Category(Base):
    """
    First-class product category with metadata.
    Products reference a category by its slug (string), scoped per org.
    Can exist without any products (for setting up an empty category).
    """
    __tablename__ = "categories"

    id = Column(Integer, primary_key=True)
    organization_slug = Column(String(80), nullable=False, default=DEFAULT_ORG_SLUG, index=True)
    slug = Column(String(80), nullable=False)
    name = Column(String(200), nullable=False)
    description = Column(Text)
    icon = Column(String(60))  # optional Lucide icon name
    sort_order = Column(Integer, default=0)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("organization_slug", "slug", name="uq_category_org_slug"),
    )


class CategoryTaxMap(Base):
    """Optional per-category tax assignment. Falls back to org's default rate."""
    __tablename__ = "category_tax_map"

    id = Column(Integer, primary_key=True)
    organization_slug = Column(String(80), nullable=False, default=DEFAULT_ORG_SLUG, index=True)
    category = Column(String(80), nullable=False)
    tax_rate_id = Column(Integer, ForeignKey("tax_rates.id"), nullable=False)

    tax_rate = relationship("TaxRate")

    __table_args__ = (
        UniqueConstraint("organization_slug", "category", name="uq_cat_tax_org_cat"),
    )


# =============================================================================
# CONVERSATIONS & QUOTES
# =============================================================================


class Conversation(Base):
    __tablename__ = "conversations"

    id = Column(Integer, primary_key=True)
    organization_slug = Column(String(80), nullable=False, default=DEFAULT_ORG_SLUG, index=True)
    external_id = Column(String(100), index=True)
    channel = Column(String(30), default="web")
    customer_name = Column(String(200))
    customer_email = Column(String(200))
    customer_phone = Column(String(50))
    messages = Column(JSON, default=list)
    status = Column(String(30), default="active")
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    quotes = relationship("Quote", back_populates="conversation", cascade="all, delete-orphan")

    __table_args__ = (
        Index("ix_conv_org_created", "organization_slug", "created_at"),
    )


class Quote(Base):
    __tablename__ = "quotes"

    id = Column(Integer, primary_key=True)
    organization_slug = Column(String(80), nullable=False, default=DEFAULT_ORG_SLUG, index=True)
    conversation_id = Column(Integer, ForeignKey("conversations.id"), index=True)
    product_key = Column(String(80))
    specs = Column(JSON)
    base_price = Column(Float)
    surcharges = Column(JSON)
    final_price_ex_vat = Column(Float)
    vat_amount = Column(Float)
    final_price_inc_vat = Column(Float)
    artwork_cost = Column(Float, default=0.0)
    total = Column(Float)
    status = Column(String(30), default="pending_approval")
    approved_by = Column(String(100))
    notes = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)

    conversation = relationship("Conversation", back_populates="quotes")

    __table_args__ = (
        Index("ix_quote_org_status", "organization_slug", "status"),
        Index("ix_quote_org_created", "organization_slug", "created_at"),
    )
