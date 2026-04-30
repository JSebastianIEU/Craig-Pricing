"""
Probe what PrintLogic actually stores for orders we've created.

Goals:
  1. Try every list-orders-style action to see if we can enumerate
  2. Pull get_order_detail for our known sentinel order_numbers and
     dump the FULL response — that's the source of truth for "what
     fields does PrintLogic echo back / enrich / accept silently"
  3. Same for the customers we created (get_customer_details)
  4. Print a clean diff: "we sent X, PrintLogic returned Y" so we can
     see which fields stuck and which were dropped or normalised.

Read-only. Safe to re-run.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx


API_KEY = "GA5PQHGaxDl3IJJVuIEZpard9OgCyPOFmegd4W4K"
URL = f"https://www.printlogicsystem.com/api.php?api_key={API_KEY}"

# Known sentinel orders we've created during probes/dashboard tests.
# (49454 = scripts/probe_printlogic_ops_cycle.py; 49412/49413 = early empty-body
# probes that we cancelled.) Add new ones the user created via the dashboard.
KNOWN_ORDER_NUMBERS = ["49412", "49413", "49454"]


def _section(title: str) -> None:
    print()
    print("=" * 78)
    print(f"  {title}")
    print("=" * 78)


def _dump(label: str, body) -> None:
    print(f"\n  [{label}]")
    if isinstance(body, dict):
        s = json.dumps(body, indent=2, default=str, ensure_ascii=False)
        if len(s) > 1500:
            print(s[:1500] + "\n  ... [truncated]")
        else:
            print(s)
    else:
        print(f"  raw: {str(body)[:500]}")


async def _post(client: httpx.AsyncClient, action: str, **extra) -> dict:
    r = await client.post(URL, json={"action": action, **extra})
    try:
        return r.json()
    except Exception:
        return {"_raw_text": r.text[:500], "_status": r.status_code}


async def _read_recent_dashboard_orders() -> list[str]:
    """
    Pull the most-recent test order_number from the Setting table so we
    include any the user just created via the dashboard.
    """
    try:
        from db import db_session
        from db.models import Setting
        with db_session() as db:
            rows = (
                db.query(Setting)
                .filter(Setting.key == "printlogic_last_test_order_number")
                .all()
            )
            return [r.value for r in rows if r.value]
    except Exception as e:
        print(f"  [warn] couldn't read dashboard orders from DB: {e}")
        return []


async def main() -> None:
    timeout = httpx.Timeout(20.0, connect=5.0)
    async with httpx.AsyncClient(timeout=timeout) as client:

        # ── 1. Can we list orders? ─────────────────────────────────────
        _section("1. CAN WE LIST ORDERS? (try every reasonable action name)")
        for action in (
            "get_orders", "list_orders", "get_all_orders",
            "get_recent_orders", "get_order_list",
            "find_order", "search_orders",
        ):
            res = await _post(client, action)
            # Just show the response keys so we don't drown in 9k rows
            if isinstance(res, dict):
                keys = sorted(res.keys())[:20]
                print(f"  {action:25s} -> result={res.get('result')!r}  keys={keys}")
                # If it returned a usable result with order data, dump first item
                if res.get("result") == "ok" and len(keys) > 4:
                    sample_key = next(
                        (k for k in keys if k not in (
                            "result", "request_length", "post_length",
                            "raw_body_length",
                        )),
                        None,
                    )
                    if sample_key:
                        sample_val = res.get(sample_key)
                        if isinstance(sample_val, dict):
                            sub_keys = sorted(sample_val.keys())[:10]
                            print(f"  {' '*25}    first record [{sample_key}] keys: {sub_keys}")

        # ── 2. Pull full detail for known sentinel orders ──────────────
        _section("2. FULL get_order_detail FOR OUR SENTINEL ORDERS")

        dashboard_orders = await _read_recent_dashboard_orders()
        all_orders = list(dict.fromkeys(KNOWN_ORDER_NUMBERS + dashboard_orders))
        print(f"  Looking up: {all_orders}")

        for order_num in all_orders:
            res = await _post(client, "get_order_detail", order_number=order_num)
            _dump(f"get_order_detail(order_number={order_num})", res)

        # ── 3. Field variations (does it accept order_id?) ─────────────
        _section("3. FIELD-NAME VARIATIONS (order_id vs order_number)")
        if all_orders:
            test = all_orders[-1]
            for field in ("order_number", "order_id", "id", "number"):
                res = await _post(client, "get_order_detail", **{field: test})
                if isinstance(res, dict):
                    keys = sorted(res.keys())
                    has_real_data = any(
                        k in res for k in (
                            "order_number", "order_description",
                            "customer_name", "items", "order_items",
                        )
                    )
                    flag = "DATA" if has_real_data else "ambiguous"
                    print(f"  field={field:15s} ({test}) -> {flag}  keys={keys[:10]}")

        # ── 4. What customer fields stuck? ─────────────────────────────
        _section("4. CUSTOMER DETAILS FOR CUSTOMERS WE CREATED")
        # Pull customer_id from DB if we have it persisted
        try:
            from db import db_session
            from db.models import Setting
            with db_session() as db:
                rows = (
                    db.query(Setting)
                    .filter(Setting.key == "printlogic_last_test_customer_id")
                    .all()
                )
                customer_ids = [r.value for r in rows if r.value]
        except Exception:
            customer_ids = []

        if not customer_ids:
            print("  (no persisted customer_ids — skipping)")
        else:
            for cid in customer_ids:
                res = await _post(client, "get_customer_details", customer_id=cid)
                _dump(f"get_customer_details(customer_id={cid})", res)

        # ── 5. What does the API echo back about itself? ───────────────
        _section("5. ACCOUNT / FIRM / META actions")
        for action in ("get_account_info", "get_firm", "whoami",
                       "get_user_info", "get_api_info"):
            res = await _post(client, action)
            if isinstance(res, dict):
                print(f"  {action:25s} -> keys={sorted(res.keys())[:12]}")

    print()
    print("=" * 78)
    print("  PROBE COMPLETE — all calls were read-only.")
    print("=" * 78)


if __name__ == "__main__":
    asyncio.run(main())
