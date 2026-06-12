#!/usr/bin/env python3
"""Product Test Report generator — drive test conversations against prod
Craig, harvest transcripts + quote PDFs, build a navigable master PDF,
then delete every trace from prod.

WHAT IT PRODUCES (in --out-dir, default ~/JustPrint/craig-test-reports/<date>/):
    Craig-Product-Test-Report.pdf   cover + clickable TOC + one section per
                                    product group with full chat transcripts
    quotes/JP-XXXX.pdf              every quote PDF generated, by quote number
    run-manifest.json               machine-readable run record (ids, results)

WHY REAL /chat AND NOT THE v35 TEST-CHAT SANDBOX:
    The sandbox injects a "TEST MODE" system message that skips the whole
    customer funnel (no artwork buttons, no [QUOTE_READY]) — the report
    would show behaviour no customer ever sees. We use the real /chat and
    clean up after ourselves instead.

NO-EMAIL GUARANTEE:
    The v33 approval email fires only on the WIDGET FORM submit
    (widget_api.py) or a Missive confirm_order — never when contact details
    are given as chat text. The suite only ever gives contact as chat text.

CLEANUP SEMANTICS:
    Every conversation id this run creates is tracked in the manifest and
    deleted at the end (DELETE cascades the quotes). Nothing outside that
    explicit id list is ever touched. --keep-data skips deletion (e.g. to
    inspect in the dashboard); re-run cleanup later with --cleanup-manifest.

USAGE:
    source .venv/bin/activate && set -a && source .env && set +a
    python -m scripts.generate_product_test_report --dry-run
    python -m scripts.generate_product_test_report --groups posters,ncr
    python -m scripts.generate_product_test_report            # full suite
    python -m scripts.generate_product_test_report --cleanup-manifest <path>

Requires in env: STRATEGOS_JWT_SECRET (harvest + cleanup are JWT-gated).
"""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as _dt
import json
import os
import re
import sys
import threading
import time
from pathlib import Path

import httpx
import jwt as pyjwt

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.test_report_scenarios import GROUPS, STYLE_LABELS  # noqa: E402

DEFAULT_BASE = "https://craig-pricing-ihhdpigdna-ew.a.run.app"
ORG = "just-print"

_print_lock = threading.Lock()


def log(msg: str) -> None:
    with _print_lock:
        print(msg, flush=True)


# ───────────────────────── auth ──────────────────────────────────────

def mint_jwt() -> str:
    """Fresh 5-min strategos_admin token (run can outlast one token)."""
    secret = os.environ.get("STRATEGOS_JWT_SECRET")
    if not secret:
        sys.exit("STRATEGOS_JWT_SECRET not in env — `set -a && source .env && set +a` first.")
    now = int(time.time())
    return pyjwt.encode(
        {
            "email": "js@strategos.ai", "sub": "js@strategos.ai",
            "org_slug": ORG, "role": "strategos_admin",
            "iss": "strategos-dashboard", "iat": now, "exp": now + 300,
        },
        secret, algorithm="HS256",
    )


def auth_headers() -> dict:
    return {"Authorization": f"Bearer {mint_jwt()}"}


# ───────────────────────── run phase ─────────────────────────────────

def drive_scenario(client: httpx.Client, base: str, group: dict, scenario: dict) -> dict:
    """Run one scenario's turns through /chat. Returns a result record."""
    cid = None
    transcript: list[dict] = []   # [{"role": "user"/"assistant", "content": ...}]
    error = None
    for turn in scenario["turns"]:
        reply = None
        for attempt in range(4):
            try:
                r = client.post(f"{base}/chat", json={
                    "message": turn, "conversation_id": cid,
                    "organization_slug": ORG, "channel": "web",
                })
                if r.status_code == 429:
                    # Rate limiter is a 60s sliding window — back off long
                    # enough for the window to clear, then retry.
                    error = "429 rate limited"
                    time.sleep(35)
                    continue
                r.raise_for_status()
                d = r.json()
                cid = d.get("conversation_id") or cid
                reply = d.get("reply") or ""
                error = None
                break
            except Exception as e:  # noqa: BLE001 — record + retry
                error = f"{type(e).__name__}: {e}"
                time.sleep(5)
        transcript.append({"role": "user", "content": turn})
        transcript.append({"role": "assistant", "content": reply if reply is not None else f"[NO REPLY — {error}]"})
        time.sleep(2.5)  # pacing — stay well under the 30/min rate limit
    return {
        "group": group["key"], "scenario": scenario["name"],
        "style": scenario.get("style", ""), "expect": scenario.get("expect", {}),
        "conversation_id": cid, "transcript": transcript, "error": error,
    }


# ───────────────────────── harvest phase ─────────────────────────────

def harvest(client: httpx.Client, base: str, result: dict, quotes_dir: Path) -> None:
    """Attach quote rows + download quote PDFs for one conversation."""
    result["quotes"] = []
    cid = result.get("conversation_id")
    if cid is None:
        return
    r = client.get(f"{base}/admin/api/orgs/{ORG}/conversations/{cid}", headers=auth_headers())
    if r.status_code != 200:
        result["harvest_error"] = f"GET conversation -> {r.status_code}"
        return
    conv = r.json().get("conversation", r.json())
    # Server-side transcript is authoritative (includes synthetic system msgs).
    msgs = conv.get("messages") or []
    if msgs:
        result["transcript"] = msgs
    for q in conv.get("quotes") or []:
        rec = {
            "id": q.get("id"),
            "ref": f"JP-{q.get('id'):04d}" if q.get("id") else "?",
            "product_key": q.get("product_key"),
            "specs": q.get("specs") or {},
            "status": q.get("status"),
            "final_price_ex_vat": q.get("final_price_ex_vat"),
            "final_price_inc_vat": q.get("final_price_inc_vat"),
            "pdf_file": None,
        }
        if q.get("id") is not None:
            pr = client.get(f"{base}/quotes/{q['id']}/pdf")
            if pr.status_code == 200 and pr.content[:4] == b"%PDF":
                fn = quotes_dir / f"JP-{q['id']:04d}.pdf"
                fn.write_bytes(pr.content)
                rec["pdf_file"] = fn.name
        result["quotes"].append(rec)


# ───────────────────────── evaluation ────────────────────────────────

def evaluate(result: dict) -> None:
    """Apply the scenario's `expect` assertions → PASS / CHECK + reasons."""
    expect = result.get("expect") or {}
    replies = " \n ".join(
        m.get("content") or "" for m in result["transcript"] if m.get("role") == "assistant"
    )
    priced = [
        q for q in result.get("quotes", [])
        if q.get("status") == "pending_approval" and q.get("final_price_inc_vat")
    ]
    issues: list[str] = []
    if result.get("error"):
        issues.append(f"network error during run: {result['error']}")
    if "price_contains" in expect and expect["price_contains"] not in replies:
        issues.append(f"expected price '{expect['price_contains']}' not found in replies")
    if "reply_contains" in expect and expect["reply_contains"].lower() not in replies.lower():
        issues.append(f"expected text '{expect['reply_contains']}' not in replies")
    if "reply_not_contains" in expect:
        token = expect["reply_not_contains"]
        if re.search(rf"\b{re.escape(token)}\b", replies, re.IGNORECASE):
            issues.append(f"forbidden term '{token}' appeared in a reply")
    if "marker" in expect and expect["marker"] not in replies:
        issues.append(f"marker {expect['marker']} never emitted")
    if expect.get("quote_created") and not priced:
        issues.append("no priced quote was created")
    if expect.get("escalates") and priced:
        issues.append(f"expected escalation but a priced quote exists ({priced[0]['ref']})")
    result["issues"] = issues
    result["verdict"] = "PASS" if not issues else "CHECK"


# ───────────────────────── cleanup phase ─────────────────────────────

def cleanup(client: httpx.Client, base: str, cids: list[int]) -> tuple[int, list[int]]:
    """Delete exactly the conversations this run created. Returns
    (deleted_count, leftover_ids)."""
    deleted, leftovers = 0, []
    for cid in cids:
        r = client.delete(f"{base}/admin/api/orgs/{ORG}/conversations/{cid}", headers=auth_headers())
        if r.status_code in (200, 204):
            deleted += 1
        else:
            leftovers.append(cid)
            log(f"  !! conv {cid}: DELETE -> {r.status_code}")
    # verify
    for cid in list(cids):
        r = client.get(f"{base}/admin/api/orgs/{ORG}/conversations/{cid}", headers=auth_headers())
        if r.status_code == 200 and cid not in leftovers:
            leftovers.append(cid)
    return deleted, leftovers


# ───────────────────────── master PDF ────────────────────────────────

NAVY = "#0d0d2b"
SLATE = "#475569"
LIGHT = "#f1f5f9"
BORDER = "#cbd5e1"
CUST_BG = "#eef2f7"
CRAIG_BG = "#e8eaf3"

_MARKER_RX = re.compile(r"\[(QUOTE_READY|ARTWORK_CHOICE|ARTWORK_UPLOAD|CUSTOMER_FORM)\]")
_MARKER_LABEL = {
    "QUOTE_READY": "widget shows: PDF quote card",
    "ARTWORK_CHOICE": "widget shows: artwork choice buttons (have / need design / send later)",
    "ARTWORK_UPLOAD": "widget shows: artwork upload box",
    "CUSTOMER_FORM": "widget shows: contact details form",
}


# Emojis (astral plane + misc-symbols blocks) have no glyphs in the PDF's
# Helvetica and render as ■ boxes — strip them from transcript text. Keeps
# €, ×, →, accented Latin.
_EMOJI_RX = re.compile(
    "[\U0001F000-\U0001FAFF\U00010000-\U0001FFFF☀-➿️‍]"
)


def _esc(text: str) -> str:
    clean = _EMOJI_RX.sub("", text or "")
    return clean.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def build_master_pdf(out_path: Path, groups_run: list[dict], results: list[dict],
                     run_meta: dict) -> None:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.lib.colors import HexColor
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.platypus import (
        BaseDocTemplate, PageTemplate, Frame, Paragraph, Spacer, Table,
        TableStyle, PageBreak, NextPageTemplate,
    )
    from reportlab.platypus.tableofcontents import TableOfContents

    styles = getSampleStyleSheet()
    H1 = ParagraphStyle("RH1", parent=styles["Title"], fontName="Helvetica-Bold",
                        fontSize=24, textColor=HexColor(NAVY), spaceAfter=6)
    GROUP_H = ParagraphStyle("GroupHeading", parent=styles["Heading1"],
                             fontName="Helvetica-Bold", fontSize=15,
                             textColor=HexColor(NAVY), spaceBefore=10, spaceAfter=6)
    SCEN_H = ParagraphStyle("ScenHeading", parent=styles["Heading2"],
                            fontName="Helvetica-Bold", fontSize=11,
                            textColor=HexColor(NAVY), spaceBefore=10, spaceAfter=2)
    BODY = ParagraphStyle("RBODY", parent=styles["Normal"], fontSize=9.5, leading=13,
                          textColor=HexColor("#1e293b"), spaceAfter=4)
    SMALL = ParagraphStyle("RSMALL", parent=BODY, fontSize=8.5, leading=11.5,
                           textColor=HexColor(SLATE))
    BUBBLE = ParagraphStyle("BUBBLE", parent=BODY, fontSize=9, leading=12.5, spaceAfter=0)
    CHIP = ParagraphStyle("CHIP", parent=SMALL, fontSize=8, textColor=HexColor("#7c3aed"))

    class ReportDoc(BaseDocTemplate):
        def afterFlowable(self, flowable):
            if isinstance(flowable, Paragraph) and flowable.style.name == "GroupHeading":
                text = flowable.getPlainText()
                key = f"grp-{abs(hash(text)) % 10_000_000}"
                self.canv.bookmarkPage(key)
                self.canv.addOutlineEntry(text, key, level=0, closed=False)
                self.notify("TOCEntry", (0, text, self.page, key))

    doc = ReportDoc(
        str(out_path), pagesize=A4,
        leftMargin=18 * mm, rightMargin=18 * mm, topMargin=16 * mm, bottomMargin=18 * mm,
        title="Craig — Product Test Report", author="Strategos AI",
    )

    def footer(canvas, _doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 7.5)
        canvas.setFillColor(HexColor(SLATE))
        canvas.drawString(18 * mm, 10 * mm, "Just Print · Craig product test report · "
                          + run_meta["date_human"])
        canvas.drawRightString(192 * mm, 10 * mm, f"Page {_doc.page}")
        canvas.restoreState()

    frame = Frame(18 * mm, 18 * mm, 174 * mm, 263 * mm, id="main")
    doc.addPageTemplates([PageTemplate(id="main", frames=[frame], onPage=footer)])

    story: list = []

    # ── Cover ─────────────────────────────────────────────────────
    n_groups = len(groups_run)
    n_scen = len(results)
    n_quotes = sum(len(r.get("quotes", [])) for r in results)
    n_pass = sum(1 for r in results if r.get("verdict") == "PASS")
    n_check = n_scen - n_pass
    story += [
        Spacer(1, 60),
        Paragraph("Craig — Product Test Report", H1),
        Paragraph("Just Print · automated conversation test suite · "
                  + run_meta["date_human"], SMALL),
        Spacer(1, 16),
        Paragraph(
            f"<b>{n_groups}</b> product groups · <b>{n_scen}</b> complete test "
            f"conversations · <b>{n_quotes}</b> quotes generated "
            f"(PDFs in the <b>quotes/</b> folder) · "
            f"<b>{n_pass}</b> passed checks · <b>{n_check}</b> flagged for review", BODY),
        Spacer(1, 10),
        Paragraph(
            "Every conversation below was run end-to-end against the live Craig agent, "
            "exactly as a customer would experience it on just-print.ie — different ways "
            "of asking, different options, including deliberate edge cases. Prices come "
            "from the live price tables. Test conversations were removed from the system "
            "after this report was generated.", BODY),
        Spacer(1, 14),
        Paragraph("How to read this report", SCEN_H),
        Paragraph("Each product section shows the full conversations as chat bubbles — "
                  "<b>Customer</b> on grey, <b>Craig</b> on blue. Tags under each scenario "
                  "name describe the conversation style being tested:", BODY),
    ]
    for k, v in STYLE_LABELS.items():
        story.append(Paragraph(f"• <b>{v}</b> ({k})", SMALL))
    story += [
        Spacer(1, 6),
        Paragraph("✓ <b>PASS</b> — the conversation behaved and priced as expected. "
                  "(!) <b>CHECK</b> — something to look at; the reason is printed with the "
                  "scenario. Where a quote was produced, its reference (e.g. JP-0123) "
                  "matches the PDF file of the same name in the quotes/ folder.", BODY),
        PageBreak(),
    ]

    # ── TOC ───────────────────────────────────────────────────────
    toc = TableOfContents()
    toc.levelStyles = [ParagraphStyle(
        "TOC0", parent=BODY, fontSize=11, leading=18, leftIndent=4,
        textColor=HexColor(NAVY),
    )]
    # "Contents" heading uses its own style name so afterFlowable doesn't
    # notify the TOC about the TOC itself.
    CONTENTS_H = ParagraphStyle("ContentsHead", parent=GROUP_H)
    story += [Paragraph("Contents", CONTENTS_H), toc, PageBreak()]

    # ── Sections ──────────────────────────────────────────────────
    by_group: dict[str, list[dict]] = {}
    for r in results:
        by_group.setdefault(r["group"], []).append(r)

    for g in groups_run:
        sect = by_group.get(g["key"], [])
        if not sect:
            continue
        story.append(Paragraph(_esc(g["title"]), GROUP_H))
        if g.get("products"):
            story.append(Paragraph("Products: " + ", ".join(g["products"]), SMALL))
        story.append(Spacer(1, 4))

        for res in sect:
            verdict = res.get("verdict", "—")
            mark = "✓ PASS" if verdict == "PASS" else "(!) CHECK"
            style_lbl = " · ".join(
                STYLE_LABELS.get(s.strip(), s.strip())
                for s in (res.get("style") or "").split(",") if s.strip()
            )
            story.append(Paragraph(f"{_esc(res['scenario'])} — {mark}", SCEN_H))
            if style_lbl:
                story.append(Paragraph(style_lbl, SMALL))
            for issue in res.get("issues", []):
                story.append(Paragraph(f"(!) {_esc(issue)}", ParagraphStyle(
                    "ISSUE", parent=SMALL, textColor=HexColor("#b91c1c"))))
            story.append(Spacer(1, 3))

            # transcript bubbles
            for m in res["transcript"]:
                role = m.get("role")
                content = (m.get("content") or "").strip()
                if not content or role == "system":
                    continue
                markers = _MARKER_RX.findall(content)
                clean = _MARKER_RX.sub("", content).strip()
                if role == "user":
                    cell = Paragraph(f"<b>Customer</b><br/>{_esc(clean)}", BUBBLE)
                    t = Table([[cell]], colWidths=[150 * mm], hAlign="RIGHT")
                    bg = CUST_BG
                else:
                    cell = Paragraph(f"<b>Craig</b><br/>{_esc(clean)}", BUBBLE)
                    t = Table([[cell]], colWidths=[150 * mm], hAlign="LEFT")
                    bg = CRAIG_BG
                t.setStyle(TableStyle([
                    ("BACKGROUND", (0, 0), (-1, -1), HexColor(bg)),
                    ("BOX", (0, 0), (-1, -1), 0.4, HexColor(BORDER)),
                    ("LEFTPADDING", (0, 0), (-1, -1), 7),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 7),
                    ("TOPPADDING", (0, 0), (-1, -1), 5),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                ]))
                story.append(t)
                story.append(Spacer(1, 3))
                for mk in markers:
                    story.append(Paragraph("→ " + _MARKER_LABEL.get(mk, mk), CHIP))
                    story.append(Spacer(1, 2))

            # quote footer(s)
            for q in res.get("quotes", []):
                inc = q.get("final_price_inc_vat")
                ex = q.get("final_price_ex_vat")
                price = (f"€{ex:.2f} + VAT = €{inc:.2f}"
                         if isinstance(inc, (int, float)) and isinstance(ex, (int, float))
                         else f"status: {q.get('status')}")
                pdf_note = f" → quotes/{q['pdf_file']}" if q.get("pdf_file") else ""
                story.append(Paragraph(
                    f"<b>Quote {q['ref']}</b> · {q.get('product_key')} · {price}{pdf_note}",
                    ParagraphStyle("QF", parent=SMALL, textColor=HexColor("#166534"))))
            story.append(Spacer(1, 8))
        story.append(PageBreak())

    # ── Appendix: all quotes ──────────────────────────────────────
    story.append(Paragraph("Appendix — every quote generated in this run",
                           ParagraphStyle("AppHead", parent=GROUP_H)))
    rows = [["Quote", "Product", "Specs", "ex VAT", "inc VAT"]]
    for r in results:
        for q in r.get("quotes", []):
            specs = q.get("specs") or {}
            spec_txt = ", ".join(f"{k}={v}" for k, v in specs.items()
                                 if k in ("quantity", "size", "finish", "double_sided",
                                          "width_mm", "height_mm", "pages", "format",
                                          "binding", "cover_type"))
            ex = q.get("final_price_ex_vat")
            inc = q.get("final_price_inc_vat")
            rows.append([
                q["ref"], q.get("product_key") or "",
                Paragraph(_esc(spec_txt), SMALL),
                f"€{ex:.2f}" if isinstance(ex, (int, float)) else "—",
                f"€{inc:.2f}" if isinstance(inc, (int, float)) else "—",
            ])
    if len(rows) > 1:
        t = Table(rows, colWidths=[20 * mm, 38 * mm, 76 * mm, 20 * mm, 20 * mm], hAlign="LEFT")
        t.setStyle(TableStyle([
            ("FONTNAME", (0, 0), (-1, -1), "Helvetica"),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("BACKGROUND", (0, 0), (-1, 0), HexColor(NAVY)),
            ("TEXTCOLOR", (0, 0), (-1, 0), HexColor("#ffffff")),
            ("GRID", (0, 0), (-1, -1), 0.4, HexColor(BORDER)),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [None, HexColor(LIGHT)]),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ]))
        story.append(t)
    checks = [(r["scenario"], i) for r in results for i in r.get("issues", [])]
    if checks:
        story.append(Spacer(1, 8))
        story.append(Paragraph("Items flagged (!) CHECK", SCEN_H))
        for name, issue in checks:
            story.append(Paragraph(f"• <b>{_esc(name)}</b>: {_esc(issue)}", SMALL))

    doc.multiBuild(story)


# ───────────────────────── main ──────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--groups", default="",
                    help="comma-separated group keys to run (default: all)")
    ap.add_argument("--out-dir", default="",
                    help="output folder (default ~/JustPrint/craig-test-reports/<date>)")
    ap.add_argument("--base-url", default=DEFAULT_BASE)
    ap.add_argument("--keep-data", action="store_true",
                    help="skip the cleanup phase (conversations stay in prod)")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the scenario plan, no network calls")
    ap.add_argument("--concurrency", type=int, default=2)
    ap.add_argument("--cleanup-manifest", default="",
                    help="ONLY run cleanup for the conversation ids in this manifest file")
    args = ap.parse_args()

    base = args.base_url.rstrip("/")

    if args.cleanup_manifest:
        manifest = json.loads(Path(args.cleanup_manifest).read_text())
        cids = [r["conversation_id"] for r in manifest["results"] if r.get("conversation_id")]
        with httpx.Client(timeout=60) as client:
            deleted, leftovers = cleanup(client, base, cids)
        log(f"cleanup: deleted {deleted}/{len(cids)}; leftovers: {leftovers or 'none'}")
        return

    wanted = {g.strip() for g in args.groups.split(",") if g.strip()}
    groups_run = [g for g in GROUPS if not wanted or g["key"] in wanted]
    if not groups_run:
        sys.exit(f"no groups matched {sorted(wanted)} — available: {[g['key'] for g in GROUPS]}")

    plan = [(g, s) for g in groups_run for s in g["scenarios"]]
    n_turns = sum(len(s["turns"]) for _, s in plan)
    log(f"plan: {len(groups_run)} groups, {len(plan)} scenarios, {n_turns} turns")
    if args.dry_run:
        for g, s in plan:
            log(f"  [{g['key']}] {s['name']} ({s.get('style')}) — {len(s['turns'])} turns")
        return

    today = _dt.date.today()
    out_dir = Path(args.out_dir or
                   Path.home() / "JustPrint" / "craig-test-reports" / today.isoformat())
    quotes_dir = out_dir / "quotes"
    quotes_dir.mkdir(parents=True, exist_ok=True)

    # Phase A — run
    log(f"\n=== RUN ({len(plan)} scenarios, concurrency {args.concurrency}) ===")
    results: list[dict] = []
    with httpx.Client(timeout=90) as client:
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            futs = {pool.submit(drive_scenario, client, base, g, s): (g, s) for g, s in plan}
            for fut in concurrent.futures.as_completed(futs):
                g, s = futs[fut]
                try:
                    res = fut.result()
                except Exception as e:  # noqa: BLE001
                    res = {"group": g["key"], "scenario": s["name"],
                           "style": s.get("style", ""), "expect": s.get("expect", {}),
                           "conversation_id": None, "transcript": [],
                           "error": f"scenario crashed: {e}"}
                results.append(res)
                log(f"  ran [{res['group']}] {res['scenario']} -> conv {res.get('conversation_id')}")

        # keep report order deterministic (suite order, not completion order)
        order = {(g["key"], s["name"]): i for i, (g, s) in enumerate(plan)}
        results.sort(key=lambda r: order.get((r["group"], r["scenario"]), 999))

        # Phase B — harvest
        log(f"\n=== HARVEST (transcripts + quote PDFs) ===")
        for res in results:
            harvest(client, base, res, quotes_dir)
            evaluate(res)
            qrefs = ", ".join(q["ref"] for q in res.get("quotes", [])) or "no quotes"
            log(f"  {res['verdict']:5} [{res['group']}] {res['scenario']} ({qrefs})")

        # manifest BEFORE cleanup so an interrupted run can still clean up
        manifest = {
            "run_at": today.isoformat(), "base_url": base,
            "groups": [g["key"] for g in groups_run],
            "results": results,  # transcripts included — needed to debug CHECK items
        }
        (out_dir / "run-manifest.json").write_text(json.dumps(manifest, indent=2, default=str))

        # Phase C — report. Cleanup runs in `finally` so a report-build
        # crash can never strand test conversations in prod (the manifest
        # on disk is the recovery path either way).
        pdf_path = out_dir / "Craig-Product-Test-Report.pdf"
        try:
            log(f"\n=== REPORT ===")
            run_meta = {"date_human": today.strftime("%-d %B %Y")}
            build_master_pdf(pdf_path, groups_run, results, run_meta)
            log(f"  master PDF: {pdf_path}")
        finally:
            # Phase D — cleanup
            cids = [r["conversation_id"] for r in results if r.get("conversation_id")]
            if args.keep_data:
                log(f"\n=== CLEANUP SKIPPED (--keep-data) — {len(cids)} conversations remain; "
                    f"clean later with --cleanup-manifest {out_dir / 'run-manifest.json'} ===")
            else:
                log(f"\n=== CLEANUP ({len(cids)} conversations) ===")
                deleted, leftovers = cleanup(client, base, cids)
                log(f"  deleted {deleted}/{len(cids)}; leftovers: {leftovers or 'none'}")

    n_pass = sum(1 for r in results if r.get("verdict") == "PASS")
    log(f"\nDONE — {n_pass}/{len(results)} PASS · report: {pdf_path}")


if __name__ == "__main__":
    main()
