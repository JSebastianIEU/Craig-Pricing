"""
Widget-public endpoints — Phase F.

These are reached by the JavaScript chat widget (the floating bubble
on just-print.ie or the preview at /). They are NOT protected by the
JWT auth that admin_api.py enforces. Auth model:

  - The widget knows two opaque values: its own client-side `external_id`
    (a session token it generates on first load) and the
    `conversation_id` returned by the first `/chat` call.
  - Every widget endpoint requires BOTH to be passed and verifies they
    match — guessing both at random has a probability of effectively 0.
  - If they don't match, the request is rejected with 403.

Two endpoints currently:
  - POST /widget/conversations/{cid}/customer-info — submit the
    structured funnel form (name, email, phone, is_company,
    is_returning_customer, past_customer_email, delivery_method,
    delivery_address). Auto-fills delivery_address with shop_address
    when delivery_method=collect, applies shipping when delivery, and
    triggers the next-turn LLM reply via a synthetic system message.
  - POST /widget/conversations/{cid}/upload-artwork — multipart upload
    of a print-ready file. Stores in Cloud Storage (or local fs in
    dev), persists URL on the most-recent pending Quote on this
    conversation, returns the URL.

Rate-limited the same as /chat to prevent abuse.
"""

from __future__ import annotations

import os
import re
import uuid
from typing import Any, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.orm import Session

from rate_limiter import rate_limit
from db import get_db, parse_artwork_files
from db.models import Conversation, Quote


router = APIRouter(prefix="/widget", tags=["Widget"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


# Irish eircode format: 1 letter + 2 digits, optional space, then 4
# alphanumerics. We accept upper/lower case + optional space.
_EIRCODE_RX = re.compile(r"^[A-Za-z]\d{2}\s?[A-Za-z0-9]{4}$")

# RFC-5322-ish email check — same regex as the rest of the codebase.
_EMAIL_RX = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")

_DISPOSABLE_DOMAINS = {
    "yopmail.com", "tempmail.com", "tempmail.org", "10minutemail.com",
    "guerrillamail.com", "mailinator.com", "throwaway.email",
}

_VALID_DELIVERY_METHODS = ("delivery", "collect")


def _validate_session(
    db: Session, cid: int, external_id: str,
) -> Conversation:
    """
    Pseudo-session check: verify that the conversation_id and
    external_id pair is one we issued via /chat. Either missing or a
    mismatch returns 403 — same as the admin layer's access_guard.
    """
    if not external_id:
        raise HTTPException(status_code=403, detail="missing external_id")
    conv = db.query(Conversation).filter_by(id=cid).first()
    if not conv:
        raise HTTPException(status_code=404, detail="conversation not found")
    if (conv.external_id or "") != external_id:
        raise HTTPException(status_code=403, detail="external_id mismatch")
    return conv


def _parse_shop_address(shop_address: str) -> dict[str, str]:
    """
    Best-effort parse of the human-readable shop_address setting into
    the {address1..4, postcode} structured shape we persist.

    The current value is:
      "Ballymount Cross Business Park, 7, Ballymount, Dublin, D24 E5NH, Ireland"

    Strategy: split by commas, strip whitespace, last segment ending
    with "Ireland" / "Eire" is dropped (country implicit), the segment
    before it that matches the eircode regex is `postcode`, the rest
    folds into address1..4 in order.
    """
    parts = [p.strip() for p in (shop_address or "").split(",") if p.strip()]
    # Drop trailing "Ireland" / "Eire" / "IE"
    if parts and parts[-1].lower() in ("ireland", "eire", "ie"):
        parts = parts[:-1]
    postcode = ""
    if parts and _EIRCODE_RX.match(parts[-1]):
        postcode = parts[-1]
        parts = parts[:-1]
    out: dict[str, str] = {}
    for idx, part in enumerate(parts[:4], start=1):
        out[f"address{idx}"] = part
    if postcode:
        out["postcode"] = postcode
    return out


# ---------------------------------------------------------------------------
# POST /widget/conversations/{cid}/customer-info
# ---------------------------------------------------------------------------


class _DeliveryAddressIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    address1: str = Field("", max_length=200)
    address2: Optional[str] = Field("", max_length=200)
    address3: Optional[str] = Field("", max_length=200)
    address4: Optional[str] = Field("", max_length=200)
    postcode: str = Field("", max_length=40)


class CustomerInfoForm(BaseModel):
    """Phase F structured funnel form. Replaces free-text Q&A in chat."""
    model_config = ConfigDict(extra="forbid")

    external_id: str = Field(..., min_length=1)
    name: str = Field(..., min_length=2, max_length=200)
    email: str = Field(..., min_length=5, max_length=200)
    phone: Optional[str] = Field(None, max_length=50)
    is_company: bool = False
    is_returning_customer: bool = False
    past_customer_email: Optional[str] = Field(None, max_length=200)
    delivery_method: str = Field(..., pattern=r"^(delivery|collect)$")
    # Required ONLY when delivery_method='delivery'. We re-check in code
    # because Pydantic conditional required-ness is awkward.
    delivery_address: Optional[_DeliveryAddressIn] = None

    @field_validator("email")
    @classmethod
    def _email_shape_and_no_disposable(cls, v: str) -> str:
        v = v.strip()
        if not _EMAIL_RX.match(v):
            raise ValueError("must be a valid email")
        domain = v.lower().split("@")[-1]
        if domain in _DISPOSABLE_DOMAINS:
            raise ValueError("disposable email domains aren't accepted")
        return v

    @field_validator("phone")
    @classmethod
    def _phone_min_digits(cls, v: Optional[str]) -> Optional[str]:
        if not v:
            return None
        digits = re.sub(r"\D", "", v)
        if len(digits) < 8:
            raise ValueError("phone must have at least 8 digits")
        return v.strip()


@router.post(
    "/conversations/{cid}/customer-info",
    dependencies=[Depends(rate_limit("widget_form", 10))],
)
def submit_customer_info(
    cid: int,
    body: CustomerInfoForm,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """
    Submit the structured funnel form. Persists every field to the
    Conversation row. Side-effects:

      - If delivery_method='collect', auto-fills delivery_address by
        parsing the tenant's shop_address setting.
      - If delivery_method='delivery', requires delivery_address.address1
        and delivery_address.postcode (Irish eircode format).
      - On the most-recent pending Quote on this conversation,
        applies shipping (€15 inc VAT, free over €100 inc VAT goods).
      - Returns a `next_message` payload the widget can either render
        directly or feed to /chat to trigger Craig's [QUOTE_READY] reply.
    """
    from pricing_engine import _get_setting, apply_shipping_to_quote

    conv = _validate_session(db, cid, body.external_id)

    method = body.delivery_method.strip().lower()
    if method not in _VALID_DELIVERY_METHODS:
        raise HTTPException(status_code=422, detail="invalid delivery_method")

    # Resolve delivery_address based on method
    addr_dict: dict[str, str] = {}
    if method == "delivery":
        if body.delivery_address is None or not body.delivery_address.address1.strip():
            raise HTTPException(
                status_code=422,
                detail="delivery_address.address1 is required when delivery_method='delivery'",
            )
        if not _EIRCODE_RX.match(body.delivery_address.postcode or ""):
            raise HTTPException(
                status_code=422,
                detail="delivery_address.postcode must be a valid Irish eircode (e.g. D02 X1Y2)",
            )
        for k in ("address1", "address2", "address3", "address4", "postcode"):
            v = (getattr(body.delivery_address, k, None) or "").strip()
            if v:
                addr_dict[k] = v
    else:
        # Collection — auto-fill from shop_address setting
        shop = _get_setting(
            db, "shop_address", "",
            organization_slug=conv.organization_slug,
        )
        addr_dict = _parse_shop_address(shop or "")

    # Returning-customer bookkeeping
    past_email = (body.past_customer_email or "").strip()
    if body.is_returning_customer and not past_email:
        raise HTTPException(
            status_code=422,
            detail="past_customer_email is required when is_returning_customer=true",
        )

    # Persist to Conversation
    conv.customer_name = body.name.strip()
    conv.customer_email = body.email.lower().strip()
    if body.phone:
        conv.customer_phone = body.phone.strip()
    conv.is_company = body.is_company
    conv.is_returning_customer = body.is_returning_customer
    conv.past_customer_email = past_email or None
    conv.delivery_method = method
    conv.delivery_address = addr_dict or None
    db.flush()

    # Find the most-recent pending Quote on this conversation and
    # apply shipping. If there is no quote yet (edge case: form submitted
    # before Craig priced), skip.
    pending_quote = (
        db.query(Quote)
        .filter_by(conversation_id=conv.id, status="pending_approval")
        .order_by(Quote.created_at.desc())
        .first()
    )

    shipping_summary: dict[str, Any] = {"applied": False}
    if pending_quote is not None:
        # Phase F gate: if the customer earlier said they have own artwork,
        # the upload is required before we finalize. We infer "promised
        # artwork" from artwork_cost==0 (no design service) on a quote
        # whose conversation has no uploaded files yet.
        #
        # v26 — scan ALL quotes on the conversation for files, not just
        # the most recent. DeepSeek sometimes calls the pricing tool a
        # second time after the customer uploads, creating a fresh empty
        # quote — the upload is on the FIRST quote and the gate
        # incorrectly blocked the form submit. Mirror the cross-quote
        # check we use in craig_agent's _quote_has_artwork_check.
        all_pending = (
            db.query(Quote)
            .filter_by(conversation_id=conv.id)
            .all()
        )
        has_any_files = any(
            bool(parse_artwork_files(q.artwork_files))
            or bool((q.artwork_file_url or "").strip())
            for q in all_pending
        )
        promised_artwork = (
            float(pending_quote.artwork_cost or 0) == 0.0
            and not has_any_files
        )
        if promised_artwork:
            # Inspect conversation messages for an explicit "I have artwork"
            # signal — only block if Craig actually emitted [ARTWORK_UPLOAD]
            # earlier (i.e. the user was offered the upload). We use that
            # marker as the canonical signal.
            had_upload_offer = any(
                "[ARTWORK_UPLOAD]" in (m.get("content") or "")
                for m in (conv.messages or [])
                if m.get("role") == "assistant"
            )
            if had_upload_offer:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "Please upload your print-ready artwork before "
                        "we finalise the quote."
                    ),
                )

        shipping_summary = apply_shipping_to_quote(
            db, pending_quote, method, organization_slug=conv.organization_slug,
        )
        shipping_summary["applied"] = True

    # Phase F refined — short-circuit the assistant reply server-side
    # rather than round-tripping through the LLM. The LLM was producing
    # noisy duplications ("That'll be €X... want me to put together the
    # full quote?") on the [SYSTEM] form-submitted trigger. Bypassing it
    # gives the customer a clean, deterministic confirmation and saves
    # tokens. We append the canned message to the conversation
    # transcript so the dashboard's view is complete.
    quote_id_for_widget: int | None = None
    canned_reply = (
        "All set 👍 here's your full quote — Justin will review it and "
        "email you shortly with the official confirmation and payment "
        "details to continue with your order."
    )
    if pending_quote is not None:
        quote_id_for_widget = pending_quote.id
        # Also append a synthetic SYSTEM "form submitted" line so the
        # dashboard transcript shows what just happened.
        history = list(conv.messages or [])
        history.append({
            "role": "system",
            "content": (
                f"[SYSTEM] Customer submitted the details form: "
                f"name={conv.customer_name}, email={conv.customer_email}, "
                f"company={conv.is_company}, returning={conv.is_returning_customer}, "
                f"delivery={method}."
            ),
        })
        history.append({
            "role": "assistant",
            "content": canned_reply + "\n\n[QUOTE_READY]",
        })
        conv.messages = history

    db.commit()
    db.refresh(conv)

    return {
        "ok": True,
        "conversation_id": conv.id,
        "quote_id": quote_id_for_widget,
        "shipping": shipping_summary,
        # The widget renders this directly in the chat as Craig's reply
        # AND shows the PDF card. No LLM round-trip needed — the canned
        # text is deterministic and clean, and we already persisted it
        # to the conversation history above.
        "assistant_reply": canned_reply,
    }


# ---------------------------------------------------------------------------
# POST /widget/conversations/{cid}/upload-artwork
# ---------------------------------------------------------------------------


# Allowed file types — matches Roi's FAQ #2 list expanded with raster
# print formats Just Print's customers commonly send. Validated by
# extension AND content-type.
ALLOWED_EXTENSIONS = {
    ".pdf", ".jpg", ".jpeg", ".png",
    ".ai", ".indd", ".eps", ".tiff", ".tif", ".psd", ".svg",
}
ALLOWED_CONTENT_TYPES = {
    "application/pdf",
    "image/jpeg", "image/png", "image/tiff", "image/svg+xml",
    "application/postscript",                           # .eps + .ai sometimes
    "application/illustrator", "application/x-illustrator",
    "application/x-photoshop", "image/vnd.adobe.photoshop",
    "application/x-indesign", "application/octet-stream",  # .indd often falls here
}
MAX_UPLOAD_BYTES = 100 * 1024 * 1024  # 100 MB

_GCS_BUCKET = os.environ.get("CRAIG_ARTWORK_BUCKET", "")  # set in prod
_LOCAL_UPLOAD_DIR = os.environ.get("CRAIG_ARTWORK_LOCAL_DIR", "/tmp/craig-artwork")


def _store_file(filename: str, data: bytes, content_type: str) -> str:
    """
    Upload to Cloud Storage if the bucket is configured (prod), else
    fall back to local disk (dev). Returns a STORAGE KEY — for GCS,
    this is `gs://{bucket}/artwork/{filename}` (an internal reference);
    for local mode, `/artwork-local/{filename}` which the FastAPI app
    serves directly.

    Phase G — we no longer fall back to `blob.public_url` on signed URL
    failure. The bucket is private and that fallback used to mask real
    config bugs (it produces 403s for clients). The dashboard now
    fetches files via a backend proxy (`/admin/api/orgs/{slug}/quotes/
    {id}/artwork/{idx}/file`) which authenticates against GCS using the
    Cloud Run service account — no signed URLs needed at all. We keep
    this function returning a stable key so the proxy can resolve it
    server-side.
    """
    if _GCS_BUCKET:
        try:
            from google.cloud import storage  # type: ignore[import-not-found]
        except ImportError:
            raise HTTPException(
                status_code=500,
                detail="google-cloud-storage not installed but CRAIG_ARTWORK_BUCKET is set",
            )
        client = storage.Client()
        bucket = client.bucket(_GCS_BUCKET)
        blob = bucket.blob(f"artwork/{filename}")
        blob.upload_from_string(data, content_type=content_type or "application/octet-stream")
        # Return a stable internal reference. Dashboard never hits this
        # URL directly — it goes through the proxy endpoint.
        return f"gs://{_GCS_BUCKET}/artwork/{filename}"

    # Local fallback (dev)
    os.makedirs(_LOCAL_UPLOAD_DIR, exist_ok=True)
    local_path = os.path.join(_LOCAL_UPLOAD_DIR, filename)
    with open(local_path, "wb") as f:
        f.write(data)
    return f"/artwork-local/{filename}"


# Phase G — cap on the number of artwork files per quote. Matches
# Missive's per-draft attachment limit so we don't have to truncate
# silently when emailing.
MAX_ARTWORK_FILES_PER_QUOTE = 10


def _resolve_pending_quote(db: Session, conv_id: int):
    return (
        db.query(Quote)
        .filter_by(conversation_id=conv_id, status="pending_approval")
        .order_by(Quote.created_at.desc())
        .first()
    )


def _public_artwork_entry(entry: dict, *, quote_id: int, idx: int) -> dict:
    """
    Build the shape the widget receives — strips the internal `gs://`
    URL and replaces it with the proxy URL the dashboard / widget can
    fetch (the proxy authenticates against GCS server-side).
    """
    return {
        "url": f"/admin/api/orgs/{{org}}/quotes/{quote_id}/artwork/{idx}/file",
        "filename": entry.get("filename") or "artwork",
        "size": int(entry.get("size") or 0),
        "content_type": entry.get("content_type") or "application/octet-stream",
        "uploaded_at": entry.get("uploaded_at"),
    }


@router.post(
    "/conversations/{cid}/upload-artwork",
    dependencies=[Depends(rate_limit("widget_upload", 20))],
)
async def upload_artwork(
    cid: int,
    external_id: str = Form(...),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """
    Phase G — multi-file artwork. Each call APPENDS to
    `Quote.artwork_files`. Cap at MAX_ARTWORK_FILES_PER_QUOTE per quote
    (matches Missive's attachment limit).

    Returns:
      {ok, files: [...], count}    — the full list after append
    """
    conv = _validate_session(db, cid, external_id)

    name = (file.filename or "upload").strip()
    ext = os.path.splitext(name)[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=415,
            detail=(
                f"file type {ext!r} not accepted. We take PDF, JPG, PNG, "
                "AI, INDD, EPS, TIFF, PSD, SVG."
            ),
        )

    pending_quote = _resolve_pending_quote(db, conv.id)
    if pending_quote is None:
        raise HTTPException(
            status_code=409,
            detail=(
                "No pending quote on this conversation. Get a price first, "
                "then upload artwork."
            ),
        )

    existing = parse_artwork_files(pending_quote.artwork_files)
    if len(existing) >= MAX_ARTWORK_FILES_PER_QUOTE:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Already have {MAX_ARTWORK_FILES_PER_QUOTE} files on this "
                "quote. Remove one before uploading another."
            ),
        )

    # Read body (enforce max size as we read so a 10 GB malicious upload
    # doesn't OOM the worker).
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(1024 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > MAX_UPLOAD_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"file too large — max {MAX_UPLOAD_BYTES // (1024*1024)} MB",
            )
        chunks.append(chunk)
    data = b"".join(chunks)

    content_type = (file.content_type or "application/octet-stream").lower()
    if content_type not in ALLOWED_CONTENT_TYPES and content_type != "application/octet-stream":
        # Some browsers report unusual types for AI/INDD/EPS — extension
        # whitelist already passed, so warn-and-allow.
        print(
            f"[widget_upload] unusual content_type={content_type!r} for "
            f"ext={ext!r} — accepting anyway (extension whitelisted)",
            flush=True,
        )

    safe_filename = f"{conv.id}-{uuid.uuid4().hex[:12]}{ext}"
    url = _store_file(safe_filename, data, content_type)

    import datetime as _dt
    new_entry = {
        "url": url,
        "filename": name,
        "size": total,
        "content_type": content_type,
        "uploaded_at": _dt.datetime.utcnow().isoformat(timespec="seconds"),
    }
    new_list = existing + [new_entry]
    pending_quote.artwork_files = new_list
    # Mirror first entry into singular columns for backwards compat
    pending_quote.artwork_file_url = new_list[0]["url"]
    pending_quote.artwork_file_name = new_list[0]["filename"]
    pending_quote.artwork_file_size = new_list[0]["size"]

    # Phase G refined — uploading a file IS the answer to "do you have
    # your own artwork?". Treat it as an implicit "yes I have artwork"
    # and flip the conversation flag so Craig stops pestering the
    # customer about it on the next turn. We also overwrite a previous
    # False (e.g. customer first asked for the design service, then
    # changed their mind and uploaded). The truth is what's in the file
    # list — if files exist, they have artwork.
    flag_was = conv.customer_has_own_artwork
    conv.customer_has_own_artwork = True
    if flag_was is not True:
        print(
            f"[widget_upload] conv={conv.id} customer_has_own_artwork "
            f"{flag_was!r} -> True (flipped by upload)",
            flush=True,
        )

    db.commit()

    # Return the public-shape list so the widget can render it (proxy
    # URLs, not internal gs:// references).
    return {
        "ok": True,
        "count": len(new_list),
        "files": [
            _public_artwork_entry(
                e, quote_id=pending_quote.id, idx=i,
            )
            for i, e in enumerate(new_list)
        ],
        # Tells the widget "this upload counted as the customer's
        # answer to the artwork question — you may auto-advance the
        # chat (synthetic user turn) on the FIRST upload only".
        "first_upload": (flag_was is not True),
    }


@router.delete(
    "/conversations/{cid}/upload-artwork/{idx}",
    dependencies=[Depends(rate_limit("widget_upload", 20))],
)
def delete_artwork_file(
    cid: int,
    idx: int,
    external_id: str,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """
    Phase G — remove a single file from the artwork_files list at the
    given index. Used when the customer clicks the ✕ on an uploaded
    file in the widget. Does NOT delete the underlying GCS blob (those
    expire via the bucket's 90-day lifecycle rule); just removes the
    pointer so it won't show in the form / get attached to Missive.
    """
    conv = _validate_session(db, cid, external_id)
    pending_quote = _resolve_pending_quote(db, conv.id)
    if pending_quote is None:
        raise HTTPException(status_code=409, detail="no pending quote")

    files = parse_artwork_files(pending_quote.artwork_files)
    if idx < 0 or idx >= len(files):
        raise HTTPException(status_code=404, detail="index out of range")

    files.pop(idx)
    pending_quote.artwork_files = files
    if files:
        pending_quote.artwork_file_url = files[0]["url"]
        pending_quote.artwork_file_name = files[0]["filename"]
        pending_quote.artwork_file_size = files[0]["size"]
    else:
        pending_quote.artwork_file_url = None
        pending_quote.artwork_file_name = None
        pending_quote.artwork_file_size = None
    db.commit()

    return {
        "ok": True,
        "count": len(files),
        "files": [
            _public_artwork_entry(
                e, quote_id=pending_quote.id, idx=i,
            )
            for i, e in enumerate(files)
        ],
    }
