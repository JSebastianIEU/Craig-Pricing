"""
Unit tests for the in-memory rate limiter.

We patch `rate_limiter._now` to control time deterministically — sleeping
60s in a test would be silly and flaky.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

import pytest
from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient

import rate_limiter
from rate_limiter import rate_limit


@pytest.fixture(autouse=True)
def _reset():
    rate_limiter._reset_for_tests()
    yield
    rate_limiter._reset_for_tests()


def _build_app(route: str, limit: int) -> FastAPI:
    app = FastAPI()

    @app.post("/probe", dependencies=[Depends(rate_limit(route, limit))])
    def probe():
        return {"ok": True}

    return app


def test_under_limit_passes():
    app = _build_app("test", limit=3)
    c = TestClient(app)
    for _ in range(3):
        assert c.post("/probe").status_code == 200


def test_over_limit_returns_429_with_retry_after():
    app = _build_app("test", limit=2)
    c = TestClient(app)
    assert c.post("/probe").status_code == 200
    assert c.post("/probe").status_code == 200
    r = c.post("/probe")
    assert r.status_code == 429
    assert r.json()["detail"] == "rate_limit_exceeded"
    assert "retry-after" in {h.lower() for h in r.headers.keys()}


def test_window_expiry_lets_traffic_through_again(monkeypatch):
    """After WINDOW_SECONDS pass, the bucket should drain and allow new requests."""
    fake_clock = {"t": 1000.0}
    monkeypatch.setattr(rate_limiter, "_now", lambda: fake_clock["t"])
    app = _build_app("test", limit=2)
    c = TestClient(app)
    assert c.post("/probe").status_code == 200
    assert c.post("/probe").status_code == 200
    assert c.post("/probe").status_code == 429
    # Skip past the window
    fake_clock["t"] += rate_limiter.WINDOW_SECONDS + 1
    assert c.post("/probe").status_code == 200


def test_different_route_keys_have_independent_buckets():
    """A request to /chat shouldn't deplete the budget for /webhook."""
    app = FastAPI()

    @app.post("/a", dependencies=[Depends(rate_limit("a", 1))])
    def a():
        return {"ok": "a"}

    @app.post("/b", dependencies=[Depends(rate_limit("b", 1))])
    def b():
        return {"ok": "b"}

    c = TestClient(app)
    assert c.post("/a").status_code == 200
    assert c.post("/a").status_code == 429
    # /b's bucket is independent
    assert c.post("/b").status_code == 200


def test_x_forwarded_for_used_when_present():
    """Behind Cloud Run / a proxy we trust X-F-F's leftmost IP."""
    app = _build_app("test", limit=1)
    c = TestClient(app)
    # First IP fills its bucket
    assert c.post("/probe", headers={"X-Forwarded-For": "1.2.3.4"}).status_code == 200
    assert c.post("/probe", headers={"X-Forwarded-For": "1.2.3.4"}).status_code == 429
    # Different IP, fresh budget
    assert c.post("/probe", headers={"X-Forwarded-For": "9.9.9.9"}).status_code == 200


def test_x_forwarded_for_takes_leftmost_only():
    """Header looks like 'client, proxy1, proxy2' — the client is leftmost."""
    app = _build_app("test", limit=1)
    c = TestClient(app)
    assert c.post(
        "/probe",
        headers={"X-Forwarded-For": "5.5.5.5, 10.0.0.1, 10.0.0.2"},
    ).status_code == 200
    assert c.post(
        "/probe",
        headers={"X-Forwarded-For": "5.5.5.5, 10.0.0.1, 10.0.0.2"},
    ).status_code == 429
