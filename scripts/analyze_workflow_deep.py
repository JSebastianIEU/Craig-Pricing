"""
Deep workflow analysis on missive_audit_5mo.jsonl.

What this answers (numerically):
1. Thread-level: how long is the average thread? What % are single-msg threads?
2. Funnel-by-label: how many threads have NO labels vs Quoted-only vs
   Quoted+Approved+Job-Bag-Printed (full lifecycle)?
3. Sender-recurrence: % of senders that appear ONCE in 13mo vs N times
   (returning customers / B2B partners).
4. Subject pattern by category: what do "potential quote" subjects
   actually look like (sample 50)?
5. Time-of-day / day-of-week pattern (does Craig need to handle off-hours?)
6. Internal team observed: who at Just Print sends from what address
   (mining the messages to find missing internal_team_addresses).
7. First-touch vs follow-up: for each thread, count the FIRST inbound
   only. Those are the "moment of truth" emails Craig must handle well.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone


# Same constants as analyze_missive_audit.py — kept local to avoid
# package wiring just for two scripts.
ACTIVE_PRODUCTION_LABELS = {
    "Quoted", "On Proof", "Approved for Print", "Job Bag Printed",
    "Outsourced", "Artwork", "Docket", "URGENT", "Accounts", "Blue Q",
}
NO_REPLY_PREFIXES = (
    "noreply", "no-reply", "no_reply",
    "donotreply", "do-not-reply", "do_not_reply",
    "mailer-daemon", "postmaster",
    "bounce", "bounces",
    "notifications", "notify", "alerts",
)
RE_RE = re.compile(r"^\s*(re|fw|fwd)\s*:", re.IGNORECASE)
QUOTE_KEYWORDS = re.compile(
    r"\b(quot[ae]|price|cost|enquir|estimate|qte)\b", re.IGNORECASE,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--in", dest="in_path", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    # Per-thread aggregation
    threads: dict[str, list[dict]] = defaultdict(list)
    with open(args.in_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            threads[rec.get("thread_id", "")].append(rec)

    n_threads = len(threads)
    n_msgs = sum(len(m) for m in threads.values())

    # Thread length distribution
    thread_lengths = Counter(len(m) for m in threads.values())

    # Label state per thread
    label_states: Counter = Counter()
    for tid, msgs in threads.items():
        # Use any message's labels (they're conversation-level)
        labels = set()
        for m in msgs:
            labels.update(m.get("labels") or [])
        active = labels & ACTIVE_PRODUCTION_LABELS
        if not active:
            label_states["no_workflow_label"] += 1
        elif active == {"Quoted"}:
            label_states["quote_only"] += 1
        elif "Quoted" in active and "Approved for Print" in active and "Job Bag Printed" in active:
            label_states["full_lifecycle"] += 1
        elif "Quoted" in active and "Approved for Print" in active:
            label_states["quote_to_approved"] += 1
        elif "Job Bag Printed" in active:
            label_states["printed_only"] += 1
        elif "Approved for Print" in active:
            label_states["approved_only"] += 1
        else:
            label_states["other_combo"] += 1

    # First-touch analysis: per thread, the FIRST inbound (chronologically)
    # — that's the moment Craig is most likely to need to engage.
    first_touches: list[dict] = []
    for tid, msgs in threads.items():
        sortable = sorted(msgs, key=lambda r: r.get("received_at") or 0)
        if sortable:
            first_touches.append(sortable[0])

    # Categorize first-touches: were they on threads that ended in
    # active production OR were they fresh quote requests Craig could
    # have handled?
    first_touch_outcomes = Counter()
    quote_targets = []  # samples of fresh quote requests
    for rec in first_touches:
        sender = (rec.get("from_address") or "").lower()
        domain = sender.split("@", 1)[1] if "@" in sender else ""
        local = sender.split("@", 1)[0] if "@" in sender else sender
        subj = (rec.get("subject") or "").strip()
        is_no_reply = any(local.startswith(p) for p in NO_REPLY_PREFIXES)
        labels = set(rec.get("labels") or [])
        is_reply = bool(RE_RE.match(subj))

        if is_no_reply:
            first_touch_outcomes["first_was_automated"] += 1
            continue
        if domain == "just-print.ie":
            first_touch_outcomes["first_was_internal"] += 1
            continue
        # Does this thread have ANY workflow label EVER?
        had_workflow = bool(labels & ACTIVE_PRODUCTION_LABELS)
        if had_workflow:
            if "Quoted" in labels:
                first_touch_outcomes["first_led_to_quote"] += 1
                if not is_reply and QUOTE_KEYWORDS.search(subj):
                    quote_targets.append(rec)
            else:
                first_touch_outcomes["first_led_to_production_but_no_quote_label"] += 1
        else:
            if is_reply:
                first_touch_outcomes["first_was_already_reply_no_label"] += 1
            else:
                first_touch_outcomes["first_fresh_no_outcome"] += 1
                if QUOTE_KEYWORDS.search(subj):
                    quote_targets.append(rec)

    # Recurring vs one-shot senders
    sender_counts: Counter = Counter()
    for m in [msg for ms in threads.values() for msg in ms]:
        s = (m.get("from_address") or "").lower()
        if s:
            sender_counts[s] += 1
    one_shot = sum(1 for c in sender_counts.values() if c == 1)
    recurring = sum(1 for c in sender_counts.values() if c > 1)
    heavy = sum(1 for c in sender_counts.values() if c >= 10)

    # Day-of-week + hour-of-day distribution
    dow_count: Counter = Counter()
    hour_count: Counter = Counter()
    DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    for m in [msg for ms in threads.values() for msg in ms]:
        ts = m.get("received_at")
        if not ts:
            continue
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        dow_count[DAYS[dt.weekday()]] += 1
        hour_count[dt.hour] += 1

    # Build markdown report
    lines = []
    add = lines.append
    add("# Just Print Missive — Deep Workflow Analysis")
    add("")
    add(f"- **Threads:** {n_threads}")
    add(f"- **Inbound messages:** {n_msgs}")
    add(f"- **Avg messages per thread:** {n_msgs / max(n_threads, 1):.2f}")
    add("")

    add("## 1. Thread length distribution")
    add("")
    add("| Messages in thread | Threads |")
    add("|---:|---:|")
    for length, count in sorted(thread_lengths.items())[:15]:
        add(f"| {length} | {count} |")
    extra = sum(c for l, c in thread_lengths.items() if l > 15)
    if extra:
        add(f"| 16+ | {extra} |")
    add("")
    single = thread_lengths.get(1, 0)
    add(
        f"**{single} threads ({100.0 * single / max(n_threads, 1):.1f}%) "
        f"received exactly ONE inbound message** — the cleanest 'first-touch / "
        f"never replied to or replied via outbound only' case."
    )
    add("")

    add("## 2. Thread lifecycle (by workflow labels)")
    add("")
    add("| Stage | Threads | % of total |")
    add("|---|---:|---:|")
    for stage, n in label_states.most_common():
        pct = 100.0 * n / max(n_threads, 1)
        add(f"| `{stage}` | {n} | {pct:.1f}% |")
    add("")

    add("## 3. First-touch outcomes")
    add("")
    add("What happened to threads, classified by their FIRST inbound message?")
    add("")
    add("| First-touch outcome | Threads | % |")
    add("|---|---:|---:|")
    total_first = sum(first_touch_outcomes.values())
    for outcome, n in first_touch_outcomes.most_common():
        pct = 100.0 * n / max(total_first, 1)
        add(f"| `{outcome}` | {n} | {pct:.1f}% |")
    add("")

    add("## 4. Sender recurrence")
    add("")
    add(f"- **Unique senders:** {len(sender_counts)}")
    add(f"- **One-shot senders** (1 email in 13mo): {one_shot} ({100.0 * one_shot / max(len(sender_counts), 1):.1f}%)")
    add(f"- **Recurring senders** (2+ emails): {recurring}")
    add(f"- **Heavy recurring** (10+ emails): {heavy}")
    add("")
    add("Top 20 most-active senders:")
    add("")
    add("| Sender | Emails |")
    add("|---|---:|")
    for s, n in sender_counts.most_common(20):
        add(f"| {s} | {n} |")
    add("")

    add("## 5. Sample 30 'fresh quote target' subjects")
    add("")
    add("Subjects on threads where the first inbound looks like a fresh quote request:")
    add("")
    add("```")
    for r in quote_targets[:30]:
        subj = (r.get("subject") or "(no subject)")[:100]
        sender = r.get("from_address", "")
        add(f"  [{sender:<45}]  {subj}")
    add("```")
    add("")

    add("## 6. When are emails received?")
    add("")
    add("### Day of week")
    add("")
    add("| Day | Count |")
    add("|---|---:|")
    for d in DAYS:
        add(f"| {d} | {dow_count.get(d, 0)} |")
    add("")
    add("### Hour of day (UTC)")
    add("")
    add("| Hour | Count |")
    add("|---|---:|")
    for h in range(24):
        add(f"| {h:02d}:00 | {hour_count.get(h, 0)} |")
    add("")

    add("## 7. Internal team senders (mining for missing allowlist entries)")
    add("")
    add("Senders matching common Just Print internal patterns:")
    add("")
    # Sniff: any sender containing 'justprint' / 'just-print' / 'just.print'
    # or known team-member surnames.
    INTERNAL_PATTERNS = (
        "justprint", "just-print", "just.print",
        "granahan", "byrne", "heneghan", "rajan", "farrell", "gallagher",
    )
    internal_candidates = []
    for s, n in sender_counts.items():
        for pat in INTERNAL_PATTERNS:
            if pat in s.lower():
                internal_candidates.append((s, n))
                break
    internal_candidates.sort(key=lambda x: -x[1])
    add("| Sender | Emails | Likely team member |")
    add("|---|---:|---|")
    for s, n in internal_candidates[:30]:
        hint = ""
        sl = s.lower()
        if "granahan" in sl:
            hint = "John Granahan"
        elif "byrne" in sl:
            hint = "Justin Byrne or Ian Byrne"
        elif "heneghan" in sl:
            hint = "Eva Heneghan"
        elif "rajan" in sl:
            hint = "Alfred Rajan"
        elif "farrell" in sl:
            hint = "Joe Farrell"
        elif "gallagher" in sl:
            hint = "Niall Gallagher"
        elif "justprint" in sl or "just-print" in sl or "just.print" in sl:
            hint = "Just Print system / alias"
        add(f"| {s} | {n} | {hint} |")
    add("")

    with open(args.out, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"Wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
