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
#
# v36 added 'per_sqm' (vinyl labels, banners) and 'per_sheet' (foamex /
# dibond / corri panels) so Craig can ACTUALLY price these instead of
# always escalating like v34 did. The legacy 'per_unit_metric' name
# remains a synonym for 'per_sqm' for backwards compat with any rows
# stamped before v36.
PRICING_STRATEGIES = (
    "tiered",
    "per_unit",
    "per_unit_metric",
    "bulk_break",
    "per_job",
    "per_sqm",
    "per_sheet",
)


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

    # v36 — per-sq/m + per-sheet config.
    #
    # `yield_per_sqm` (Float, nullable) is the count of items produced
    # per square meter for products like vinyl labels (e.g. 81 for
    # 50x30mm labels). When the customer doesn't specify per-item
    # dimensions and `default_unit_size_mm` isn't set, the engine
    # falls back to this to compute m^2 from quantity:
    #   total_m2 = quantity / yield_per_sqm
    # For products priced strictly by area (banners), `yield_per_sqm`
    # stays null and the engine requires `width_mm`/`height_mm` from
    # the LLM call.
    yield_per_sqm = Column(Float, nullable=True)
    # `default_unit_size_mm` is the per-item size used when the customer
    # doesn't specify one. Format: "WIDTHxHEIGHT" e.g. "50x30" for
    # 50mm x 30mm vinyl labels. Engine parses this lazily via
    # _parse_size_mm in pricing_engine.
    default_unit_size_mm = Column(String(20), nullable=True)
    # `sheet_size_mm` is the size of one sheet (e.g. 8x4 ft = 2400x1200)
    # used for `per_sheet` strategy. Default null; engine errors out
    # cleanly if a per_sheet product is mis-configured.
    sheet_size_mm = Column(String(20), nullable=True)
    # `sheet_price` is the cost of one full sheet for `per_sheet`
    # products. The engine multiplies ceil(qty / units_per_sheet) by
    # this. Null = config missing -> escalate.
    sheet_price = Column(Float, nullable=True)

    # v34 — manual-review escalation flag. When True, Craig refuses to
    # auto-quote this product and instead creates a Quote with
    # status='needs_revision' so Justin prices it manually from the
    # dashboard. Auto-set for the six per-sq/m products in v34
    # migration; can also be flipped on POA items / rush items / any
    # product where the catalog price is unreliable.
    manual_review_required = Column(
        Boolean, nullable=False, default=False, server_default="0",
    )
    manual_review_reason = Column(Text, nullable=True)
    # v34 — operator-only notes. Distinct from `notes` (which is
    # customer-facing — Craig may quote it back). `internal_notes`
    # NEVER reaches the customer; it's strictly for the dashboard.
    internal_notes = Column(Text, nullable=True)

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

    Scope precedence — most specific wins (v34):
      1. `applies_to_product_keys` non-empty → only applies to products
         whose `key` is in the list.
      2. `applies_to_category` non-null → only applies to products in
         that category.
      3. Both null → global (applies to every product in the org).

    If both keys + category are set, product-keys wins. The category
    field is preserved for backwards compatibility with v32 surcharges
    that were already category-scoped.
    """
    __tablename__ = "surcharge_rules"

    id = Column(Integer, primary_key=True)
    organization_slug = Column(String(80), nullable=False, default=DEFAULT_ORG_SLUG, index=True)
    name = Column(String(60), nullable=False)
    multiplier = Column(Float, nullable=False)
    kind = Column(String(20), nullable=False, default="multiplier")
    applies_to_category = Column(String(80))
    # v34 — per-product scoping. JSON list of product `key` strings.
    # When non-empty, this overrides applies_to_category at runtime.
    applies_to_product_keys = Column(JSON, nullable=True)
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

    # v35 — test-mode flag. When True, this is a sandbox conversation
    # from the dashboard's Test Chat module — Craig skips the funnel
    # (no artwork question, no contact-info form, no delivery prompt)
    # and the row is hidden from the regular Conversations module so
    # JS / Justin can play with the bot without polluting real
    # customer data.
    is_test = Column(
        Boolean, nullable=False, default=False, server_default="0",
        index=True,
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
    # v33 — when Justin approved (dashboard click). Drives the lifecycle
    # tracker on the dashboard. `approved_by` already exists (the user
    # who clicked); `approved_at` is the matching timestamp.
    approved_at = Column(DateTime, nullable=True)
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

    # v33 — operator notification (Resend). Set when the
    # 'quote ready for approval' email is sent to Justin. Idempotent:
    # non-null means we've already pinged him for this quote and the
    # second trigger should bail. `notification_message_id` is the
    # Resend message id for audit; `notification_last_error` records
    # the failure mode if the send 4xx'd / network'd / Resend was
    # disabled in settings. The dashboard surfaces the error so Justin
    # can retry.
    notification_sent_at = Column(DateTime, nullable=True)
    notification_message_id = Column(String(128), nullable=True)
    notification_last_error = Column(Text, nullable=True)

    # v34 — manual-review escalation. When the engine detects a
    # product flagged manual_review_required (per-sq/m, POA, rush
    # job), Craig auto-creates a Quote with status='needs_revision'
    # and final_price_inc_vat=NULL — Justin prices it from the
    # dashboard. The reason is copied from Product.manual_review_reason
    # so the email + sidebar can show why without joining back.
    manual_review_reason = Column(Text, nullable=True)
    # Justin's hand-typed price + audit. Set via the
    # PATCH /quotes/{id}/manual-price endpoint, which then flips the
    # status to 'pending_approval' so the v33 approval pipeline takes
    # over (Resend approval email → Approve click → payment link).
    manual_quote_price_inc_vat = Column(Float, nullable=True)
    manual_quote_price_ex_vat = Column(Float, nullable=True)
    manual_quote_notes = Column(Text, nullable=True)
    manually_priced_at = Column(DateTime, nullable=True)
    manually_priced_by = Column(String(120), nullable=True)

    # v35 — test-mode mirror of Conversation.is_test. When True, this
    # quote was generated in a sandbox conversation from the Test Chat
    # module. Hidden from the regular Quotations module by default.
    is_test = Column(
        Boolean, nullable=False, default=False, server_default="0",
        index=True,
    )

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


# =============================================================================
# PRICING VERIFICATION (v34)
# =============================================================================


class PricingVerificationFlag(Base):
    """
    v34 — operator-side flag + comment for a (product, quantity, spec_key)
    row in the Pricing Verification table.

    Justin uses the new dashboard tab to scan every product's calculated
    price at representative quantities, flag any that look wrong, and
    leave a per-row note. Persisting the flag means his observations
    survive page reloads + Excel re-exports.

    Distinct from Product.internal_notes (per-product) — these flags
    are per-product-quantity-pairing, so he can flag "100 business
    cards is wrong" without affecting the quantity-1 tier.
    """
    __tablename__ = "pricing_verification_flags"

    id = Column(Integer, primary_key=True)
    organization_slug = Column(String(80), nullable=False, default=DEFAULT_ORG_SLUG, index=True)
    product_key = Column(String(80), nullable=False, index=True)
    quantity = Column(Integer, nullable=False)
    # spec_key disambiguates booklets ("32pp|soft_touch") and similar
    # multi-axis products. Empty string for products that don't use it.
    spec_key = Column(String(120), nullable=False, default="", server_default="")
    flagged_wrong = Column(Boolean, nullable=False, default=False, server_default="0")
    comment = Column(Text, nullable=True)
    flagged_by = Column(String(120), nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint(
            "organization_slug", "product_key", "quantity", "spec_key",
            name="uq_pricing_verification_flag",
        ),
        Index(
            "ix_pricing_verification_org_prod",
            "organization_slug", "product_key",
        ),
    )


# =============================================================================
# ISSUE REPORTS (v35)
# =============================================================================


class IssueReport(Base):
    """
    v35 — customer-reported issue from the widget (or email channel).
    Triggered by the "Report an issue" link in the widget footer or
    by Craig's escalate_to_justin tool with reason_code='customer_reported_issue'.

    Captures the customer's message + a snapshot of their conversation
    so the operator can review what went wrong without losing context
    when the conversation moves on. Sends an admin alert email to
    sebastian@strategos-ai.com (or whoever the org's admin_alert_email
    setting points at).

    The customer gets a friendly canned reply: "Thanks for letting us
    know — we're working on improving Craig and will reach out ASAP
    to keep your quote moving."
    """
    __tablename__ = "issue_reports"

    id = Column(Integer, primary_key=True)
    organization_slug = Column(String(80), nullable=False, default=DEFAULT_ORG_SLUG, index=True)
    # Conversation the customer was in (if any). Null = standalone report.
    conversation_id = Column(Integer, ForeignKey("conversations.id"), nullable=True, index=True)
    # Captured at submit time so the alert email has context even if the
    # conversation later changes / is deleted.
    customer_email = Column(String(200), nullable=True)
    customer_name = Column(String(200), nullable=True)
    channel = Column(String(30), nullable=True)  # web | missive | etc
    # Customer's free-text message describing the issue.
    message = Column(Text, nullable=False)
    # Status workflow — for now 'open' on creation, manually flipped via
    # admin endpoints later (resolve / dismiss / etc).
    status = Column(String(30), nullable=False, default="open", server_default="open")
    reviewed_by = Column(String(120), nullable=True)
    reviewed_at = Column(DateTime, nullable=True)
    resolution_notes = Column(Text, nullable=True)

    # Admin notification audit (mirrors the v33 pattern on Quote).
    notification_sent_at = Column(DateTime, nullable=True)
    notification_message_id = Column(String(128), nullable=True)
    notification_last_error = Column(Text, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        Index("ix_issue_reports_org_status", "organization_slug", "status"),
        Index("ix_issue_reports_org_created", "organization_slug", "created_at"),
    )
