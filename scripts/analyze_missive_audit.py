"""
Analyze the JSONL dump from `export_missive_sample.py` and categorize
every inbound email by structural patterns (no LLM calls — that's
the next step). Output: a markdown report summarizing the email mix
Craig will face in production at Just Print.

Categories detected:
  * reply_in_thread     — subject starts with Re:/RE:/Fwd:
  * obvious_junk        — caught by Craig's obvious_junk() prefilter today
  * internal_team       — sender's domain matches the configured allowlist
                          (or appears to be a Just Print team member)
  * wetransfer_filedrop — wetransfer.com / dropbox / google-drive notification
  * automated_no_reply  — sender starts with noreply/donotreply/etc
  * support_ticket      — domains/keywords matching support systems
  * already_labelled    — conversation already carries a workflow label
                          (Quoted / Approved for Print / On Proof / etc)
                          meaning it's mid-production, NOT a fresh inquiry
  * potential_quote     — what's left: probably new customer email
                          (subset of these are real quote requests)

Usage:
    python -m scripts.analyze_missive_audit \\
        --in missive_audit_5mo.jsonl \\
        --out missive_audit_report.md
"""

from __future__ import annotations

import argparse
import json
import sys
import re
from collections import Counter, defaultdict
from datetime import datetime


# Labels that mean "this thread is already in active production —
# Craig has no business jumping in".
ACTIVE_PRODUCTION_LABELS = {
    "Quoted", "On Proof", "Approved for Print", "Job Bag Printed",
    "Outsourced", "Artwork", "Docket", "URGENT", "Accounts", "Blue Q",
}

# Domains where Just Print's team members operate.
# Seeded from production: just-print.ie + (greg.justprintie@gmail.com
# observed for John Granahan during testing).
KNOWN_INTERNAL_DOMAINS = {
    "just-print.ie",
}
# Personal Gmail aliases used by team members (filled in as we find them).
KNOWN_INTERNAL_ADDRESSES = {
    "greg.justprintie@gmail.com",  # John Granahan
}

NO_REPLY_PREFIXES = (
    "noreply", "no-reply", "no_reply",
    "donotreply", "do-not-reply", "do_not_reply",
    "mailer-daemon", "postmaster",
    "bounce", "bounces",
    "notifications", "notify", "alerts",
)

BAD_SUBJECT_KEYWORDS = (
    "out of office", "auto-reply", "auto reply", "automatic reply",
    "delivery failure", "undeliverable", "delivery status notification",
    "mail delivery", "returned mail", "failure notice",
    "unsubscribe",
)

WETRANSFER_DOMAINS = {
    "wetransfer.com",
    "dropbox.com",
    "drive.google.com",
    "smashfileshare.com",
    "sendgb.com",
    "filemail.com",
}

SUPPORT_DOMAINS = {
    "blacknight.com", "blacknight.ie", "blacknight.solutions",
}

# Replied-to-thread patterns.
RE_RE = re.compile(r"^\s*(re|fw|fwd)\s*:", re.IGNORECASE)

# Quote-request signal in subject (heuristic — NOT classifier, just
# pattern matching).
QUOTE_KEYWORDS = re.compile(
    r"\b(quot[ae]|price|cost|enquir|estimate|qte)\b", re.IGNORECASE,
)


def categorize(rec: dict) -> str:
    sender = (rec.get("from_address") or "").lower()
    subject = (rec.get("subject") or "").strip()
    labels = set(rec.get("labels") or [])

    if not sender:
        return "missing_sender"

    domain = sender.split("@", 1)[1] if "@" in sender else ""
    local_part = sender.split("@", 1)[0] if "@" in sender else sender

    # 1. Obvious-junk prefilter (Craig drops these automatically today)
    if any(local_part.startswith(p) for p in NO_REPLY_PREFIXES):
        return "automated_no_reply"
    subject_lc = subject.lower()
    for kw in BAD_SUBJECT_KEYWORDS:
        if kw in subject_lc:
            return "obvious_junk_subject"

    # 2. Internal team
    if domain in KNOWN_INTERNAL_DOMAINS:
        return "internal_team"
    if sender in KNOWN_INTERNAL_ADDRESSES:
        return "internal_team"

    # 3. File-transfer
    if domain in WETRANSFER_DOMAINS:
        return "wetransfer_filedrop"

    # 4. Support / hosting providers
    if domain in SUPPORT_DOMAINS:
        return "support_ticket"

    # 5. Mid-production threads (label = stage in workflow)
    active = labels & ACTIVE_PRODUCTION_LABELS
    is_reply = bool(RE_RE.match(subject))
    if active:
        # Distinguish "already in production" vs "fresh thread that
        # happens to carry one of these labels because of historical
        # mis-tagging". If subject starts with Re:, it's almost
        # certainly a production follow-up.
        if is_reply:
            return "active_production_reply"
        return "active_production_new"

    # 6. Thread reply on a no-label thread (vague — could be a quote
    # continuation or anything)
    if is_reply:
        return "reply_in_thread"

    # 7. Subject obviously asks about a quote
    if QUOTE_KEYWORDS.search(subject):
        return "potential_quote_subject"

    # 8. Otherwise: fresh email, unknown intent
    return "fresh_unknown_intent"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--in", dest="in_path", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    by_cat: Counter = Counter()
    label_combos: Counter = Counter()
    sender_domains_by_cat: dict[str, Counter] = defaultdict(Counter)
    senders_by_cat: dict[str, Counter] = defaultdict(Counter)
    subject_first_words: Counter = Counter()
    total_messages = 0
    earliest: int | None = None
    latest: int | None = None

    with open(args.in_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            total_messages += 1
            cat = categorize(rec)
            by_cat[cat] += 1

            sender = (rec.get("from_address") or "").lower()
            domain = sender.split("@", 1)[1] if "@" in sender else "(no domain)"
            sender_domains_by_cat[cat][domain] += 1
            senders_by_cat[cat][sender] += 1

            subj = (rec.get("subject") or "").strip()
            if subj:
                first = subj.split()[0] if subj.split() else ""
                subject_first_words[first[:20]] += 1

            for lbl in rec.get("labels") or []:
                label_combos[lbl] += 1

            ts = rec.get("received_at")
            if ts:
                if earliest is None or ts < earliest:
                    earliest = ts
                if latest is None or ts > latest:
                    latest = ts

    # ── Write markdown report ────────────────────────────────────
    lines: list[str] = []
    add = lines.append
    add("# Missive 5-Month Audit — Just Print")
    add("")
    if earliest and latest:
        e_iso = datetime.utcfromtimestamp(earliest).strftime("%Y-%m-%d")
        l_iso = datetime.utcfromtimestamp(latest).strftime("%Y-%m-%d")
        add(f"**Date range:** {e_iso} → {l_iso}")
    add(f"**Total inbound messages analysed:** {total_messages}")
    add("")
    add("---")
    add("")
    add("## Category distribution")
    add("")
    add("| Category | Count | % | Craig behaviour today |")
    add("|---|---:|---:|---|")
    craig_action = {
        "automated_no_reply": "Tier 1 drop (`obvious_junk()` no-reply prefix) ✓",
        "obvious_junk_subject": "Tier 1 drop (`obvious_junk()` subject keyword) ✓",
        "internal_team": "Tier 1 drop (v37.7 `internal_team_domains` allowlist) ✓ if domain known",
        "wetransfer_filedrop": "Goes to classifier — body usually short, **likely Tier 2 noise**",
        "support_ticket": "Goes to classifier — usually Tier 1 verdict=False junk",
        "active_production_reply": "**RISK** — classifier may Tier 3 reply OR Tier 2 notify Justin. Should NOT engage; thread is in production with human handlers",
        "active_production_new": "**RISK** — fresh-looking inbound on a labelled thread; ambiguous",
        "reply_in_thread": "Classifier runs with thread-reply hint; depends on content",
        "potential_quote_subject": "Tier 3 if confidence high — **THIS IS CRAIG's TARGET**",
        "fresh_unknown_intent": "Classifier decides — could be quote, could be off-topic",
        "missing_sender": "Skipped (no sender)",
    }
    for cat, n in by_cat.most_common():
        pct = 100.0 * n / max(total_messages, 1)
        add(f"| `{cat}` | {n} | {pct:.1f}% | {craig_action.get(cat, '?')} |")
    add("")

    # Compute key aggregates
    target = by_cat.get("potential_quote_subject", 0) + by_cat.get("fresh_unknown_intent", 0)
    risky = (
        by_cat.get("active_production_reply", 0)
        + by_cat.get("active_production_new", 0)
        + by_cat.get("reply_in_thread", 0)
    )
    safe_drops = (
        by_cat.get("automated_no_reply", 0)
        + by_cat.get("obvious_junk_subject", 0)
        + by_cat.get("internal_team", 0)
        + by_cat.get("wetransfer_filedrop", 0)
        + by_cat.get("support_ticket", 0)
    )
    add("## Key aggregates")
    add("")
    add(
        f"- **Craig's target zone** (potential new quotes + fresh unknown): "
        f"**{target}** msgs ({100.0 * target / max(total_messages, 1):.1f}%)"
    )
    add(
        f"- **Risk zone** (mid-production threads + thread replies on no-label threads): "
        f"**{risky}** msgs ({100.0 * risky / max(total_messages, 1):.1f}%)"
    )
    add(
        f"- **Safe drops** (junk, internal, file-transfers, support): "
        f"**{safe_drops}** msgs ({100.0 * safe_drops / max(total_messages, 1):.1f}%)"
    )
    add("")
    add("---")
    add("")

    add("## Labels seen on conversations (Justin's workflow tags)")
    add("")
    add("| Label | Conversations touched |")
    add("|---|---:|")
    for lbl, n in label_combos.most_common():
        add(f"| `{lbl}` | {n} |")
    add("")
    add("---")
    add("")

    add("## Top 20 sender domains overall")
    add("")
    overall_domains: Counter = Counter()
    for cat, ctr in sender_domains_by_cat.items():
        for d, n in ctr.items():
            overall_domains[d] += n
    add("| Domain | Count |")
    add("|---|---:|")
    for d, n in overall_domains.most_common(20):
        add(f"| {d} | {n} |")
    add("")
    add("---")
    add("")

    # Show senders flagged as internal_team — Justin needs to verify
    if by_cat.get("internal_team"):
        add("## Internal team senders detected (verify allowlist completeness)")
        add("")
        add("Senders matching `KNOWN_INTERNAL_DOMAINS` or `KNOWN_INTERNAL_ADDRESSES`:")
        add("")
        add("| Sender | Count |")
        add("|---|---:|")
        for s, n in senders_by_cat["internal_team"].most_common(30):
            add(f"| {s} | {n} |")
        add("")

    # Show suspected internal-team senders that AREN'T in the
    # allowlist yet: any sender whose name strongly suggests a team
    # member (we can't detect this from email alone; flag manually).
    add("## Top 20 senders in 'fresh_unknown_intent' bucket")
    add("")
    add("If any of these are actually Just Print team members on personal")
    add("Gmail, add them to `internal_team_addresses` Setting.")
    add("")
    add("| Sender | Count |")
    add("|---|---:|")
    for s, n in senders_by_cat.get("fresh_unknown_intent", Counter()).most_common(20):
        add(f"| {s} | {n} |")
    add("")
    add("---")
    add("")

    add("## Top 10 senders in 'potential_quote_subject' bucket (Craig's target customers)")
    add("")
    add("| Sender | Count |")
    add("|---|---:|")
    for s, n in senders_by_cat.get("potential_quote_subject", Counter()).most_common(10):
        add(f"| {s} | {n} |")
    add("")
    add("---")
    add("")

    add("## Subject first-word distribution (top 20)")
    add("")
    add("| First word | Count |")
    add("|---|---:|")
    for w, n in subject_first_words.most_common(20):
        add(f"| {w} | {n} |")
    add("")

    with open(args.out, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"Wrote {args.out} ({len(lines)} lines, {total_messages} messages analysed)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
