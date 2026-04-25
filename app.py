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
from admin_api import router as admin_router


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


class QuoteSmallFormatRequest(BaseModel):
    product_key: str
    quantity: int
    double_sided: bool = False
    finish: Optional[str] = None
    needs_artwork: bool = False
    artwork_hours: float = 0.0


class QuoteLargeFormatRequest(BaseModel):
    product_key: str
    quantity: int
    needs_artwork: bool = False
    artwork_hours: float = 0.0


class QuoteBookletRequest(BaseModel):
    format: str
    binding: str
    pages: int
    cover_type: str
    quantity: int
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

    return {
        "organization_slug": client,
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
    result = quote_large_format(
        db, req.product_key, req.quantity,
        req.needs_artwork, req.artwork_hours,
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
    """Embeddable widget script — drop <script src=\"/widget.js\"> on any page."""
    return FileResponse(os.path.join(STATIC_DIR, "widget.js"), media_type="application/javascript")


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

        token = _get_setting(db, "missive_api_token", "", organization_slug=org_slug)
        enabled = _get_setting(db, "missive_enabled", "false", organization_slug=org_slug)
        if not token or enabled.lower() != "true":
            _mlog.info(
                "%s: disabled or no token, skipping %s",
                org_slug, evt["message_id"],
            )
            return

        # Don't reply to ourselves. If the sender is the same address we're
        # configured to draft from, it's almost certainly Justin's manual
        # reply (or a previous Craig draft Justin hit send on). Skip.
        from_addr = _get_setting(db, "missive_from_address", "", organization_slug=org_slug)
        if from_addr and evt["from_address"].lower() == from_addr.lower():
            _mlog.info(
                "%s: sender is our own from-address, skipping loop",
                org_slug,
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

        # find-or-create the Conversation. external_id keeps threading tidy:
        # every future email in the same Missive thread maps back to this
        # single Craig Conversation row.
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

        # If this is the first turn, stash the sender's email on the row so
        # [QUOTE_READY] gate opens automatically (Missive is inherently a
        # channel where we always know who wrote in).
        if not existing and evt["from_address"]:
            # We'll let chat_with_craig() create the row, then patch it below.
            pass

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

        # Patch the sender's email onto the conversation row so downstream
        # turns + the dashboard know who's on the other side.
        if evt["from_address"]:
            conv = db.query(Conversation).filter_by(id=result["conversation_id"]).first()
            if conv and not (conv.customer_email or "").strip():
                conv.customer_email = evt["from_address"]
                if evt["from_name"] and not (conv.customer_name or "").strip():
                    conv.customer_name = evt["from_name"]
                db.commit()

        reply_text = (result.get("reply") or "").strip()
        if not reply_text:
            _mlog.info("%s: empty reply, not drafting", org_slug)
            return

        # If a quote was generated this turn, attach the PDF to the draft
        # so the customer gets the full branded quote without a separate
        # "click this link" step. Strip the [QUOTE_READY] marker from the
        # visible body — it's only meaningful to the web widget.
        attachments = None
        had_quote_marker = "[QUOTE_READY]" in reply_text
        reply_text_clean = reply_text.replace("[QUOTE_READY]", "").strip()

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
            ))
            _mlog.info(
                "%s: draft posted on conv %s (subject=%s, had_quote=%s)",
                org_slug, evt["conversation_id"], reply_subject, had_quote_marker,
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
