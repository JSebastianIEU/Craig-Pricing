"""
Unit tests for `db.parse_artwork_files` — the defensive helper that
normalizes whatever the DB driver returns for `Quote.artwork_files`
into a clean Python list.

This guards against the regression where v25 created the column as
TEXT (instead of JSONB) on Postgres, which made writes serialize to
a JSON string and reads return a Python str. Code that did
`enumerate(quote.artwork_files)` then iterated the JSON STRING
character-by-character — one upload showed up as ~179 phantom files
in the dashboard, the cap fired after one file, and the proxy 500'd
because no character looked like a `gs://` URL.
"""

from __future__ import annotations

import os

os.environ.setdefault("STRATEGOS_JWT_SECRET", "test-secret-32b-pad-enough-now")

from db import parse_artwork_files  # noqa: E402


def test_none_returns_empty_list():
    assert parse_artwork_files(None) == []


def test_empty_string_returns_empty_list():
    assert parse_artwork_files("") == []


def test_real_list_passes_through():
    items = [
        {"url": "gs://bucket/a", "filename": "a.pdf", "size": 100},
        {"url": "gs://bucket/b", "filename": "b.png", "size": 200},
    ]
    assert parse_artwork_files(items) == items


def test_empty_list_returns_empty():
    assert parse_artwork_files([]) == []


def test_json_string_parses_to_list():
    """The exact regression: TEXT column returns a JSON string."""
    json_str = '[{"url":"gs://bucket/a","filename":"a.pdf","size":100}]'
    parsed = parse_artwork_files(json_str)
    assert isinstance(parsed, list)
    assert len(parsed) == 1
    assert parsed[0]["url"] == "gs://bucket/a"


def test_json_string_with_multiple_items():
    json_str = (
        '[{"url":"gs://bucket/a","filename":"a.pdf","size":100},'
        '{"url":"gs://bucket/b","filename":"b.png","size":200}]'
    )
    parsed = parse_artwork_files(json_str)
    assert len(parsed) == 2
    assert parsed[1]["filename"] == "b.png"


def test_malformed_json_returns_empty():
    """Garbage in -> empty list out, never raises."""
    assert parse_artwork_files("not json at all {[}]") == []


def test_json_object_not_list_returns_empty():
    """We expect a list. An object is not a list — return empty."""
    assert parse_artwork_files('{"foo": "bar"}') == []


def test_json_string_returns_empty():
    # A bare JSON string like '"hello"' parses to "hello" (str), not a list
    assert parse_artwork_files('"hello"') == []


def test_json_number_returns_empty():
    assert parse_artwork_files("42") == []


def test_json_null_returns_empty():
    assert parse_artwork_files("null") == []


def test_other_type_returns_empty():
    """Defensive: dict, int, etc. -> empty list."""
    assert parse_artwork_files({"foo": "bar"}) == []
    assert parse_artwork_files(42) == []
    assert parse_artwork_files(True) == []


# ---------------------------------------------------------------------------
# The exact production data shape from the conv 99 / quote 62 bug
# ---------------------------------------------------------------------------


def test_production_regression_shape():
    """
    On the day of the bug, quote 62's artwork_files came back from the
    JSONlist endpoint as a 179-character JSON string instead of a
    1-element list. enumerate() then iterated it as 179 chars. The
    parser MUST catch this and return the real list.
    """
    real_list = [{
        "url": "gs://craig-pricing-artwork/artwork/99-ae103f536ff8.png",
        "filename": "qr-code (1).png",
        "size": 2748,
        "content_type": "image/png",
        "uploaded_at": "2026-05-01T09:29:04",
    }]
    import json as _json
    json_str = _json.dumps(real_list)
    # Simulate what the buggy DB returned
    assert isinstance(json_str, str)
    assert len(json_str) > 100  # multi-char string

    parsed = parse_artwork_files(json_str)
    assert isinstance(parsed, list)
    assert len(parsed) == 1  # NOT 179
    assert parsed[0]["filename"] == "qr-code (1).png"
