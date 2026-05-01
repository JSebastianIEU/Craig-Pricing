"""
Phase F tests — shipping logic + customer-info form endpoint + artwork
upload endpoint + the artwork-required gate.
"""

from __future__ import annotations

import io
import os
import time

import jwt
import pytest
from fastapi.testclient import TestClient

os.environ["STRATEGOS_JWT_SECRET"] = os.environ.get(
    "STRATEGOS_JWT_SECRET", "test-secret-32-bytes-long-padding-enough-now",
)
os.environ.setdefault("CRAIG_ARTWORK_LOCAL_DIR", "/tmp/craig-artwork-test")

from app import app  # noqa: E402
from db import db_session  # noqa: E402
from db.models import Conversation, Quote, Setting, DEFAULT_ORG_SLUG  # noqa: E402
from rate_limiter import _reset_for_tests as _rl_reset  # noqa: E402

client = TestClient(app)


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    """The widget upload endpoint is rate-limited at 5/min — tests that
    exercise multi-file flows easily blow past it. Reset between tests."""
    _rl_reset()
    yield
    _rl_reset()


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


def _ensure_setting(db, key: str, value: str):
    row = (
        db.query(Setting)
        .filter_by(organization_slug=DEFAULT_ORG_SLUG, key=key)
        .first()
    )
    if row:
        row.value = value
    else:
        db.add(Setting(
            organization_slug=DEFAULT_ORG_SLUG,
            key=key, value=value, value_type="string",
        ))


def _seed_conv_and_quote(*, goods_inc=200.0, with_artwork_offer=False) -> tuple[int, str]:
    """Returns (conversation_id, external_id)."""
    eid = f"phase-f-{time.time_ns()}"
    with db_session() as db:
        conv = Conversation(
            organization_slug=DEFAULT_ORG_SLUG,
            external_id=eid, channel="web", messages=[],
        )
        if with_artwork_offer:
            conv.messages = [
                {"role": "assistant", "content": "Want artwork? [ARTWORK_UPLOAD]"},
            ]
        db.add(conv); db.flush()
        # Pending quote with no artwork
        q = Quote(
            organization_slug=DEFAULT_ORG_SLUG,
            conversation_id=conv.id,
            product_key="business_cards",
            specs={"quantity": 500},
            base_price=goods_inc / 1.135,
            surcharges=[],
            final_price_ex_vat=round(goods_inc / 1.135, 2),
            vat_amount=round(goods_inc - goods_inc / 1.135, 2),
            final_price_inc_vat=goods_inc,
            artwork_cost=0.0,
            total=goods_inc,
            status="pending_approval",
        )
        db.add(q)
        # Ensure shipping settings exist
        _ensure_setting(db, "shipping_fee_inc_vat", "15.00")
        _ensure_setting(db, "free_shipping_threshold_inc_vat", "100.00")
        _ensure_setting(
            db, "shop_address",
            "Ballymount Cross Business Park, 7, Ballymount, Dublin, D24 E5NH, Ireland",
        )
        db.commit()
        return conv.id, eid


# ---------------------------------------------------------------------------
# apply_shipping_to_quote
# ---------------------------------------------------------------------------


def test_apply_shipping_collect_zero():
    """Collection never has shipping."""
    cid, _ = _seed_conv_and_quote(goods_inc=50.0)
    with db_session() as db:
        from pricing_engine import apply_shipping_to_quote
        q = db.query(Quote).filter_by(conversation_id=cid).first()
        result = apply_shipping_to_quote(db, q, "collect", organization_slug=DEFAULT_ORG_SLUG)
        assert result["shipping_inc_vat"] == 0.0
        assert result["applies"] is False
        assert q.shipping_cost_inc_vat == 0.0


def test_apply_shipping_delivery_below_threshold_charges():
    """Delivery + goods €50 (under €100) → €15 inc VAT shipping."""
    cid, _ = _seed_conv_and_quote(goods_inc=50.0)
    with db_session() as db:
        from pricing_engine import apply_shipping_to_quote
        q = db.query(Quote).filter_by(conversation_id=cid).first()
        result = apply_shipping_to_quote(db, q, "delivery", organization_slug=DEFAULT_ORG_SLUG)
        assert result["shipping_inc_vat"] == 15.0
        # ex VAT: 15 / 1.23 = 12.20 (rounded)
        assert abs(result["shipping_ex_vat"] - 12.20) < 0.01
        assert result["free_shipping"] is False
        assert q.shipping_cost_inc_vat == 15.0
        # Total now = goods + shipping
        assert abs(q.total - 65.0) < 0.01


def test_apply_shipping_delivery_above_threshold_free():
    """Delivery + goods €200 (over €100) → free shipping."""
    cid, _ = _seed_conv_and_quote(goods_inc=200.0)
    with db_session() as db:
        from pricing_engine import apply_shipping_to_quote
        q = db.query(Quote).filter_by(conversation_id=cid).first()
        result = apply_shipping_to_quote(db, q, "delivery", organization_slug=DEFAULT_ORG_SLUG)
        assert result["shipping_inc_vat"] == 0.0
        assert result["free_shipping"] is True
        assert result["applies"] is True
        assert q.total == 200.0  # unchanged — free shipping doesn't add to total


def test_apply_shipping_at_exact_threshold_is_free():
    """Goods inc VAT exactly €100 → free (≥, not strict >)."""
    cid, _ = _seed_conv_and_quote(goods_inc=100.0)
    with db_session() as db:
        from pricing_engine import apply_shipping_to_quote
        q = db.query(Quote).filter_by(conversation_id=cid).first()
        result = apply_shipping_to_quote(db, q, "delivery", organization_slug=DEFAULT_ORG_SLUG)
        assert result["shipping_inc_vat"] == 0.0
        assert result["free_shipping"] is True


# ---------------------------------------------------------------------------
# /widget/conversations/{id}/customer-info
# ---------------------------------------------------------------------------


def test_form_submit_collection_autofills_shop_address():
    cid, eid = _seed_conv_and_quote(goods_inc=50.0)
    body = {
        "external_id": eid,
        "name": "Sebastian Test",
        "email": "seb@example.ie",
        "is_company": False,
        "is_returning_customer": False,
        "delivery_method": "collect",
    }
    r = client.post(f"/widget/conversations/{cid}/customer-info", json=body)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["ok"] is True
    # Conversation now has the shop address as delivery_address
    with db_session() as db:
        conv = db.query(Conversation).filter_by(id=cid).first()
        assert conv.delivery_method == "collect"
        addr = conv.delivery_address or {}
        assert addr.get("postcode") == "D24 E5NH"
        # First line should be the business park name
        assert "Ballymount" in (addr.get("address1") or "")
        # No shipping (it's collection)
        q = db.query(Quote).filter_by(conversation_id=cid).first()
        assert q.shipping_cost_inc_vat == 0.0


def test_form_submit_delivery_below_threshold_charges_shipping():
    cid, eid = _seed_conv_and_quote(goods_inc=50.0)
    body = {
        "external_id": eid,
        "name": "Sebastian Test",
        "email": "seb@example.ie",
        "is_company": True,
        "is_returning_customer": False,
        "delivery_method": "delivery",
        "delivery_address": {
            "address1": "12 Main Street",
            "address4": "Dublin 2",
            "postcode": "D02 X1Y2",
        },
    }
    r = client.post(f"/widget/conversations/{cid}/customer-info", json=body)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["shipping"]["applied"] is True
    assert data["shipping"]["shipping_inc_vat"] == 15.0
    with db_session() as db:
        q = db.query(Quote).filter_by(conversation_id=cid).first()
        assert q.shipping_cost_inc_vat == 15.0
        assert abs(q.total - 65.0) < 0.01


def test_form_rejects_invalid_eircode():
    cid, eid = _seed_conv_and_quote()
    r = client.post(
        f"/widget/conversations/{cid}/customer-info",
        json={
            "external_id": eid,
            "name": "Sebastian Test",
            "email": "seb@example.ie",
            "delivery_method": "delivery",
            "delivery_address": {
                "address1": "12 Main Street",
                "postcode": "INVALID",
            },
        },
    )
    assert r.status_code == 422
    assert "eircode" in r.json()["detail"].lower()


def test_form_rejects_disposable_email():
    cid, eid = _seed_conv_and_quote()
    r = client.post(
        f"/widget/conversations/{cid}/customer-info",
        json={
            "external_id": eid,
            "name": "Spam Test",
            "email": "throwaway@yopmail.com",
            "delivery_method": "collect",
        },
    )
    assert r.status_code == 422
    # Pydantic wraps validator errors — body should mention disposable
    detail = str(r.json()["detail"]).lower()
    assert "disposable" in detail


def test_form_rejects_external_id_mismatch():
    cid, _ = _seed_conv_and_quote()
    r = client.post(
        f"/widget/conversations/{cid}/customer-info",
        json={
            "external_id": "wrong-session",
            "name": "Mallory",
            "email": "mal@example.ie",
            "delivery_method": "collect",
        },
    )
    assert r.status_code == 403


def test_form_blocks_when_artwork_required_but_missing():
    """Phase F gate: customer was offered the upload button (Craig
    emitted [ARTWORK_UPLOAD] earlier) but didn't upload anything →
    form submit should 409."""
    cid, eid = _seed_conv_and_quote(goods_inc=50.0, with_artwork_offer=True)
    r = client.post(
        f"/widget/conversations/{cid}/customer-info",
        json={
            "external_id": eid,
            "name": "Sebastian Test",
            "email": "seb@example.ie",
            "delivery_method": "collect",
        },
    )
    assert r.status_code == 409
    assert "artwork" in r.json()["detail"].lower()


# ---------------------------------------------------------------------------
# /widget/conversations/{id}/upload-artwork
# ---------------------------------------------------------------------------


def test_upload_persists_url_on_quote():
    """Phase G — upload now appends to artwork_files list and returns
    the full list with proxy URLs (not raw GCS URLs)."""
    cid, eid = _seed_conv_and_quote()
    fake_pdf = b"%PDF-1.4\n%test fake pdf for unit test\n"
    r = client.post(
        f"/widget/conversations/{cid}/upload-artwork",
        data={"external_id": eid},
        files={"file": ("design.pdf", io.BytesIO(fake_pdf), "application/pdf")},
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["ok"] is True
    assert data["count"] == 1
    files = data["files"]
    assert len(files) == 1
    assert files[0]["filename"] == "design.pdf"
    assert files[0]["size"] == len(fake_pdf)
    # Proxy URL shape: /admin/api/orgs/{slug}/quotes/{id}/artwork/0/file
    assert "/artwork/0/file" in files[0]["url"]
    with db_session() as db:
        q = db.query(Quote).filter_by(conversation_id=cid).first()
        assert q.artwork_files is not None
        assert len(q.artwork_files) == 1
        # Internal storage URL (gs:// in prod, /artwork-local/ in dev)
        internal_url = q.artwork_files[0]["url"]
        assert (
            internal_url.startswith("/artwork-local/")
            or internal_url.startswith("gs://")
        ), f"unexpected url shape: {internal_url}"
        # Singular columns mirror the first file (back-compat)
        assert q.artwork_file_url == internal_url
        assert q.artwork_file_name == "design.pdf"
        assert q.artwork_file_size == len(fake_pdf)


def test_upload_flips_customer_has_own_artwork_to_true():
    """Phase G refined — uploading a file IS the answer to the artwork
    question. The upload endpoint flips customer_has_own_artwork to True
    even if it was previously None or False."""
    cid, eid = _seed_conv_and_quote()
    # Pre-condition: flag is None
    with db_session() as db:
        conv = db.query(Conversation).filter_by(id=cid).first()
        assert conv.customer_has_own_artwork is None

    r = client.post(
        f"/widget/conversations/{cid}/upload-artwork",
        data={"external_id": eid},
        files={"file": ("art.pdf", io.BytesIO(b"%PDF-1.4\n"), "application/pdf")},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["first_upload"] is True

    with db_session() as db:
        conv = db.query(Conversation).filter_by(id=cid).first()
        assert conv.customer_has_own_artwork is True

    # Second upload should NOT mark as first_upload again
    _rl_reset()
    r2 = client.post(
        f"/widget/conversations/{cid}/upload-artwork",
        data={"external_id": eid},
        files={"file": ("back.pdf", io.BytesIO(b"%PDF-1.4\n"), "application/pdf")},
    )
    assert r2.status_code == 200
    assert r2.json()["first_upload"] is False


def test_upload_overrides_previous_false_artwork_flag():
    """If the customer first asked for the design service then changed
    their mind and uploaded, the upload should still flip the flag to
    True (truth = what's in the file list)."""
    cid, eid = _seed_conv_and_quote()
    with db_session() as db:
        conv = db.query(Conversation).filter_by(id=cid).first()
        conv.customer_has_own_artwork = False  # design service path
        db.commit()

    r = client.post(
        f"/widget/conversations/{cid}/upload-artwork",
        data={"external_id": eid},
        files={"file": ("changed-mind.pdf", io.BytesIO(b"x"), "application/pdf")},
    )
    assert r.status_code == 200
    assert r.json()["first_upload"] is True

    with db_session() as db:
        conv = db.query(Conversation).filter_by(id=cid).first()
        assert conv.customer_has_own_artwork is True


def test_upload_appends_multiple_files():
    """Phase G — successive uploads APPEND to the list, not replace."""
    cid, eid = _seed_conv_and_quote()
    for n in ("front.pdf", "back.pdf", "ref.png"):
        _rl_reset()  # widget_upload limit is 5/min — reset between calls
        ct = "application/pdf" if n.endswith(".pdf") else "image/png"
        r = client.post(
            f"/widget/conversations/{cid}/upload-artwork",
            data={"external_id": eid},
            files={"file": (n, io.BytesIO(b"fake"), ct)},
        )
        assert r.status_code == 200, r.text
    final = r.json()
    assert final["count"] == 3
    assert [f["filename"] for f in final["files"]] == ["front.pdf", "back.pdf", "ref.png"]


def test_upload_caps_at_max():
    """11th upload is rejected with 409."""
    cid, eid = _seed_conv_and_quote()
    for i in range(10):
        _rl_reset()
        client.post(
            f"/widget/conversations/{cid}/upload-artwork",
            data={"external_id": eid},
            files={"file": (f"f{i}.pdf", io.BytesIO(b"x"), "application/pdf")},
        )
    _rl_reset()
    r = client.post(
        f"/widget/conversations/{cid}/upload-artwork",
        data={"external_id": eid},
        files={"file": ("f10.pdf", io.BytesIO(b"x"), "application/pdf")},
    )
    assert r.status_code == 409
    assert "Already have" in r.json()["detail"]


def test_delete_artwork_removes_one():
    """DELETE endpoint pops the file at the given index."""
    cid, eid = _seed_conv_and_quote()
    for n in ("a.pdf", "b.pdf", "c.pdf"):
        _rl_reset()
        client.post(
            f"/widget/conversations/{cid}/upload-artwork",
            data={"external_id": eid},
            files={"file": (n, io.BytesIO(b"x"), "application/pdf")},
        )
    _rl_reset()
    r = client.delete(
        f"/widget/conversations/{cid}/upload-artwork/1?external_id={eid}",
    )
    assert r.status_code == 200
    data = r.json()
    assert data["count"] == 2
    names = [f["filename"] for f in data["files"]]
    assert names == ["a.pdf", "c.pdf"]


def test_delete_artwork_out_of_range():
    cid, eid = _seed_conv_and_quote()
    r = client.delete(
        f"/widget/conversations/{cid}/upload-artwork/5?external_id={eid}",
    )
    assert r.status_code == 404


def test_upload_rejects_unknown_extension():
    cid, eid = _seed_conv_and_quote()
    r = client.post(
        f"/widget/conversations/{cid}/upload-artwork",
        data={"external_id": eid},
        files={"file": ("malware.exe", io.BytesIO(b"MZ"), "application/x-msdownload")},
    )
    assert r.status_code == 415


def test_upload_rejects_external_id_mismatch():
    cid, _ = _seed_conv_and_quote()
    r = client.post(
        f"/widget/conversations/{cid}/upload-artwork",
        data={"external_id": "wrong"},
        files={"file": ("design.pdf", io.BytesIO(b"%PDF"), "application/pdf")},
    )
    assert r.status_code == 403


def test_upload_then_form_submit_passes_artwork_gate():
    """The full happy path: artwork uploaded → form submits → gate
    passes (no 409)."""
    cid, eid = _seed_conv_and_quote(goods_inc=50.0, with_artwork_offer=True)
    # Upload first
    r = client.post(
        f"/widget/conversations/{cid}/upload-artwork",
        data={"external_id": eid},
        files={"file": ("design.pdf", io.BytesIO(b"%PDF-1.4\nfake"), "application/pdf")},
    )
    assert r.status_code == 200
    # Now submit the form
    r = client.post(
        f"/widget/conversations/{cid}/customer-info",
        json={
            "external_id": eid,
            "name": "Sebastian Test",
            "email": "seb@example.ie",
            "delivery_method": "collect",
        },
    )
    assert r.status_code == 200, r.text
