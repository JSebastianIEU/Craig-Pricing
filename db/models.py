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

    # Phase E — extended customer-funnel fields (v22 migration). Roi's
    # post-meeting spec: Craig must collect company-vs-individual,
    # returning-customer status, and delivery-vs-collect preference
    # before generating a quote so the eventual PrintLogic order has
    # everything Justin needs to invoice + ship.
    is_company = Column(Boolean, nullable=True)
    is_returning_customer = Column(Boolean, nullable=True)
    past_customer_email = Column(String(200), nullable=True)
    # 'delivery' | 'collect' — null until customer picks
    delivery_method = Column(String(20), nullable=True)
    # JSON object: {address1, address2, address3, address4, postcode}
    delivery_address = Column(JSON, nullable=True)

    # Phase F (refined) — explicit artwork status. Set by the server-side
    # sniff in chat_with_craig (looks at "do you have artwork?" reply
    # patterns) and read back by the [ARTWORK_UPLOAD] gate so the upload
    # button only appears when the customer actually said they have own
    # artwork. True  = customer says they have print-ready artwork
    # False = customer wants the €65 design service
    # null  = the artwork question hasn't been answered yet
    customer_has_own_artwork = Column(Boolean, nullable=True)

    # Phase G v30 — set when the customer chose "I'll send my artwork
    # later" (the third artwork-choice button) or said something
    # equivalent ("I haven't finalised it yet", "just need a price").
    # When True, treat as customer_has_own_artwork=True for pricing
    # purposes (no design line item) BUT skip the upload-first replace
    # gate and the [ARTWORK_UPLOAD] auto-emit so Craig doesn't loop on
    # "send your artwork over". The dashboard renders an "Artwork
    # pending" badge so Justin knows the order is incomplete; the
    # PrintLogic jobsheet says "Artwork: PENDING".
    artwork_will_send_later = Column(
        Boolean, nullable=False, default=False, server_default="0",
    )

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

    # PrintLogic integration (Phase A). `printlogic_order_id` stores either
    # a real PrintLogic order id (pushed from the dashboard or from
    # confirm_order) OR a synthetic "DRY-xxxxxxxx" id when the tenant's
    # `printlogic_dry_run` Setting is still true. Non-null is the idempotency
    # signal that prevents duplicate pushes.
    printlogic_order_id = Column(String(64), nullable=True, index=True)
    printlogic_customer_id = Column(String(64), nullable=True)
    printlogic_pushed_at = Column(DateTime, nullable=True)
    printlogic_last_error = Column(Text, nullable=True)
    printlogic_push_attempts = Column(Integer, nullable=False, default=0, server_default="0")

    # Stripe payment links (Phase B). We create a Stripe Payment Link when the
    # customer confirms a quote and store the URL + status here. `payment_status`
    # transitions: null → "unpaid" (link sent) → "paid" (webhook confirmed)
    # → "refunded"/"failed". `stripe_payment_link_id` is the idempotency guard
    # — if non-null we never create a second link for the same quote.
    stripe_payment_link_id = Column(String(128), nullable=True, index=True)
    stripe_payment_link_url = Column(Text, nullable=True)
    stripe_checkout_session_id = Column(String(128), nullable=True, index=True)
    stripe_payment_status = Column(String(32), nullable=True)  # unpaid / paid / refunded / failed
    stripe_paid_at = Column(DateTime, nullable=True)
    stripe_last_error = Column(Text, nullable=True)

    # Missive outbound draft (Phase C). When the dashboard "Approve" action
    # fires on a web-widget conversation and the tenant has Missive enabled,
    # we create a brand-new Missive thread (not a reply — the customer
    # never emailed in) with the quote PDF + payment link. `missive_draft_id`
    # is the idempotency guard — non-null means we already drafted, don't
    # double-send. `missive_drafted_at` records when. `missive_last_error`
    # keeps the failure mode if the draft creation 4xx'd / network'd.
    missive_draft_id = Column(String(128), nullable=True, index=True)
    missive_drafted_at = Column(DateTime, nullable=True)
    missive_last_error = Column(Text, nullable=True)

    # Customer-side acceptance signal (Phase D). Set by the LLM's
    # `confirm_order` tool when the customer explicitly says "yes / go
    # ahead" in the chat. Distinct from `approved_by` (Justin's manual
    # approval action in the dashboard). The pair lets the dashboard
    # show queues split by:
    #   - "Customer confirmed, awaiting your approval" (client_confirmed_at
    #     set, approved_by null)
    #   - "Approved, sent to customer for payment"     (approved_by set,
    #     stripe_payment_status != 'paid')
    #   - "Paid, ready for production"                 (stripe_payment_status
    #     = 'paid', no PrintLogic order yet)
    client_confirmed_at = Column(DateTime, nullable=True)

    # Phase F — shipping line item. Just Print's policy: €15 inc VAT
    # flat fee for delivery, free over €100 goods inc VAT. The cost is
    # 0 for collection or for orders that hit the threshold. We persist
    # both ex-VAT and inc-VAT so the PDF can render the line item with
    # an accurate VAT breakdown without re-derivation.
    shipping_cost_ex_vat = Column(Float, nullable=False, default=0.0, server_default="0")
    shipping_cost_inc_vat = Column(Float, nullable=False, default=0.0, server_default="0")

    # Phase F — customer-uploaded artwork file. Cloud Storage URL +
    # original filename + size. (Singular columns kept for backwards
    # compat with code paths that haven't been updated to read the
    # array. Every write to `artwork_files` mirrors the FIRST entry
    # into these.)
    artwork_file_url = Column(Text, nullable=True)
    artwork_file_name = Column(String(255), nullable=True)
    artwork_file_size = Column(Integer, nullable=True)

    # Phase G — multi-file artwork support. JSON array where each entry
    # is `{url, filename, size, content_type, uploaded_at}`. Customers
    # commonly need to attach front + back PDFs, design + reference
    # images, etc. Capped at 10 files per quote (matches Missive's
    # attachment limit). Null = no uploads yet.
    artwork_files = Column(JSON, nullable=True)

    conversation = relationship("Conversation", back_populates="quotes")

    __table_args__ = (
        Index("ix_quote_org_status", "organization_slug", "status"),
        Index("ix_quote_org_created", "organization_slug", "created_at"),
    )
