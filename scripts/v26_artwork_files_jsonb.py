"""
V26 migration — convert quotes.artwork_files from TEXT to proper JSON
storage on Postgres.

Backstory: v25 added the column with `TEXT NULL` because we were
shipping fast and the schema was designed before we noticed that
SQLAlchemy's generic `JSON` type expects a JSONB-backed column on
Postgres to round-trip lists/dicts cleanly. With TEXT under the hood,
writes silently serialize to a JSON string, and reads come back as a
Python str instead of a deserialized list. Code that did
`enumerate(quote.artwork_files)` started iterating the JSON string
character-by-character — a single uploaded file showed up as ~179
phantom "artwork" entries in the dashboard, the upload cap fired
after the first file (`len("[{...}]") > 10`), and the proxy 500'd
because no character looked like a `gs://` URL.

What this migration does:

  1. Postgres only — alter the column type to JSONB. Each existing
     TEXT value is parsed via `::jsonb` so we don't lose data.
  2. Sanity-pass: any rows whose JSONB value is NOT an array (or a
     valid object representing one) are reset to NULL so reads can
     fall back to the singular columns.
  3. Mirror the FIRST entry of each non-empty array into the singular
     columns (`artwork_file_url/name/size`) — this was already done
     by the upload endpoint going forward, but legacy rows persisted
     before the fix may be inconsistent.

  SQLite (local dev) is a no-op — it stores JSON-as-TEXT natively
  and SQLAlchemy round-trips it correctly via the `JSON` type adapter.

Idempotent. Safe to re-run.

Usage:
    python -m scripts.v26_artwork_files_jsonb
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import inspect, text

from db import db_session, engine, parse_artwork_files
from db.models import Quote


def _is_postgres() -> bool:
    return engine.url.drivername.startswith("postgresql")


def _column_type(conn, table: str, column: str) -> str | None:
    """Return the underlying column type as a string ('text', 'jsonb',
    etc.). Postgres only — returns None on SQLite."""
    if not _is_postgres():
        return None
    row = conn.execute(
        text(
            "SELECT data_type FROM information_schema.columns "
            "WHERE table_name = :t AND column_name = :c"
        ),
        {"t": table, "c": column},
    ).fetchone()
    return row[0] if row else None


def migrate() -> None:
    print("V26: artwork_files column type fix (TEXT -> JSONB)...")

    if not _is_postgres():
        print("  · SQLite — no schema change needed; defensive parser handles all reads")
        # Still run the data-repair pass for sanity
        _repair_data_pass()
        return

    # ── 1. Column type conversion ──────────────────────────────────────
    with engine.begin() as conn:
        current = _column_type(conn, "quotes", "artwork_files")
        print(f"  · current column type: {current!r}")

        if current == "jsonb":
            print("  · already jsonb — skipping ALTER")
        else:
            # The existing TEXT values ARE JSON strings (SQLAlchemy
            # serialized them on write), so ::jsonb works for the
            # well-formed ones. Anything malformed becomes NULL so
            # the cast doesn't fail mid-row.
            conn.execute(text("""
                UPDATE quotes
                SET artwork_files = NULL
                WHERE artwork_files IS NOT NULL
                  AND artwork_files !~ '^\\s*(\\[|null)';
            """))
            conn.execute(text("""
                ALTER TABLE quotes
                ALTER COLUMN artwork_files TYPE JSONB
                USING (
                    CASE
                        WHEN artwork_files IS NULL THEN NULL
                        ELSE artwork_files::jsonb
                    END
                );
            """))
            print("  + altered quotes.artwork_files -> JSONB")

    # ── 2. Data-repair pass ───────────────────────────────────────────
    _repair_data_pass()


def _repair_data_pass() -> None:
    """For every quote with a non-empty artwork_files array, mirror the
    first entry into the singular columns. Also clears array entries
    that aren't dicts (defensive) so downstream code never trips on a
    char/str.

    v33 fix — uses RAW SQL with explicit columns instead of ORM. The
    ORM model would otherwise SELECT every column on Quote, including
    columns added by later migrations (e.g. approved_at from v33),
    which fail with 'column does not exist' on a fresh deploy where
    the later migration hasn't run yet."""
    import json as _json
    repaired = 0
    cleared = 0
    with engine.begin() as conn:
        rows = conn.execute(text(
            "SELECT id, artwork_files FROM quotes WHERE artwork_files IS NOT NULL"
        )).fetchall()
        for row in rows:
            qid, raw = row
            # JSONB columns return dicts/lists already; TEXT returns str.
            if isinstance(raw, str):
                files = parse_artwork_files(raw)
            elif isinstance(raw, list):
                files = raw
            else:
                files = parse_artwork_files(raw)
            cleaned = [e for e in files if isinstance(e, dict) and (e.get("url") or "").strip()]
            if not cleaned:
                conn.execute(
                    text(
                        "UPDATE quotes SET artwork_files = NULL, "
                        "artwork_file_url = NULL, "
                        "artwork_file_name = NULL, "
                        "artwork_file_size = NULL "
                        "WHERE id = :id"
                    ),
                    {"id": qid},
                )
                cleared += 1
                continue
            if cleaned != files:
                conn.execute(
                    text("UPDATE quotes SET artwork_files = :v WHERE id = :id"),
                    {"v": _json.dumps(cleaned, ensure_ascii=False), "id": qid},
                )
                repaired += 1
            # Mirror first entry into singular cols
            first = cleaned[0]
            conn.execute(
                text(
                    "UPDATE quotes SET "
                    "artwork_file_url = :u, "
                    "artwork_file_name = :n, "
                    "artwork_file_size = :s "
                    "WHERE id = :id"
                ),
                {
                    "u": first.get("url"),
                    "n": first.get("filename") or "artwork",
                    "s": int(first.get("size") or 0),
                    "id": qid,
                },
            )
        if repaired:
            print(f"  + repaired {repaired} artwork_files row(s) (dropped non-dict entries)")
        if cleared:
            print(f"  + cleared {cleared} fully-junk artwork_files row(s)")
        if not (repaired or cleared):
            print("  · no repair needed")


if __name__ == "__main__":
    migrate()
