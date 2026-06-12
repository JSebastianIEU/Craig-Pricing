"""
Craig Pricing Service — v2

Main FastAPI app. Serves:
  - /chat           → conversational Craig (DeepSeek-powered)
  - /quote/*        → direct pricing endpoints (for webhooks / other services)
  - /products       → catalog browser
  - /conversations  → review past chats
  - /               → static web chat UI
"""

from dotenv import load_dotenv
load_dotenv()  # must be before any os.environ reads

from fastapi import FastAPI, Depends, HTTPException, Request, BackgroundTasks
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from typing import Optional
from sqlalchemy import func as _sa_func
from sqlalchemy.orm import Session
import asyncio
import json as _json_std
import logging
import os
import re as _re

import missive
from rate_limiter import rate_limit
from db import get_db, init_db, SessionLocal
from db.models import Conversation, Quote
from pricing_engine import (
    quote_small_format, quote_large_format, quote_booklet, list_products,
)
from llm.craig_agent import chat_with_craig
from llm.inbound_classifier import classify_inbound_email, obvious_junk
from admin_api import router as admin_router
from widget_api import router as widget_router
from widget_api import (
    ALLOWED_EXTENSIONS as _ART_ALLOWED_EXTENSIONS,
    ALLOWED_CONTENT_TYPES as _ART_ALLOWED_CONTENT_TYPES,
    MAX_ARTWORK_FILES_PER_QUOTE as _ART_MAX_FILES,
    _store_file as _store_artwork_file,
)
from db import parse_artwork_files


app = FastAPI(
    title="Craig Pricing Service",
    description="Just Print quoting assistant — DeepSeek + SQLite + FastAPI",
    version="2.0.0",
)

# CORS — required for widget embedded on just-print.ie AND
# for the Strategos Dashboard calling /admin/api/* from Vercel.
# CORS — production origins only by default. Local dev origins are added
# only when CRAIG_ENV=dev, so a misconfigured prod deploy can't accept
# requests from a developer's localhost.
_PROD_ORIGINS = [
    "https://just-print.ie",
    "https://www.just-print.ie",
    "https://strategos-dashboard.vercel.app",
    "https://strategos-dashboard-jsebastianieus-projects.vercel.app",
    "https://agents.strategos-ai.com",
]
_DEV_ORIGINS = [
    "http://localhost:8000",
    "http://localhost:8080",
    "http://localhost:3000",
    "http://127.0.0.1:8000",
    "http://127.0.0.1:3000",
]
_allowed_origins = list(_PROD_ORIGINS)
if os.environ.get("CRAIG_ENV", "").lower() == "dev":
    _allowed_origins.extend(_DEV_ORIGINS)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Admin API consumed by Strategos Dashboard. JWT-protected per endpoint.
app.include_router(admin_router)
app.include_router(widget_router)


@app.on_event("startup")
def startup():
    init_db()


# =============================================================================
# REQUEST MODELS
# =============================================================================


class ChatRequest(BaseModel):
    message: str = Field(..., description="Customer's message")
    conversation_id: Optional[int] = Field(None, description="Existing conversation to continue")
    session_id: Optional[str] = Field(None, description="External session/user id (for web)")
    channel: str = Field("web", description="web | whatsapp | email | discord")
    organization_slug: str = Field(
        "just-print",
        description="Which tenant's catalog + system prompt Craig should use. "
                    "Widgets pass their client slug here (from data-client on the embed).",
    )
    # v40 — marketing attribution captured by the widget from the
    # landing-page URL (UTMs + ad click IDs). Merged into the
    # conversation server-side. Shape: {first_touch:{...}, last_touch:{...}}.
    attribution: Optional[dict] = Field(
        None, description="UTM + ad click-ID attribution from the widget"
    )


class QuoteSmallFormatRequest(BaseModel):
    product_key: str
    quantity: int = Field(gt=0)  # reject 0 / negative — no negative-price quotes
    double_sided: bool = False
    finish: Optional[str] = None
    needs_artwork: bool = False
    artwork_hours: float = 0.0


class QuoteLargeFormatRequest(BaseModel):
    product_key: str
    quantity: int = Field(gt=0)  # reject 0 / negative
    needs_artwork: bool = False
    artwork_hours: float = 0.0
    # v36 — dimensions for per-sq/m + per-sheet products. Optional;
    # required only when the product's pricing_strategy is per_sqm or
    # per_sheet (engine escalates with manual_review=True if missing).
    width_mm: int | None = None
    height_mm: int | None = None
    area_sqm: float | None = None
    # v41.4 — standard size for tiered-by-size products (boards, posters):
    # A4/A3/A2/A1/A0/2440x1220/1220x1220. The engine and the LLM tool have
    # supported this since v40.7; the HTTP surface never got it, so boards
    # and posters couldn't be quoted by size through the raw API at all.
    size: str | None = None


class QuoteBookletRequest(BaseModel):
    format: str
    binding: str
    pages: int = Field(gt=0)  # reject 0 / negative
    cover_type: str
    quantity: int = Field(gt=0)  # reject 0 / negative
    needs_artwork: bool = False
    artwork_hours: float = 0.0


# =============================================================================
# ENDPOINTS
# =============================================================================


@app.get("/health", tags=["Health"])
def health():
    return {"status": "ok", "service": "craig-pricing-service", "version": "2.0.0"}


@app.get("/widget-config", tags=["Widget"])
def widget_config(client: str, db: Session = Depends(get_db)):
    """
    Public branding + greeting for an embedded widget.

    Called by static/widget.js on load:
        GET /widget-config?client=<org_slug>

    No auth required — returns only non-sensitive visual configuration. If the
    tenant hasn't configured any of these settings yet, hardcoded defaults
    (Just Print's look + a generic greeting) are returned.
    """
    import json as _json

    from pricing_engine import _get_setting

    primary_color = _get_setting(
        db, "widget_primary_color", "#040f2a", organization_slug=client,
    )
    accent_pink = _get_setting(
        db, "widget_accent_pink", "#e30686", organization_slug=client,
    )
    accent_yellow = _get_setting(
        db, "widget_accent_yellow", "#feea03", organization_slug=client,
    )
    accent_blue = _get_setting(
        db, "widget_accent_blue", "#3e8fcd", organization_slug=client,
    )

    # New (V5): dynamic-length accents array + stripe render mode.
    # Backwards-compat: if widget_accents isn't set, synthesize it from the
    # legacy 3-slot pink/yellow/blue + primary color so existing tenants get a
    # 4-segment rainbow identical to the old hardcoded stripe.
    raw_accents = _get_setting(db, "widget_accents", None, organization_slug=client)
    accents: list[str]
    if raw_accents:
        try:
            parsed = _json.loads(raw_accents)
            accents = [str(c) for c in parsed if isinstance(c, str) and c.strip()]
        except (ValueError, TypeError):
            accents = []
    else:
        accents = []
    if not accents:
        accents = [accent_pink, accent_yellow, accent_blue, primary_color]

    stripe_mode = _get_setting(
        db, "widget_stripe_mode", "sections", organization_slug=client,
    )
    if stripe_mode not in ("sections", "gradient", "solid"):
        stripe_mode = "sections"

    # v37.8 \u2014 server-side kill switch for the embedded chat widget.
    # When `widget_enabled` is "false" the bubble must NOT render on
    # the client's website. The widget reads `disabled` on bootConfig
    # and early-exits its mount() before injecting any DOM. Same
    # operator-flippable Setting pattern as `missive_enabled`.
    widget_enabled_raw = _get_setting(
        db, "widget_enabled", "true", organization_slug=client,
    )
    widget_enabled = (str(widget_enabled_raw or "true").strip().lower() != "false")

    return {
        "organization_slug": client,
        # v37.8 \u2014 operator kill switch. When False, the embedded widget
        # script reads this and bails before mounting. No bubble, no
        # /chat traffic, no token spend.
        "disabled": not widget_enabled,
        "primary_color": primary_color,
        "logo_url": _get_setting(
            db, "widget_logo_url", None, organization_slug=client,
        ),
        "font": _get_setting(
            db, "widget_font", "Poppins", organization_slug=client,
        ),
        "greeting": _get_setting(
            db,
            "widget_greeting",
            "Hey \u2014 Craig here. What are you looking to print?",
            organization_slug=client,
        ),
        "accents": accents,
        "stripe_mode": stripe_mode,
        # Legacy keys kept so older widget.js builds still render correctly.
        "accent_pink": accent_pink,
        "accent_yellow": accent_yellow,
        "accent_blue": accent_blue,
    }


@app.post("/chat", tags=["Chat"], dependencies=[Depends(rate_limit("chat", 30))])
def chat(req: ChatRequest, db: Session = Depends(get_db)):
    """
    Main conversational endpoint. Send a user message, get Craig's reply.
    Pass conversation_id on subsequent turns to keep memory.
    """
    try:
        return chat_with_craig(
            db=db,
            conversation_id=req.conversation_id,
            user_message=req.message,
            external_id=req.session_id,
            channel=req.channel,
            organization_slug=req.organization_slug,
            attribution=req.attribution,
        )
    except Exception as e:
        # Surface a friendly error to the frontend
        msg = str(e)
        if "authentication" in msg.lower() or "api key" in msg.lower() or "401" in msg:
            friendly = (
                "DeepSeek API key is missing or invalid. Paste a real key into the "
                "DEEPSEEK_API_KEY environment variable and restart the server."
            )
        else:
            friendly = f"Backend error: {msg}"
        return {
            "reply": friendly,
            "conversation_id": None,
            "quote_generated": False,
            "escalated": False,
            "tool_calls": [],
            "error": msg,
        }


@app.post("/quote/small-format", tags=["Quoting"])
def api_small_format(req: QuoteSmallFormatRequest, db: Session = Depends(get_db)):
    result = quote_small_format(
        db, req.product_key, req.quantity,
        req.double_sided, req.finish,
        req.needs_artwork, req.artwork_hours,
    )
    return result.to_dict()


@app.post("/quote/large-format", tags=["Quoting"])
def api_large_format(req: QuoteLargeFormatRequest, db: Session = Depends(get_db)):
    # v36 — pass dimensions through so per_sqm + per_sheet strategies
    # actually compute prices instead of escalating.
    result = quote_large_format(
        db, req.product_key, req.quantity,
        needs_artwork=req.needs_artwork,
        artwork_hours=req.artwork_hours,
        width_mm=req.width_mm,
        height_mm=req.height_mm,
        area_sqm=req.area_sqm,
        size=req.size,
    )
    return result.to_dict()


@app.post("/quote/booklet", tags=["Quoting"])
def api_booklet(req: QuoteBookletRequest, db: Session = Depends(get_db)):
    result = quote_booklet(
        db, req.format, req.binding, req.pages, req.cover_type, req.quantity,
        req.needs_artwork, req.artwork_hours,
    )
    return result.to_dict()


@app.get("/products", tags=["Catalog"])
def api_list_products(category: Optional[str] = None, db: Session = Depends(get_db)):
    return list_products(db, category=category)


@app.get("/conversations", tags=["Admin"])
def list_conversations(
    limit: int = 20, status: Optional[str] = None, db: Session = Depends(get_db),
):
    q = db.query(Conversation)
    if status:
        q = q.filter_by(status=status)
    rows = q.order_by(Conversation.created_at.desc()).limit(limit).all()
    return [
        {
            "id": c.id,
            "channel": c.channel,
            "status": c.status,
            "message_count": len(c.messages or []),
            "quote_count": len(c.quotes),
            "created_at": c.created_at.isoformat() if c.created_at else None,
        }
        for c in rows
    ]


@app.get("/conversations/{cid}", tags=["Admin"])
def get_conversation(cid: int, db: Session = Depends(get_db)):
    conv = db.query(Conversation).filter_by(id=cid).first()
    if conv is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return {
        "id": conv.id,
        "channel": conv.channel,
        "status": conv.status,
        "messages": conv.messages,
        "quotes": [
            {
                "id": q.id,
                "product_key": q.product_key,
                "specs": q.specs,
                "final_price_ex_vat": q.final_price_ex_vat,
                "total": q.total,
                "status": q.status,
                "created_at": q.created_at.isoformat() if q.created_at else None,
            }
            for q in conv.quotes
        ],
    }


@app.get("/quotes", tags=["Admin"])
def list_quotes(
    limit: int = 50, status: Optional[str] = None, db: Session = Depends(get_db),
):
    """All quotes pending Justin's approval (by default)."""
    q = db.query(Quote)
    if status:
        q = q.filter_by(status=status)
    else:
        q = q.filter_by(status="pending_approval")
    rows = q.order_by(Quote.created_at.desc()).limit(limit).all()
    return [
        {
            "id": r.id,
            "conversation_id": r.conversation_id,
            "product_key": r.product_key,
            "specs": r.specs,
            "base_price": r.base_price,
            "surcharges": r.surcharges,
            "final_price_ex_vat": r.final_price_ex_vat,
            "total": r.total,
            "status": r.status,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in rows
    ]


@app.get("/quotes/{quote_id}", tags=["Admin"])
def get_quote(quote_id: int, db: Session = Depends(get_db)):
    """Get a single quote by ID."""
    r = db.query(Quote).filter_by(id=quote_id).first()
    if not r:
        raise HTTPException(status_code=404, detail="Quote not found")
    return {
        "id": r.id,
        "product_key": r.product_key,
        "specs": r.specs,
        "base_price": r.base_price,
        "surcharges": r.surcharges,
        "final_price_ex_vat": r.final_price_ex_vat,
        "vat_amount": r.vat_amount,
        "final_price_inc_vat": r.final_price_inc_vat,
        "total": r.total,
        "status": r.status,
    }


@app.get("/quotes/{quote_id}/pdf", tags=["Quoting"])
def quote_pdf(quote_id: int, db: Session = Depends(get_db)):
    """Generate a branded PDF for a quote."""
    from pdf_generator import generate_quote_pdf

    quote = db.query(Quote).filter_by(id=quote_id).first()
    if not quote:
        raise HTTPException(status_code=404, detail="Quote not found")

    pdf_bytes = generate_quote_pdf(quote)
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'inline; filename="JustPrint-Quote-JP-{quote_id:04d}.pdf"',
        },
    )


# =============================================================================
# STATIC FRONTEND
# =============================================================================

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
if os.path.isdir(STATIC_DIR):
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# Phase F — local-mode artwork serving. When CRAIG_ARTWORK_BUCKET is unset
# (dev), uploads go to disk and we serve them from /artwork-local. In prod
# the GCS signed URLs are absolute, so this mount is a no-op there.
_ARTWORK_LOCAL_DIR = os.environ.get("CRAIG_ARTWORK_LOCAL_DIR", "/tmp/craig-artwork")
if not os.environ.get("CRAIG_ARTWORK_BUCKET"):
    os.makedirs(_ARTWORK_LOCAL_DIR, exist_ok=True)
    app.mount(
        "/artwork-local",
        StaticFiles(directory=_ARTWORK_LOCAL_DIR),
        name="artwork-local",
    )


@app.get("/", tags=["Frontend"])
def index():
    """Serve the preview page (mock just-print.ie with Craig widget mounted)."""
    index_path = os.path.join(STATIC_DIR, "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path)
    return {
        "service": "Craig Pricing Service",
        "message": "Frontend not built yet. See /docs for the API.",
    }


@app.get("/widget.js", tags=["Frontend"])
def widget_embed():
    """Embeddable widget script — drop <script src=\"/widget.js\"> on any page.

    v37.8 — short-cache headers (60s). The embedded script itself is
    rarely changed, but when the operator flips `widget_enabled=false`
    we want that change to propagate to live customer pages within a
    minute, not after the next browser cache eviction. The /widget-config
    fetch (called by the widget after load) is already no-store, so once
    a customer's browser pulls the latest widget.js it picks up the
    disabled flag immediately.
    """
    return FileResponse(
        os.path.join(STATIC_DIR, "widget.js"),
        media_type="application/javascript",
        headers={
            "Cache-Control": "public, max-age=60, must-revalidate",
        },
    )


# ============================================================================
# Missive integration: webhook-in + draft-reply-out
# ============================================================================

def _mlog_print(level: str, msg: str, *args) -> None:
    """
    Cloud-Run-friendly logger. Python's `logging` module has no handler
    attached to arbitrary app loggers by default, so `.info()` calls
    silently disappear. Plain `print(..., flush=True)` to stdout always
    shows up in `gcloud run services logs`.
    """
    formatted = msg % args if args else msg
    print(f"[missive] {level}: {formatted}", flush=True)


class _MissiveLogger:
    def info(self, msg, *args): _mlog_print("info", msg, *args)
    def warning(self, msg, *args): _mlog_print("warn", msg, *args)
    def error(self, msg, *args): _mlog_print("error", msg, *args)
    def exception(self, msg, *args):
        import traceback
        _mlog_print("error", msg, *args)
        print(traceback.format_exc(), flush=True)


_mlog = _MissiveLogger()


# ─── Inbound idempotency (in-memory) ────────────────────────────────
# Missive retries failed webhooks up to 5x over ~8 minutes. If the
# first delivery's background task is still running when the retry
# arrives, both would draft a reply. We track every message_id we've
# already drafted-for keyed by (org_slug, message_id). Cleared on
# container restart — that's fine because Missive's retry window is
# minutes and Cloud Run instances live longer than that for a warm
# service. Capped at 1024 entries with FIFO eviction so memory stays
# bounded even on a heavy day.
_DRAFTED_FOR_MESSAGES: set[tuple[str, str]] = set()
_DRAFTED_FOR_MESSAGES_ORDER: list[tuple[str, str]] = []
_DRAFTED_FOR_MESSAGES_CAP = 1024


def _mark_drafted(org_slug: str, message_id: str) -> bool:
    """Returns True if this is the FIRST draft for (org, message) — i.e.
    we should proceed with the work. Returns False on a duplicate
    webhook delivery."""
    key = (org_slug, message_id)
    if key in _DRAFTED_FOR_MESSAGES:
        return False
    _DRAFTED_FOR_MESSAGES.add(key)
    _DRAFTED_FOR_MESSAGES_ORDER.append(key)
    while len(_DRAFTED_FOR_MESSAGES_ORDER) > _DRAFTED_FOR_MESSAGES_CAP:
        old = _DRAFTED_FOR_MESSAGES_ORDER.pop(0)
        _DRAFTED_FOR_MESSAGES.discard(old)
    return True


# ─── Inbound HTML quote-block stripping ─────────────────────────────
# When a customer replies, Outlook/Gmail/Apple Mail prepend a
# cascading quote of the prior thread ("On Mon X wrote: > > >"). We
# already have the prior turns persisted in Conversation.messages, so
# the quoted block adds nothing but noise + token cost. Strip the
# first matching splitter and keep what came before.
_QUOTE_THREAD_SPLITTERS = (
    _re.compile(r"\n\s*On .+(?:wrote|escribi[oó]):.*", _re.IGNORECASE | _re.DOTALL),
    _re.compile(r"\n\s*El .+(?:escribi[oó]).*", _re.IGNORECASE | _re.DOTALL),
    _re.compile(r"\n\s*From: .+\s*\n", _re.IGNORECASE),
    _re.compile(r"\n\s*De: .+\s*\n", _re.IGNORECASE),
    _re.compile(r"\n-----Original Message-----.*", _re.DOTALL),
    _re.compile(r"\n_{5,}\s*\n.*", _re.DOTALL),
    _re.compile(r"\n\s*Sent from my .+", _re.IGNORECASE),  # "Sent from my iPhone"
)


def _strip_quoted_thread(body_text: str) -> str:
    """Remove the cascading quoted-thread block that Outlook/Gmail
    prepend onto replies. Keeps the customer's actual new text."""
    if not body_text:
        return body_text
    earliest = len(body_text)
    for splitter in _QUOTE_THREAD_SPLITTERS:
        m = splitter.search(body_text)
        if m and m.start() < earliest:
            earliest = m.start()
    if earliest < len(body_text):
        return body_text[:earliest].rstrip()
    return body_text


def _detect_returning_customer(
    db: Session,
    org_slug: str,
    email: str | None,
    current_conv_id: int | None = None,
) -> dict:
    """
    Returns whether the sender is a returning customer based on the
    org's conversation + quote history under the EXACT same email
    address (case-insensitive). v32.2 — used by the Missive handler
    to inject [CUSTOMER STATUS] context into the LLM prompt so Craig
    doesn't ask 'have you ordered with us before?' when we already
    know the answer.

    Returns:
        {
            "is_returning":          bool,
            "prior_conversations":   int,
            "prior_quote_count":     int,
        }

    Empty / None email returns is_returning=False (we won't guess).
    """
    if not email:
        return {"is_returning": False, "prior_conversations": 0, "prior_quote_count": 0}
    eq_email = email.strip().lower()
    if not eq_email:
        return {"is_returning": False, "prior_conversations": 0, "prior_quote_count": 0}

    convs_q = (
        db.query(Conversation)
        .filter(Conversation.organization_slug == org_slug)
        .filter(_sa_func.lower(Conversation.customer_email) == eq_email)
    )
    if current_conv_id is not None:
        convs_q = convs_q.filter(Conversation.id != current_conv_id)
    prior_convs = convs_q.count()

    quotes_q = (
        db.query(Quote)
        .join(Conversation, Quote.conversation_id == Conversation.id)
        .filter(Conversation.organization_slug == org_slug)
        .filter(_sa_func.lower(Conversation.customer_email) == eq_email)
    )
    if current_conv_id is not None:
        quotes_q = quotes_q.filter(Conversation.id != current_conv_id)
    prior_quotes = quotes_q.count()

    return {
        "is_returning": prior_convs > 0,
        "prior_conversations": prior_convs,
        "prior_quote_count": prior_quotes,
    }


def _is_self_sent_email(
    *, from_address: str, subject: str,
    missive_from_address: str = "",
    notification_sender_address: str = "",
    internal_team_domains: list[str] | None = None,
    internal_team_addresses: list[str] | None = None,
) -> str | None:
    """v37.2 — return a non-None reason string if the inbound email
    should be silently dropped (self-sent OR internal team OR
    notification-subject). Returns None if it looks like a legitimate
    external sender that should be classified.

    Pulled out of `_handle_missive_event` so it's unit-testable. The
    real webhook reads the relevant settings from the DB and passes
    them in.

    Triggers:
      * sender == missive_from_address (Justin's own outbound identity)
      * sender == notification_sender_address (Craig's notification mailer)
      * sender's DOMAIN matches `internal_team_domains` setting (v37.7;
        e.g. emails from anyone @just-print.ie — team-to-team mail
        landing in the watched inbox)
      * sender's full address matches `internal_team_addresses` setting
        (v37.7; for team members using personal Gmail / Outlook)
      * subject starts with '[Just Print' (operator-notification mark)
    """
    sender = (from_address or "").strip().lower()
    loop_addrs = {
        (a or "").strip().lower()
        for a in (missive_from_address, notification_sender_address)
        if a
    }
    if sender and sender in loop_addrs:
        return f"address-match: {sender!r}"

    # v37.7 — internal team allowlist (configurable per-tenant). Domain
    # match catches all `@just-print.ie` team members in one rule;
    # address match handles team members on personal email.
    if sender and "@" in sender:
        sender_domain = sender.split("@", 1)[1]
        if internal_team_domains:
            domain_set = {(d or "").strip().lower() for d in internal_team_domains if d}
            if sender_domain in domain_set:
                return f"internal-team-domain: {sender_domain!r}"
    if internal_team_addresses and sender:
        addr_set = {(a or "").strip().lower() for a in internal_team_addresses if a}
        if sender in addr_set:
            return f"internal-team-address: {sender!r}"

    subj = (subject or "").strip().lower()
    if subj.startswith("[just print"):
        return f"notification-subject-prefix: {subject!r}"
    return None


def _handle_missive_event(org_slug: str, payload: dict) -> None:
    """
    Background task for an incoming Missive webhook.

    Runs off the 15-second ack path — we've already returned 200 to Missive
    by the time this fires. Keep errors non-fatal and log verbosely so we
    can debug from Cloud Run logs.

    Flow:
      1. Parse the inbound event; skip if it isn't a customer email.
      2. Fetch the full message body (webhook only ships a 140-char preview).
      3. find-or-create a Conversation keyed on Missive's conversation ID
         so every future reply in the same thread goes into the same row.
      4. chat_with_craig(...) — same LLM pipeline as the widget uses.
      5. POST the reply back to Missive as a DRAFT (send=false).

    Any non-2xx from Missive gets logged but does not raise — the webhook
    was already acknowledged, so retrying here would just duplicate the
    work Missive would retry on its side.
    """
    from pricing_engine import _get_setting  # noqa: local import matches other handlers

    # Fresh session for the background task (the request's session is closed
    # by the time this runs).
    db = SessionLocal()
    try:
        # Entry log so we can see the background task actually fires.
        _mlog.info(
            "%s: handler entered. payload keys=%s",
            org_slug,
            sorted(list(payload.keys())),
        )

        evt = missive.extract_inbound_email(payload)
        if not evt:
            _msg = payload.get("message") or payload.get("latest_message") or {}
            _mlog.info(
                "%s: non-reply event (msg.type=%s, conv.id=%s, msg.id=%s), skipping",
                org_slug,
                _msg.get("type"),
                (payload.get("conversation") or {}).get("id"),
                _msg.get("id"),
            )
            return

        # ── v29: idempotency. Missive retries failed webhooks; if the
        # first delivery's background task is still running, the retry
        # would draft a duplicate reply. Skip if we've already processed
        # this exact message_id.
        if not _mark_drafted(org_slug, evt["message_id"]):
            _mlog.info(
                "%s: DUPLICATE webhook for message_id=%s — already drafted, skipping",
                org_slug, evt["message_id"],
            )
            return

        token = _get_setting(db, "missive_api_token", "", organization_slug=org_slug)
        enabled = _get_setting(db, "missive_enabled", "false", organization_slug=org_slug)
        if not token or enabled.lower() != "true":
            _mlog.info(
                "%s: disabled or no token, skipping %s",
                org_slug, evt["message_id"],
            )
            return

        # v37.2 — drop self-sent / operator-notification mail before we
        # spend any tokens on it. Two separate failure modes covered:
        #   1. The Missive draft sender (missive_from_address) replies
        #      land back in the watched mailbox (legacy v28 case).
        #   2. The Resend operator-notification mailer
        #      (notification_sender_address) lands in the same inbox
        #      because the to-address is a Missive-watched alias.
        #      Subject-prefix sniff catches the case where the operator
        #      changed the notification sender but the forward rule
        #      still routes it into Missive.
        from_addr = _get_setting(db, "missive_from_address", "", organization_slug=org_slug)
        notif_sender = _get_setting(
            db, "notification_sender_address", "", organization_slug=org_slug,
        )
        # v37.7 — internal-team allowlist. Settings stored as JSON lists.
        # `_get_setting` already parses value_type='json' into a Python
        # list. Fall back to [] if unset / bad shape.
        team_domains = _get_setting(
            db, "internal_team_domains", [], organization_slug=org_slug,
        )
        if not isinstance(team_domains, list):
            team_domains = []
        team_addresses = _get_setting(
            db, "internal_team_addresses", [], organization_slug=org_slug,
        )
        if not isinstance(team_addresses, list):
            team_addresses = []
        loop_reason = _is_self_sent_email(
            from_address=evt.get("from_address", ""),
            subject=evt.get("subject", ""),
            missive_from_address=from_addr,
            notification_sender_address=notif_sender,
            internal_team_domains=team_domains,
            internal_team_addresses=team_addresses,
        )
        if loop_reason:
            _mlog.info(
                "%s: dropping self-sent / notification email (%s)",
                org_slug, loop_reason,
            )
            return

        # First try to read the body straight from the webhook payload —
        # Missive includes the full message body in most events, which
        # saves a round-trip AND sidesteps get_message flakiness.
        msg_obj = payload.get("message") or payload.get("latest_message") or {}
        body_text = (
            msg_obj.get("body")
            or msg_obj.get("body_text")
            or msg_obj.get("preview")
            or evt["preview"]
            or ""
        ).strip()
        _mlog.info(
            "%s: payload.message keys=%s body_len=%d",
            org_slug, sorted(list(msg_obj.keys())), len(body_text),
        )

        # Only reach out to GET /v1/messages if the payload didn't include
        # the body (some rule configurations strip it). `asyncio` is imported
        # at module-level — do NOT re-import inside this branch, or Python
        # makes it a local to the whole function and break `asyncio.run`
        # calls in paths where this branch didn't execute.
        if not body_text:
            try:
                full = asyncio.run(missive.get_message(evt["message_id"], token))
                message_row = (full.get("messages") or [{}])[0]
                body_text = (
                    message_row.get("body")
                    or message_row.get("preview")
                    or ""
                ).strip()
                _mlog.info("%s: got body from REST API (len=%d)", org_slug, len(body_text))
            except Exception as fetch_err:
                _mlog.warning(
                    "%s: get_message failed (%r), using preview",
                    org_slug, fetch_err,
                )
                body_text = (evt["preview"] or "").strip()

        if not body_text.strip():
            _mlog.info("%s: empty body, skipping", org_slug)
            return

        # Strip HTML — customer emails often arrive as HTML, and the LLM
        # does better with plain text. Keeps line breaks.
        if "<" in body_text and ">" in body_text:
            # naive but safe: drop tags, keep text
            body_text = _re.sub(r"<br\s*/?>", "\n", body_text, flags=_re.IGNORECASE)
            body_text = _re.sub(r"</p>\s*<p[^>]*>", "\n\n", body_text, flags=_re.IGNORECASE)
            body_text = _re.sub(r"<[^>]+>", "", body_text)
            import html as _html_decode
            body_text = _html_decode.unescape(body_text).strip()
            _mlog.info("%s: stripped HTML, body_len=%d", org_slug, len(body_text))

        # v29 — strip the cascading quoted-thread block Outlook/Gmail
        # prepend onto replies. We already have prior turns in
        # Conversation.messages, so the quoted history is just noise.
        _before_strip_len = len(body_text)
        body_text = _strip_quoted_thread(body_text)
        if len(body_text) < _before_strip_len:
            _mlog.info(
                "%s: stripped quoted thread, %d -> %d chars",
                org_slug, _before_strip_len, len(body_text),
            )

        if not body_text.strip():
            # Edge case: the body was 100% quoted thread (e.g. customer
            # hit reply but didn't type anything). Fall back to the
            # preview snippet so we have SOMETHING for the LLM, but log
            # it — usually a customer error.
            body_text = (evt.get("preview") or "").strip()
            _mlog.info(
                "%s: post-strip body was empty, falling back to preview",
                org_slug,
            )

        # find-or-create the Conversation. external_id keeps threading tidy:
        # every future email in the same Missive thread maps back to this
        # single Craig Conversation row. Resolved here (before the spam
        # filter) so we can decide is_thread_reply for the LLM gate.
        existing = (
            db.query(Conversation)
            .filter_by(
                organization_slug=org_slug,
                channel="missive",
                external_id=evt["conversation_id"],
            )
            .first()
        )
        conversation_id = existing.id if existing else None

        # ── v28: spam / non-quote filter ──────────────────────────────
        # 1. Hard-reject prefilter (no-reply senders, bouncebacks,
        #    auto-replies, mailing-list headers). Runs before the LLM
        #    so we don't burn tokens on obvious junk.
        # 2. LLM classifier (DeepSeek) decides whether the email is a
        #    real quote inquiry. Replies to threads where Craig already
        #    drafted are auto-passed (no LLM call).
        # 3. Both filters fail-OPEN — any error defaults to passing the
        #    email through. Better to draft a draft Justin can throw
        #    away than to silently swallow a real lead.
        msg_headers = msg_obj.get("headers") if isinstance(msg_obj, dict) else None
        junk_reason = obvious_junk(
            from_address=evt["from_address"],
            subject=evt["subject"],
            headers=msg_headers if isinstance(msg_headers, dict) else None,
        )
        if junk_reason:
            _mlog.info(
                "%s: HARD-REJECT inbound (conv_id=%s): %s",
                org_slug, conversation_id, junk_reason,
            )
            return

        prior_assistant_msgs = 0
        last_assistant_snippet = ""
        if existing:
            for m in (existing.messages or []):
                if m.get("role") == "assistant":
                    prior_assistant_msgs += 1
                    last_assistant_snippet = (m.get("content") or "")[:300]
        is_thread_reply = prior_assistant_msgs > 0

        verdict = classify_inbound_email(
            from_address=evt["from_address"],
            subject=evt["subject"],
            body_preview=body_text[:800],
            is_thread_reply=is_thread_reply,
            last_assistant_snippet=last_assistant_snippet,
        )

        # v37 — three-tier triage on inbound. Confidence is the LLM's
        # certainty about its verdict (`is_quote_inquiry`). Tier
        # boundaries are confidence-driven, NOT verdict-driven, so a
        # half-sure "this isn't a quote" still gets Justin's eyeball
        # — that's the whole point of the gate.
        #
        #   Tier 1 — confidence < LOW_FLOOR: classifier is too confused
        #            to call. Treat as garbled noise, silent drop.
        #   Tier 2 — LOW_FLOOR <= confidence < threshold: uncertain
        #            either way. Pause + notify Justin with the
        #            pre-rendered Craig draft. Craig writes nothing to
        #            Missive until Justin clicks Approve.
        #   Tier 3 — confidence >= threshold (or is_thread_reply, which
        #            short-circuits to confidence=1.0):
        #            • verdict True  → respond as today
        #            • verdict False → confident junk, silent drop
        confidence = float(verdict.get("confidence", 1.0))
        is_quote = bool(verdict.get("is_quote_inquiry", True))
        from llm.inbound_classifier import LOW_CONFIDENCE_FLOOR

        # Engagement-rejected threads stay silent forever — no
        # re-classification, no notification, no draft. Prevents a
        # bad sender from pestering Justin after he said "don't engage".
        if existing and existing.status == "engagement_rejected":
            _mlog.info(
                "%s: thread previously engagement_rejected (conv_id=%s), dropping",
                org_slug, conversation_id,
            )
            return

        # Tier 1 — classifier confused. Drop silently.
        if confidence < LOW_CONFIDENCE_FLOOR:
            _mlog.info(
                "%s: TIER-1 DROP (conv_id=%s) confidence=%.2f below floor=%.2f reason=%r",
                org_slug, conversation_id, confidence,
                LOW_CONFIDENCE_FLOOR, verdict.get("reason"),
            )
            return

        # Tier 2 — low confidence: engagement-approval gate. v37.1 —
        # Don't return here. We still run Craig like Tier 3 so Justin's
        # approval email can show the proposed reply (one extra LLM call
        # per ambiguous email, but Justin gets a full preview to decide
        # on; once approved, the cached reply ships verbatim — no second
        # LLM call). The Missive draft post + the order-confirmed
        # approval notification are GATED on this flag below so nothing
        # actually leaves the building until Justin clicks Approve.
        threshold = float(_get_setting(
            db, "engagement_confidence_threshold",
            default="0.85", organization_slug=org_slug,
        ) or 0.85)

        # v37.5 — gate-routing rules:
        #
        #   confidence < threshold            → Tier 2 (notify Justin)
        #   confidence >= threshold + True    → Tier 3 (respond)
        #   confidence >= threshold + False   → depends on thread state:
        #     • new thread (no prior assistant turn): silent drop
        #       (newsletters, sales pitches, etc.)
        #     • engaged thread (Craig already replied here): Tier 2
        #       (notify Justin) — we don't silent-drop on a customer
        #       who's mid-conversation with us; Justin gets to decide
        #       whether Craig should send a polite "we don't do that"
        #       or just leave it alone.
        needs_engagement_approval = bool(
            confidence < threshold
            or (is_thread_reply and not is_quote)
        )
        if needs_engagement_approval:
            _mlog.info(
                "%s: TIER-2 PAUSE-WITH-PREVIEW (conv_id=%s) "
                "confidence=%.2f threshold=%.2f verdict=%s thread_reply=%s "
                "reason=%r — running Craig for preview, gating Missive + "
                "approval-notification",
                org_slug, conversation_id, confidence, threshold,
                is_quote, is_thread_reply, verdict.get("reason"),
            )

        # Tier 3 confident-junk drop — ONLY for fresh inbound (no prior
        # assistant turn). Off-topic continuations in engaged threads
        # were rerouted to Tier 2 above so Justin sees them.
        if not needs_engagement_approval and not is_quote:
            _mlog.info(
                "%s: TIER-3 CONFIDENT-DROP (conv_id=%s) "
                "confidence=%.2f >= threshold=%.2f verdict=False "
                "fresh-thread reason=%r — confident junk, silent drop",
                org_slug, conversation_id, confidence, threshold,
                verdict.get("reason"),
            )
            return

        # Tier 3 (verdict True) or Tier 2 (running Craig for preview): fall through.
        # ── end v37 triage ────────────────────────────────────────────

        # ── v29: ingest customer-attached artwork files ──────────────
        # If the inbound email has artwork attached (PDF, AI, INDD, JPG,
        # PNG, etc.), pull the bytes from Missive and stash in the same
        # GCS bucket the widget upload uses. We persist them onto the
        # latest Quote on this conversation AFTER chat_with_craig runs
        # (so the dedupe in pricing_tool sees a fresh quote and the LLM
        # can include them in its reply).
        inbound_attachments_to_store: list[dict] = []
        try:
            raw_atts = missive.extract_attachments_from_message(msg_obj)
            for att in raw_atts:
                fname = (att.get("filename") or "").strip()
                ext = os.path.splitext(fname)[1].lower() if fname else ""
                mt = (att.get("media_type") or "").lower()
                # Allow either a whitelisted extension OR a whitelisted
                # MIME type — different mail clients label things
                # differently (e.g. .ai shows as application/postscript
                # sometimes, application/illustrator other times).
                if ext not in _ART_ALLOWED_EXTENSIONS and mt not in _ART_ALLOWED_CONTENT_TYPES:
                    _mlog.info(
                        "%s: skipping non-artwork attachment filename=%r media_type=%r",
                        org_slug, fname, mt,
                    )
                    continue
                # Cap on per-quote files mirrors widget upload.
                if len(inbound_attachments_to_store) >= _ART_MAX_FILES:
                    _mlog.warning(
                        "%s: hit per-quote attachment cap, skipping rest",
                        org_slug,
                    )
                    break
                try:
                    blob = asyncio.run(
                        missive.download_attachment_bytes(att, token)
                    )
                except Exception as dl_err:
                    _mlog.warning(
                        "%s: attachment download failed (filename=%r): %s",
                        org_slug, fname, dl_err,
                    )
                    continue
                # Store under the same naming convention as widget upload
                import uuid as _uuid
                safe_name = (
                    f"{evt['conversation_id']}-{_uuid.uuid4().hex[:12]}{ext or '.bin'}"
                )
                stored_url = _store_artwork_file(
                    safe_name, blob, mt or "application/octet-stream",
                )
                import datetime as _dt
                inbound_attachments_to_store.append({
                    "url": stored_url,
                    "filename": fname or "artwork",
                    "size": len(blob),
                    "content_type": mt or "application/octet-stream",
                    "uploaded_at": _dt.datetime.utcnow().isoformat(timespec="seconds"),
                })
                _mlog.info(
                    "%s: ingested attachment filename=%r size=%d -> %s",
                    org_slug, fname, len(blob), stored_url,
                )
        except Exception as att_err:
            _mlog.warning(
                "%s: attachment ingestion failed (continuing): %s",
                org_slug, att_err,
            )

        # If we got any artwork files, set the conversation flag to
        # True so the pricing-tool guard recognizes the customer brought
        # their own. Persist BEFORE chat_with_craig so the system prompt
        # can see the right state.
        if inbound_attachments_to_store and existing:
            if existing.customer_has_own_artwork is not True:
                existing.customer_has_own_artwork = True
                db.flush()
                _mlog.info(
                    "%s: flipped customer_has_own_artwork=True (email had attachments)",
                    org_slug,
                )

        # If this is the first turn, stash the sender's email on the row so
        # [QUOTE_READY] gate opens automatically (Missive is inherently a
        # channel where we always know who wrote in).
        if not existing and evt["from_address"]:
            # We'll let chat_with_craig() create the row, then patch it below.
            pass

        # v32.2 — patch sender's display name + email onto the conv row
        # BEFORE the LLM runs, so the prompt can read the real values
        # rather than getting placeholder phrases hallucinated. This
        # used to happen AFTER the LLM call and was too late.
        sender_name = (evt.get("from_name") or "").strip()
        sender_email = (evt.get("from_address") or "").strip()
        if existing:
            if sender_email and not (existing.customer_email or "").strip():
                existing.customer_email = sender_email
            if sender_name and not (existing.customer_name or "").strip():
                existing.customer_name = sender_name
            db.flush()

        # v32.2 — server-detected returning-customer status. We compute
        # it from prior conversations under the same email and inject
        # the result as a system message into the LLM prompt so Craig
        # doesn't ask 'have you ordered with us before?' when we
        # already know the answer.
        returning_status = _detect_returning_customer(
            db, org_slug, sender_email, current_conv_id=conversation_id,
        )
        _mlog.info(
            "%s: returning-customer lookup conv_id=%s email=%s -> "
            "is_returning=%s prior_convs=%d prior_quotes=%d",
            org_slug, conversation_id, sender_email,
            returning_status["is_returning"],
            returning_status["prior_conversations"],
            returning_status["prior_quote_count"],
        )

        extra_ctx: list[dict] = []
        sender_block = (
            "[SENDER METADATA — extracted from the email envelope]\n"
            f"Email: {sender_email or '(unknown)'}\n"
            f"Display name: {sender_name or '(empty — not in envelope)'}\n"
            "When you call save_customer_info(name=...), use the EXACT "
            "display name above. If the display name is empty, use what "
            "the customer signed the email with (\"Cheers, <Name>\"). If "
            "neither is available, ask 'And could I get your name for "
            "the invoice?' in your STEP 3 funnel-collection email. "
            "NEVER pass placeholder phrases like \"the customer's name "
            "from the conversation\" — that is a hallucination and the "
            "server will reject it."
        )
        extra_ctx.append({"role": "system", "content": sender_block})

        if returning_status["is_returning"]:
            customer_block = (
                "[CUSTOMER STATUS — server-detected]\n"
                f"This email ({sender_email}) has had "
                f"{returning_status['prior_conversations']} prior "
                f"conversation(s) and "
                f"{returning_status['prior_quote_count']} prior quote(s) "
                "with us. Treat them as a RETURNING customer.\n"
                " - When you call save_customer_info, pass "
                "is_returning_customer=true AND past_customer_email=<the "
                "sender's current email above>. Don't ask 'have you "
                "ordered with us before?' — we already know they have.\n"
                " - In your STEP 3 funnel-collection email, replace the "
                "'have you ordered with us before?' bullet with a brief "
                "'welcome back' acknowledgement in the opening line."
            )
        else:
            customer_block = (
                "[CUSTOMER STATUS — server-detected]\n"
                f"This email ({sender_email or '(unknown)'}) has no prior "
                "conversations with us. Treat them as a NEW customer. "
                "When you call save_customer_info, pass "
                "is_returning_customer=false. The 'have you ordered with "
                "us before?' question can be omitted from STEP 3 — we "
                "already know they're new."
            )
        extra_ctx.append({"role": "system", "content": customer_block})

        _mlog.info(
            "%s: calling chat_with_craig (conversation_id=%s, body preview=%r)",
            org_slug, conversation_id, body_text[:120],
        )
        result = chat_with_craig(
            db=db,
            conversation_id=conversation_id,
            user_message=body_text,
            external_id=evt["conversation_id"],
            channel="missive",
            organization_slug=org_slug,
            extra_system_messages=extra_ctx,
        )
        _mlog.info(
            "%s: chat_with_craig returned (quote_generated=%s, quote_id=%s, reply_len=%d)",
            org_slug,
            result.get("quote_generated"),
            result.get("quote_id"),
            len(result.get("reply") or ""),
        )
        # Log the full reply so we can debug tone / formatting issues without
        # needing to round-trip through the dashboard.
        _mlog.info(
            "%s: reply=%r",
            org_slug,
            (result.get("reply") or "")[:1000],
        )

        # ── v33: notify Justin when the customer commits ───────────
        # Email channel commit signal: `confirm_order` tool fired this
        # turn. Idempotent — bails if `notification_sent_at` is set.
        # Catches its own errors so the customer flow continues even
        # if Resend is down.
        # v37.1 — DO NOT fire when we're in Tier 2 (needs engagement
        # approval). Justin will get the engagement-approval email
        # below; firing the order-approval one too would be confusing
        # (and arguably wrong — Craig hasn't actually contacted the
        # customer yet, so an "order confirmed" notification is
        # premature). The order-approval email will fire naturally
        # later, when the customer replies post-engagement-approval.
        if (
            result.get("order_confirmed")
            and result.get("quote_id")
            and not needs_engagement_approval
        ):
            try:
                from notifications import trigger_approval_notification
                trigger_approval_notification(
                    db, org_slug, int(result["quote_id"]),
                )
                _mlog.info(
                    "%s: triggered approval notification (confirm_order) quote=%s",
                    org_slug, result["quote_id"],
                )
            except Exception as notif_err:
                _mlog.warning(
                    "%s: notification trigger failed (non-fatal): %s",
                    org_slug, notif_err,
                )

        # Patch the sender's email onto the conversation row so downstream
        # turns + the dashboard know who's on the other side.
        if evt["from_address"]:
            conv = db.query(Conversation).filter_by(id=result["conversation_id"]).first()
            if conv and not (conv.customer_email or "").strip():
                conv.customer_email = evt["from_address"]
                if evt["from_name"] and not (conv.customer_name or "").strip():
                    conv.customer_name = evt["from_name"]
                # v40 — an email lead carries no UTM/click IDs of its own,
                # but if this person clicked an ad + landed on the site
                # earlier, we can attribute them by identity (email match).
                try:
                    from attribution import backfill_attribution_by_identity
                    backfill_attribution_by_identity(db, conv)
                except Exception as _attr_err:  # pragma: no cover - defensive
                    _mlog.warning("attribution backfill failed: %s", _attr_err)
                db.commit()

        # ── v29: persist inbound artwork attachments onto the Quote ──
        # We staged them in `inbound_attachments_to_store` BEFORE the
        # LLM call (so `customer_has_own_artwork=True` was visible to
        # the prompt). Now that the LLM may have created a fresh Quote
        # row, append the files to it.
        if inbound_attachments_to_store and result.get("quote_id"):
            try:
                quote_row = db.query(Quote).filter_by(id=result["quote_id"]).first()
                if quote_row is not None:
                    existing_files = parse_artwork_files(quote_row.artwork_files)
                    new_files = existing_files + inbound_attachments_to_store
                    # Cap at MAX
                    new_files = new_files[:_ART_MAX_FILES]
                    quote_row.artwork_files = new_files
                    if new_files:
                        first = new_files[0]
                        quote_row.artwork_file_url = first.get("url")
                        quote_row.artwork_file_name = first.get("filename") or "artwork"
                        quote_row.artwork_file_size = int(first.get("size") or 0)
                    db.commit()
                    _mlog.info(
                        "%s: persisted %d inbound attachment(s) on quote %s",
                        org_slug, len(inbound_attachments_to_store), quote_row.id,
                    )
            except Exception as persist_err:
                _mlog.warning(
                    "%s: failed to persist inbound attachments on quote: %s",
                    org_slug, persist_err,
                )

        reply_text = (result.get("reply") or "").strip()
        if not reply_text:
            _mlog.info("%s: empty reply, not drafting", org_slug)
            return

        # ── v32.1 — defensive marker strip (handles LLM drift) ───────
        # DeepSeek occasionally writes the markers without the underscore
        # ([QUOTEREADY], [ARTWORKUPLOAD]) or with a space ([QUOTE READY]).
        # The pre-v32.1 strip only caught the canonical form and let the
        # variants through into the customer's inbox. Now we kill any
        # bracket-wrapped marker token that LOOKS like ours, regardless
        # of separator (underscore / space / nothing). Detect first so
        # we can still flag had_quote_marker.
        had_quote_marker = bool(_re.search(
            r"\[\s*QUOTE[_\s]*READY\s*\]", reply_text, flags=_re.IGNORECASE,
        ))
        # Strip every [QUOTE READY] / [QUOTE_READY] / [QUOTEREADY] / [ARTWORK_*]
        # / [CUSTOMER_FORM] variation, regardless of underscore / space.
        reply_text = _re.sub(
            r"\[\s*(QUOTE[_\s]*READY|ARTWORK[_\s]*UPLOAD|ARTWORK[_\s]*CHOICE|CUSTOMER[_\s]*FORM)\s*\]",
            "",
            reply_text,
            flags=_re.IGNORECASE,
        )
        # Collapse the blank lines those markers may have left behind.
        reply_text = _re.sub(r"\n{3,}", "\n\n", reply_text).strip()

        # If a quote was generated this turn, attach the PDF to the draft
        # so the customer gets the full branded quote without a separate
        # "click this link" step. (Markers already stripped above.)
        attachments = None
        reply_text_clean = reply_text

        if result.get("quote_generated") and result.get("quote_id"):
            try:
                from pdf_generator import generate_quote_pdf
                import base64

                quote_row = db.query(Quote).filter_by(id=result["quote_id"]).first()
                if quote_row is not None:
                    pdf_bytes = generate_quote_pdf(quote_row)
                    # Missive accepts EITHER an existing attachment reference
                    # (`id`) OR a new one (`filename` + `base64_data`) — and
                    # rejects any mix with other fields like `content_type`.
                    # So we only send filename + base64_data.
                    attachments = [{
                        "filename": f"JustPrint-Quote-JP-{quote_row.id:04d}.pdf",
                        "base64_data": base64.b64encode(pdf_bytes).decode("ascii"),
                    }]
                    _mlog.info(
                        "%s: attaching quote PDF (%d bytes) for quote %s",
                        org_slug, len(pdf_bytes), quote_row.id,
                    )
            except Exception as pdf_err:
                _mlog.error(
                    "%s: PDF generation failed, draft will go out without attachment: %s",
                    org_slug, pdf_err,
                )

        # Plain-text → HTML. Keeps paragraph breaks, escapes angle brackets.
        import html as _html

        html_body = (
            "<p>"
            + _html.escape(reply_text_clean).replace("\n\n", "</p><p>").replace("\n", "<br>")
            + "</p>"
        )

        from_name = _get_setting(
            db, "missive_from_name", "Craig", organization_slug=org_slug,
        )
        to_fields = [{
            "address": evt["from_address"],
            "name": evt["from_name"] or evt["from_address"],
        }] if evt["from_address"] else []

        # Build a "Re: ..." subject so it looks like a proper email reply
        # in the recipient's inbox. Don't double-prefix if the incoming
        # subject already starts with Re:. Fall back to a friendly default
        # when the customer didn't set a subject at all — an empty draft
        # subject looks unprofessional and confuses their inbox threading.
        original_subject = (evt.get("subject") or "").strip()
        if original_subject:
            reply_subject = (
                original_subject
                if original_subject.lower().startswith(("re:", "re :"))
                else f"Re: {original_subject}"
            )
        else:
            reply_subject = "Re: Your quote from Just Print"

        # ── v32: auto-send vs draft gate ─────────────────────────────
        # Default posture: auto-send the clarifying chatter (asking
        # specs / artwork / funnel info / order-confirm acks) so Justin
        # doesn't have to click Send 4 times per quote. Drafts ONLY
        # when one of these fires:
        #   1. The reply carries the binding price + PDF (had_quote_marker
        #      AND attachments is non-empty). The customer must not see a
        #      hallucinated total — Justin reviews and clicks Send.
        #   2. Craig escalated (escalate_to_justin tool fired). Means
        #      Craig couldn't handle the request — needs human judgement.
        #   3. The org disabled auto-send via settings (emergency rollback
        #      back to fully-human-reviewed).
        auto_send_enabled = (_get_setting(
            db, "missive_auto_send_enabled", "true",
            organization_slug=org_slug,
        ) or "true").lower() == "true"

        # v33 — ALL Missive replies auto-send, including the binding
        # quote PDF. Justin's approval moment moved from "Missive draft
        # Send button" to "dashboard Approve button". The PDF + price
        # still go to the customer auto-magically; Justin only steps in
        # AFTER the customer says "yes I want to order" (confirm_order
        # tool fires → operator notification → dashboard Approve →
        # payment-link email).
        #
        # The only remaining draft case is `escalation` — Craig couldn't
        # handle the request and needs Justin's own words.
        quote_generated_this_turn = bool(result.get("quote_generated"))
        is_escalation = bool(result.get("escalated"))

        draft_only = (not auto_send_enabled) or is_escalation
        should_send = not draft_only

        gating_reason = (
            "auto_send_disabled" if not auto_send_enabled
            else "escalation" if is_escalation
            else "auto_send"
        )
        _mlog.info(
            "%s: missive reply gating: should_send=%s reason=%s "
            "had_quote_marker=%s quote_generated=%s has_attachment=%s escalated=%s",
            org_slug, should_send, gating_reason, had_quote_marker,
            quote_generated_this_turn, bool(attachments), is_escalation,
        )

        # v37.1 — engagement-approval branch. We've already run Craig
        # (so we have a real reply + maybe a Quote), but Justin hasn't
        # cleared this thread for outbound. Park the proposed reply
        # inside `engagement_classification` and email Justin instead
        # of posting to Missive. The approve endpoint reads these
        # fields and posts the SAME reply/HTML/subject/attachments to
        # Missive — no second LLM call, no drift between preview and
        # what the customer actually sees.
        if needs_engagement_approval:
            from datetime import datetime as _dt37
            from notifications import trigger_engagement_approval_notification

            conv_id_ref = result.get("conversation_id") or conversation_id
            conv_row = (
                db.query(Conversation).filter_by(id=conv_id_ref).first()
                if conv_id_ref else None
            )
            if conv_row is None:
                _mlog.warning(
                    "%s: TIER-2 has no conversation row to park on (id=%s) — "
                    "skipping notification (Missive draft already gated)",
                    org_slug, conv_id_ref,
                )
                return

            conv_row.status = "pending_engagement_approval"
            classification = dict(conv_row.engagement_classification or {})
            classification.update({
                "from": evt["from_address"],
                "subject": evt.get("subject", ""),
                "body_preview": body_text[:1500],
                "verdict": bool(verdict.get("is_quote_inquiry", True)),
                "confidence": confidence,
                "reason": verdict.get("reason", ""),
                "classified_at": _dt37.utcnow().isoformat(timespec="seconds"),
                "missive_message_id": evt.get("message_id"),
                "missive_subject": evt.get("subject", ""),
                # v37.1 — pre-rendered draft. Approve endpoint posts
                # these verbatim to Missive without a second LLM call.
                "proposed_reply": reply_text_clean,
                "proposed_html": html_body,
                "proposed_subject": reply_subject,
                "proposed_quote_id": result.get("quote_id"),
                "proposed_attachments_present": bool(attachments),
                "proposed_should_send": should_send,
            })
            conv_row.engagement_classification = classification
            db.commit()
            try:
                trigger_engagement_approval_notification(db, org_slug, conv_row.id)
            except Exception as notif_err:
                _mlog.warning(
                    "%s: engagement notification trigger failed (non-fatal): %s",
                    org_slug, notif_err,
                )
            _mlog.info(
                "%s: TIER-2 PARKED preview reply (conv_id=%s, reply_len=%d, quote_id=%s)",
                org_slug, conv_row.id, len(reply_text_clean),
                result.get("quote_id"),
            )
            return  # skip Missive post

        # v37.7 — Tier 3 label tagging. The operator creates a "Craig:
        # Auto-Replied" label in Missive UI manually, then pastes its
        # UUID into the Setting `missive_label_auto_replied`. Empty /
        # missing setting = labelling skipped (graceful fallback so
        # cutover doesn't depend on labels being configured).
        label_auto_replied = _get_setting(
            db, "missive_label_auto_replied", "",
            organization_slug=org_slug,
        )
        add_labels = [label_auto_replied] if label_auto_replied else None

        try:
            asyncio.run(missive.create_draft(
                conversation_id=evt["conversation_id"],
                html_body=html_body,
                from_address=from_addr or "",
                from_name=from_name,
                to_fields=to_fields,
                token=token,
                subject=reply_subject,
                attachments=attachments,
                send=should_send,
                add_shared_labels=add_labels,
            ))
            _mlog.info(
                "%s: missive reply %s on conv %s (subject=%s, reason=%s, "
                "label_auto_replied=%s)",
                org_slug,
                "sent" if should_send else "drafted",
                evt["conversation_id"], reply_subject, gating_reason,
                "applied" if add_labels else "skipped",
            )
        except Exception as draft_err:
            _mlog.error(
                "%s: create_draft failed: %s", org_slug, draft_err,
            )
    except Exception:
        _mlog.exception("%s: handler crashed", org_slug)
    finally:
        db.close()


@app.post(
    "/webhook/missive/{org_slug}",
    tags=["Missive"],
    dependencies=[Depends(rate_limit("missive_webhook", 60))],
)
async def missive_webhook(
    org_slug: str,
    request: Request,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """
    Public endpoint Missive calls when a new email hits the watched inbox.

    Auth is via HMAC signature, not a user token — the `missive_webhook_secret`
    setting is the shared key, and Missive sends `X-Hook-Signature` as a
    SHA256 hex digest over the raw body.

    We do the minimum here: verify the signature, enqueue a background task,
    and ack within the 15s budget Missive gives us. Everything slow (REST
    calls to Missive, LLM turn, DB writes) happens off the request thread.
    """
    from pricing_engine import _get_setting

    raw_body = await request.body()
    signature = request.headers.get("X-Hook-Signature", "")

    secret = _get_setting(db, "missive_webhook_secret", "", organization_slug=org_slug)
    if not missive.verify_webhook(raw_body, signature, secret):
        raise HTTPException(status_code=401, detail="bad signature")

    try:
        payload = _json_std.loads(raw_body)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid json")

    background_tasks.add_task(_handle_missive_event, org_slug, payload)
    return {"ok": True}


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("app:app", host="0.0.0.0", port=port, reload=True)
