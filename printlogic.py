"""
Thin async client for the PrintLogic JSON API.

PrintLogic is the order-management system our first tenant (Just Print)
uses. They expose a single endpoint (`/api.php`) that dispatches on an
`action` field in the JSON body, with the `api_key` carried in the URL
query string. See `docs/printlogic-API-2.pdf` for the source spec and
`docs/printlogic-integration.md` (if written) for our runbook.

Design principles — read these before editing:

  1. NEVER raise exceptions to the caller. Every function returns a
     normalised `{"ok": bool, ...}` dict. Lets the orchestrator do clean
     state-machine transitions on `Quote`.

  2. `create_order(..., dry_run=True)` MUST NOT touch the network. This
     is the core safety primitive — the tenant-level `printlogic_dry_run`
     setting defaults to `"true"` and we only flip it false in supervised
     ceremonies. A dry-run call returns a synthetic `DRY-xxxx` order_id
     so the rest of the flow can be exercised end-to-end with zero risk.

  3. A 200 OK that DOES NOT contain `order_id` is treated as AMBIGUOUS,
     not as success. We saw a real probe return `{"result":"ok",
     "request_length":1, "post_length":0, "raw_body_length":53}` for a
     nonexistent order — that shape must surface a warning, never get
     persisted as a valid id.

  4. API key is NEVER logged. It lives in the URL query string; callers
     pass it explicitly every time (no module-level state).
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any

import httpx


PL_BASE = "https://www.printlogicsystem.com/api.php"
TIMEOUT = httpx.Timeout(15.0, connect=5.0)


def _endpoint(api_key: str) -> str:
    """Endpoint URL with the api_key as query param (PrintLogic convention)."""
    return f"{PL_BASE}?api_key={api_key}"


async def _post(action: str, api_key: str, **extra: Any) -> tuple[int, dict | None, str | None]:
    """
    Shared POST helper.

    Returns `(status_code, parsed_json or None, error_str or None)`.
    Never raises — caller interprets the triple.
    """
    body = {"action": action, **extra}
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            r = await client.post(_endpoint(api_key), json=body)
    except httpx.TimeoutException:
        return (0, None, "timeout")
    except httpx.HTTPError as e:
        return (0, None, f"network_error:{type(e).__name__}")
    except Exception as e:
        return (0, None, f"unexpected:{type(e).__name__}")

    try:
        parsed = r.json() if r.text else None
    except ValueError:
        parsed = None

    if r.status_code >= 400:
        # Surface the body if we have it, otherwise just the status
        body_preview = (r.text or "")[:500]
        return (r.status_code, parsed, f"http_{r.status_code}:{body_preview}")

    return (r.status_code, parsed, None)


def _is_ambiguous_ok(parsed: dict | None) -> bool:
    """
    A 200 response from PrintLogic that doesn't carry the expected
    success fields is suspicious. We want to tag it rather than guess.

    Concretely: the probe against a nonexistent order_number returned
      {"result":"ok","request_length":1,"post_length":0,"raw_body_length":53}
    No `order_id`, no `order_number`, no `customer_id`. We never treat
    that as success.
    """
    if not isinstance(parsed, dict):
        return True
    # A real create_order response has `order_id`; a real get_order_detail
    # response has `order_number` (per the spec PDF). Either is acceptable.
    if "order_id" in parsed or "order_number" in parsed:
        return False
    # A pure {"status":"ok"} (e.g. update_order_status) is fine.
    if parsed.get("status") == "ok" and len(parsed) <= 2:
        return False
    return True


# ---------------------------------------------------------------------------
# Read-only helpers (safe for validation + staging probes)
# ---------------------------------------------------------------------------


async def find_customer(
    api_key: str,
    *,
    email: str | None = None,
    phone: str | None = None,
    customer_uid: str | None = None,
) -> dict:
    """
    Exact-match customer lookup. Per Alexander (2026-04-24), the API's
    `find_customer` action supports email, phone, mobile, and the
    customer unique stamp.

    Returns `{ok, customer, raw, error}`.
      - `customer` is the record dict on a hit, None on a miss.
      - `ok=False` only when the HTTP call itself failed (auth / network).
        A 200 with "not found" is `ok=True, customer=None`.
    """
    args: dict[str, Any] = {}
    if email: args["email"] = email
    if phone: args["phone"] = phone
    if customer_uid: args["customer_uid"] = customer_uid
    if not args:
        return {"ok": False, "customer": None, "raw": None, "error": "no_identifier"}

    status, parsed, err = await _post("find_customer", api_key, **args)
    if err:
        return {"ok": False, "customer": None, "raw": parsed, "error": err}

    # Heuristic: a real hit has at least a customer_id / customer_uid field.
    if isinstance(parsed, dict) and (
        parsed.get("customer_id") or parsed.get("customer_uid") or parsed.get("id")
    ):
        return {"ok": True, "customer": parsed, "raw": parsed, "error": None}
    # Anything else (empty, ambiguous) we treat as "not found"
    return {"ok": True, "customer": None, "raw": parsed, "error": None}


async def get_order_detail(order_number: str, api_key: str) -> dict:
    """
    Read an order back from PrintLogic. Used both for (a) audit after
    creating a new order and (b) the Stage 1 probe that validates the
    api_key is scoped to the right firm.

    Returns `{ok, order, raw, error, ambiguous}`.
    """
    status, parsed, err = await _post("get_order_detail", api_key, order_number=str(order_number))
    if err:
        return {"ok": False, "order": None, "raw": parsed, "error": err, "ambiguous": False}

    if _is_ambiguous_ok(parsed):
        return {
            "ok": False, "order": None, "raw": parsed,
            "error": "ambiguous_ok", "ambiguous": True,
        }
    return {"ok": True, "order": parsed, "raw": parsed, "error": None, "ambiguous": False}


# ---------------------------------------------------------------------------
# Destructive operations (guarded by dry_run)
# ---------------------------------------------------------------------------


def _synthetic_dry_run_id(quote_id: int | str) -> str:
    """Deterministic-ish DRY-xxxx marker that stays stable for the same
    quote within the same second. Good enough to spot in the dashboard."""
    seed = f"{quote_id}:{int(time.time())}".encode()
    return "DRY-" + hashlib.sha1(seed).hexdigest()[:8].upper()


async def create_order(
    payload: dict,
    api_key: str,
    *,
    dry_run: bool = True,
    quote_id_for_dry: int | str | None = None,
) -> dict:
    """
    POST `create_order` to PrintLogic — or simulate it when `dry_run`.

    Returns `{ok, order_id, customer_id, dry_run, ambiguous, raw, error}`.

    Safety behaviour:
      - `dry_run=True` (DEFAULT) → zero network traffic. Returns a
        synthetic DRY-xxxx order_id derived from `quote_id_for_dry`.
        Ensures we can wire the whole dashboard without ever risking
        a real write to Justin's system.
      - `dry_run=False` → actually POSTs. A 200 without `order_id` is
        flagged `ambiguous=True, ok=False` — we persist a warning and
        never set the real order_id column.

    `payload` is the PrintLogic create_order body WITHOUT the `action`
    key (we add that here). Keep `customer_*` and `order_items` there.
    """
    if dry_run:
        order_id = _synthetic_dry_run_id(quote_id_for_dry or "unknown")
        print(
            f"[printlogic] DRY RUN — would POST create_order "
            f"(payload size: {len(json.dumps(payload))} bytes). "
            f"Synthetic order_id={order_id}",
            flush=True,
        )
        return {
            "ok": True,
            "order_id": order_id,
            "customer_id": "DRY-CUST",
            "dry_run": True,
            "ambiguous": False,
            "raw": None,
            "error": None,
        }

    status, parsed, err = await _post("create_order", api_key, **payload)
    if err:
        return {
            "ok": False, "order_id": None, "customer_id": None,
            "dry_run": False, "ambiguous": False,
            "raw": parsed, "error": err,
        }

    if _is_ambiguous_ok(parsed):
        return {
            "ok": False, "order_id": None, "customer_id": None,
            "dry_run": False, "ambiguous": True,
            "raw": parsed, "error": "ambiguous_ok",
        }

    # Happy path — extract the ids PrintLogic gave us
    order_id = str(parsed.get("order_id")) if parsed and parsed.get("order_id") else None
    customer_id = str(parsed.get("customer_id")) if parsed and parsed.get("customer_id") else None
    if not order_id:
        # Shouldn't reach here (ambiguous check catches it) but belt-and-suspenders
        return {
            "ok": False, "order_id": None, "customer_id": None,
            "dry_run": False, "ambiguous": True,
            "raw": parsed, "error": "no_order_id_in_response",
        }
    return {
        "ok": True, "order_id": order_id, "customer_id": customer_id,
        "dry_run": False, "ambiguous": False, "raw": parsed, "error": None,
    }


async def update_order_status(order_number: str, status: str, api_key: str) -> dict:
    """
    Change the lifecycle state of an order (e.g. to 'Cancelled' for our
    rollback path after a mistaken push). Returns `{ok, raw, error}`.
    """
    code, parsed, err = await _post(
        "update_order_status", api_key,
        order_number=str(order_number), status=status,
    )
    if err:
        return {"ok": False, "raw": parsed, "error": err}
    # The PDF spec says `{"status":"ok"}` but the real API returns
    # `{"result":"ok"}`. Accept either shape — verified against live
    # probe `scripts/probe_printlogic_ops_cycle.py`.
    if isinstance(parsed, dict) and (
        parsed.get("status") == "ok" or parsed.get("result") == "ok"
    ):
        return {"ok": True, "raw": parsed, "error": None}
    return {"ok": False, "raw": parsed, "error": "unexpected_response"}
