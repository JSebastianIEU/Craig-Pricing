"""
Stage 1 — READ-ONLY validation probe for PrintLogic integration.

Runs two safe calls against the real PrintLogic API to verify that the
stored `printlogic_api_key` for a tenant:
  1. Authenticates (HTTP 200, not 401).
  2. Is scoped to the right firm — we can read a known order_number
     that Justin gave us, and the response carries real data (not the
     ambiguous `{"result":"ok"}` shape we've seen).

ZERO destructive calls. Does not create, update, or delete anything
in PrintLogic. Safe to run any number of times.

Usage:
    python -m scripts.probe_printlogic <org_slug> [<known_order_number>] [<known_customer_email>]

Examples:
    python -m scripts.probe_printlogic just-print 1519487 info@just-print.ie
    python -m scripts.probe_printlogic just-print    # interactive prompts

Exit codes:
    0 — both probes passed; safe to proceed to Stage 2
    1 — failed; do NOT enable live pushes; investigate

The probe output is designed to be copy-pasted into a ticket/email for
the PrintLogic team if something is off.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db import db_session
import printlogic
from pricing_engine import _get_setting


def _ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    try:
        value = input(f"{prompt}{suffix}: ").strip()
    except EOFError:
        value = ""
    return value or default


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: python -m scripts.probe_printlogic <org_slug> [<order_number>] [<customer_email>]")
        return 1
    org_slug = sys.argv[1]
    order_number = sys.argv[2] if len(sys.argv) > 2 else None
    customer_email = sys.argv[3] if len(sys.argv) > 3 else None

    with db_session() as db:
        api_key = _get_setting(db, "printlogic_api_key", default="", organization_slug=org_slug)
        dry_run = _get_setting(db, "printlogic_dry_run", default="true", organization_slug=org_slug)

    if not api_key:
        print(f"\u2717 No printlogic_api_key set for tenant '{org_slug}'.")
        print("  Set it in the dashboard (Connections \u2192 PrintLogic) or via SQL, then retry.")
        return 1

    print(f"\u2713 Tenant: {org_slug}")
    print(f"\u2713 api_key loaded (length={len(api_key)})")
    print(f"  printlogic_dry_run currently: {dry_run!r}")
    print()

    if not order_number:
        order_number = _ask("Known real order_number from Justin's PrintLogic UI", "")
    if not customer_email:
        customer_email = _ask("Known customer email (e.g. info@just-print.ie)", "")

    # ── Probe 1: get_order_detail ───────────────────────────────────
    all_green = True
    if order_number:
        print(f"[probe 1/2] get_order_detail({order_number!r}) \u2014 read-only...")
        try:
            result = asyncio.run(printlogic.get_order_detail(str(order_number), api_key))
        except Exception as e:
            print(f"  \u2717 Crashed: {e}")
            result = {"ok": False, "error": f"crash:{e}"}

        if result.get("ok"):
            order = result.get("order") or {}
            print(f"  \u2713 OK  (order #{order.get('order_number')}, total={order.get('order_total')})")
        elif result.get("ambiguous"):
            print(f"  \u2717 AMBIGUOUS response \u2014 PrintLogic returned: {result.get('raw')}")
            print("    This means the api_key authenticated but there's no real data")
            print("    at the given order_number. Either the number is wrong, or the")
            print("    key is scoped to a different firm. Ask Alexander to double-check.")
            all_green = False
        else:
            print(f"  \u2717 FAILED: {result.get('error')}")
            all_green = False
    else:
        print("[probe 1/2] SKIPPED \u2014 no order_number provided")

    print()

    # ── Probe 2: find_customer ──────────────────────────────────────
    if customer_email:
        print(f"[probe 2/2] find_customer(email={customer_email!r}) \u2014 read-only...")
        try:
            result = asyncio.run(printlogic.find_customer(api_key, email=customer_email))
        except Exception as e:
            print(f"  \u2717 Crashed: {e}")
            result = {"ok": False, "error": f"crash:{e}"}

        if result.get("ok") and result.get("customer"):
            c = result["customer"]
            print(f"  \u2713 OK  found customer_id={c.get('customer_id') or c.get('id')}")
        elif result.get("ok"):
            print(f"  \u26a0 api_key works, but no customer matched that email \u2014 harmless.")
        else:
            print(f"  \u2717 FAILED: {result.get('error')}")
            all_green = False
    else:
        print("[probe 2/2] SKIPPED \u2014 no customer_email provided")

    print()

    # ── Summary ──────────────────────────────────────────────────────
    if all_green:
        print("\u2713 READY FOR STAGE 2 \u2014 auth + firm binding validated.")
        print("  Next: run the widget end-to-end with printlogic_dry_run=true")
        print("  and verify the dashboard shows a DRY-xxxx badge.")
        return 0
    else:
        print("\u2717 STOP \u2014 do NOT flip printlogic_dry_run=false yet.")
        print("  Investigate above errors with Alexander before proceeding.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
