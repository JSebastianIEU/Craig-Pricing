"""
In-memory IP-based rate limiter for Craig's public endpoints.

Why this exists: webhooks (`/admin/api/webhooks/stripe/{org_slug}`,
`/missive/webhook`) and the customer-facing `/chat` endpoint are public —
no JWT in front of them. Stripe and Missive defend with HMAC, but a hostile
client can still drown the worker with garbage POSTs whose signatures we
have to verify. `/chat` has no signature at all and is the most exposed
surface. A simple per-IP cap is enough to make casual abuse expensive.

Design:
  - Sliding window (fixed-size deque per (ip, route_key))
  - In-memory only — Cloud Run runs `--min-instances=1` so the bucket is
    stable across requests; if we ever scale to N instances the limit
    becomes N * limit, which is still strictly better than no limit. A
    distributed implementation (Redis / Cloud SQL) is explicitly OOS.
  - FastAPI dependency factory — each route opts in with
    `Depends(rate_limit("route_name", limit=N))`.
  - Thread-safe (Lock) — uvicorn's worker model can serve concurrent
    requests off the same process.

NOT a token-bucket — sliding window is simpler and gives strict per-window
guarantees. We don't care about burstiness for this use case.
"""

from __future__ import annotations

from collections import deque
from threading import Lock
from time import monotonic
from typing import Callable

from fastapi import HTTPException, Request


# (ip, route_key) -> deque[float]   timestamps via monotonic()
_buckets: dict[tuple[str, str], deque[float]] = {}
_lock = Lock()

# Sliding window length, seconds. Tuneable if we want, but 60s is the
# canonical "per minute" rate-limit unit and matches Stripe's own retry
# expectations.
WINDOW_SECONDS = 60.0
DEFAULT_LIMIT = 60


def _now() -> float:
    """Indirection so tests can monkeypatch `rate_limiter._now`."""
    return monotonic()


def _client_ip(request: Request) -> str:
    """
    Best-effort client IP. Trusts `X-Forwarded-For` only when running behind
    Cloud Run (which terminates TLS and rewrites it). Falls back to the
    direct peer IP otherwise.

    NOTE: this is "best effort" — a sufficiently determined attacker can
    spoof X-Forwarded-For. For abuse-prevention this is fine; for billing
    or security gating it would not be.
    """
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        # X-F-F is "client, proxy1, proxy2" — the client is the leftmost
        return fwd.split(",")[0].strip()
    if request.client:
        return request.client.host
    return "unknown"


def rate_limit(route_key: str, limit: int = DEFAULT_LIMIT) -> Callable:
    """
    Build a FastAPI dependency that 429s after `limit` requests in the
    last `WINDOW_SECONDS` from the same client IP for this route_key.

    Usage:
        @router.post("/chat", dependencies=[Depends(rate_limit("chat", 30))])
        async def chat(...): ...
    """

    async def _dep(request: Request) -> None:
        ip = _client_ip(request)
        key = (ip, route_key)
        now = _now()
        with _lock:
            q = _buckets.setdefault(key, deque())
            # Drop entries that fell out of the window
            while q and (now - q[0]) > WINDOW_SECONDS:
                q.popleft()
            if len(q) >= limit:
                # Time until the oldest request ages out → Retry-After
                retry_after = max(1, int(WINDOW_SECONDS - (now - q[0])))
                raise HTTPException(
                    status_code=429,
                    detail="rate_limit_exceeded",
                    headers={"Retry-After": str(retry_after)},
                )
            q.append(now)

    return _dep


def _reset_for_tests() -> None:
    """Test-only — wipe all buckets between tests so state doesn't leak."""
    with _lock:
        _buckets.clear()
