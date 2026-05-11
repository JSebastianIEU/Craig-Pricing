"""
Pre-cutover Missive audit tool.

Pulls a sample of inbound emails from Justin's Missive workspace so we
can categorize what Craig will actually see in production — quote
requests, supplier replies, WeTransfer notifications, internal team
chatter, support tickets, marketing — and tune the classifier + filters
before flipping Craig ON.

Usage:
    # token must be read-scoped — never paste a write token
    export MISSIVE_TOKEN=missive_pat-...
    python -m scripts.export_missive_sample \\
        --account info@just-print.ie \\
        --days 90 \\
        --out missive_audit.jsonl

Output: JSONL — one record per INBOUND message (outbound is skipped
since Craig never sees those). Body capped at 800 chars to keep the
file small. Token comes from $MISSIVE_TOKEN to avoid leaking it via
process list / shell history.

Rate-limited to 5 req/s to stay polite to Missive's API. Estimate:
for ~1000 conversations × ~5 messages each = ~6000 messages = ~25min
wall clock at 4 req/s. Resumable via `--since YYYY-MM-DD` if you need
to chunk it.

The output is the GO/NO-GO checkpoint for the cutover. Once you have
the JSONL, share it with Claude and we'll produce the categorization
+ tuning report.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator

try:
    import httpx
except ImportError:
    print("error: httpx is required (pip install httpx)", file=sys.stderr)
    sys.exit(1)


MISSIVE_BASE = "https://public.missiveapp.com/v1"
DEFAULT_TIMEOUT = 30.0  # seconds
RATE_LIMIT_DELAY = 0.25  # 4 req/s steady, polite


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _get_with_retry(client: httpx.Client, url: str, params: dict | None = None,
                    max_retries: int = 3) -> dict | None:
    """GET with exponential backoff on 429 / 5xx. Returns None on
    permanent failure so the caller can decide to skip vs abort."""
    delay = 1.0
    for attempt in range(max_retries):
        try:
            r = client.get(url, params=params)
        except httpx.RequestError as e:
            print(f"  network error ({e!r}) — retry in {delay:.0f}s", file=sys.stderr)
            time.sleep(delay)
            delay *= 2
            continue
        if r.status_code == 200:
            return r.json()
        if r.status_code == 429:
            retry_after = float(r.headers.get("Retry-After", delay))
            print(f"  429 rate-limited — sleeping {retry_after:.1f}s", file=sys.stderr)
            time.sleep(retry_after)
            delay *= 2
            continue
        if r.status_code >= 500:
            print(
                f"  {r.status_code} server error — retry in {delay:.0f}s",
                file=sys.stderr,
            )
            time.sleep(delay)
            delay *= 2
            continue
        # 4xx other than 429 — permanent
        print(
            f"  error {r.status_code}: {r.text[:200]}", file=sys.stderr,
        )
        return None
    print(f"  giving up on {url} after {max_retries} retries", file=sys.stderr)
    return None


def _iter_conversations(
    client: httpx.Client, account: str, since_iso: str,
) -> Iterator[dict]:
    """Paginate `/conversations` filtered by account + since. Missive's
    pagination uses cursor-based `until` query param; if absent, returns
    the most recent page."""
    until: float | None = None
    page = 0
    while True:
        params: dict[str, Any] = {
            "account": account,
            "since": since_iso,
            "limit": 50,
        }
        if until is not None:
            params["until"] = int(until)
        page += 1
        print(f"page {page}  fetching conversations...", file=sys.stderr)
        body = _get_with_retry(client, f"{MISSIVE_BASE}/conversations", params)
        time.sleep(RATE_LIMIT_DELAY)
        if not body:
            break
        convs = body.get("conversations") or []
        if not convs:
            break
        for c in convs:
            yield c
        # Cursor: the oldest message's last_activity_at (Unix epoch)
        oldest = convs[-1]
        next_until = oldest.get("last_activity_at")
        if not next_until or next_until == until:
            # No progress — bail out
            break
        until = next_until


def _list_messages(
    client: httpx.Client, conversation_id: str, limit: int = 20,
) -> list[dict]:
    """Fetch the latest N messages in a conversation."""
    body = _get_with_retry(
        client,
        f"{MISSIVE_BASE}/conversations/{conversation_id}/messages",
        params={"limit": limit},
    )
    time.sleep(RATE_LIMIT_DELAY)
    if not body:
        return []
    return body.get("messages") or []


def _extract_inbound(msg: dict, account: str) -> dict | None:
    """Build a sanitized record if `msg` is an inbound email. Returns
    None for outbound or non-email messages."""
    if (msg.get("type") or "").lower() != "email":
        return None
    from_field = msg.get("from_field") or {}
    sender = (from_field.get("address") or "").strip().lower()
    if not sender or sender == account.lower():
        # Outbound (from the watched account itself)
        return None

    # Sanity: strip body to 800 chars + drop HTML if possible. Missive
    # gives us `body` and `preview` — prefer plain `preview` if present.
    body = msg.get("body") or msg.get("preview") or ""
    if "<" in body[:1000] and ">" in body[:1000]:
        # naive HTML strip
        import re as _re
        body = _re.sub(r"<br\s*/?>", "\n", body, flags=_re.IGNORECASE)
        body = _re.sub(r"<[^>]+>", "", body)
    body_preview = body.strip()[:800]

    # Labels live on the conversation, not the message — caller injects.
    return {
        "message_id": msg.get("id"),
        "received_at": msg.get("delivered_at") or msg.get("created_at"),
        "from_address": sender,
        "from_name": (from_field.get("name") or "").strip(),
        "subject": (msg.get("subject") or "").strip(),
        "body_preview": body_preview,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Export inbound Missive emails for Craig audit.",
    )
    parser.add_argument("--account", required=True, help="Watched inbox, e.g. info@just-print.ie")
    parser.add_argument("--days", type=int, default=90, help="Lookback window (default 90)")
    parser.add_argument("--out", required=True, help="Output path (.jsonl)")
    parser.add_argument(
        "--max-conversations", type=int, default=2000,
        help="Safety cap on conversations scanned (default 2000)",
    )
    parser.add_argument(
        "--max-messages-per-conv", type=int, default=20,
        help="Max messages pulled per conversation (default 20)",
    )
    args = parser.parse_args()

    token = os.environ.get("MISSIVE_TOKEN", "").strip()
    if not token:
        print(
            "error: MISSIVE_TOKEN env var is required.\n"
            "  Set it before running:  export MISSIVE_TOKEN=missive_pat-...",
            file=sys.stderr,
        )
        return 2

    cutoff_dt = datetime.now(timezone.utc) - timedelta(days=args.days)
    cutoff_iso = cutoff_dt.isoformat(timespec="seconds").replace("+00:00", "Z")
    print(
        f"Fetching inbound emails for {args.account} since {cutoff_iso} "
        f"(last {args.days} days)...",
        file=sys.stderr,
    )

    n_convs = 0
    n_inbound = 0
    n_outbound = 0
    n_nonmail = 0
    with httpx.Client(headers=_headers(token), timeout=DEFAULT_TIMEOUT) as client, \
         open(args.out, "w", encoding="utf-8") as out:
        for conv in _iter_conversations(client, args.account, cutoff_iso):
            n_convs += 1
            if n_convs > args.max_conversations:
                print(
                    f"reached --max-conversations cap ({args.max_conversations}), stopping",
                    file=sys.stderr,
                )
                break
            conv_id = conv.get("id")
            if not conv_id:
                continue
            labels = [
                (lbl.get("name") or "")
                for lbl in (conv.get("shared_labels") or conv.get("labels") or [])
                if isinstance(lbl, dict)
            ]
            messages = _list_messages(
                client, conv_id, limit=args.max_messages_per_conv,
            )
            for msg in messages:
                rec = _extract_inbound(msg, args.account)
                if rec is None:
                    msg_type = (msg.get("type") or "").lower()
                    if msg_type == "email":
                        n_outbound += 1
                    else:
                        n_nonmail += 1
                    continue
                rec["thread_id"] = conv_id
                rec["labels"] = labels
                out.write(json.dumps(rec, ensure_ascii=False) + "\n")
                n_inbound += 1
            if n_convs % 50 == 0:
                print(
                    f"  progress: {n_convs} conversations / "
                    f"{n_inbound} inbound emails so far",
                    file=sys.stderr,
                )

    print(file=sys.stderr)
    print(
        f"Done. {n_convs} conversations scanned, "
        f"{n_inbound} inbound emails exported, "
        f"{n_outbound} outbound skipped, "
        f"{n_nonmail} non-email events skipped.",
        file=sys.stderr,
    )
    print(f"Output: {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
