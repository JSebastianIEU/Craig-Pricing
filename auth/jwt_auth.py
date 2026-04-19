"""
JWT verification for admin API calls coming from Strategos Dashboard.

Strategos signs short-lived (5 min) HS256 tokens with claims:
    { email, org_slug, role, iat, exp, iss='strategos-dashboard', sub=email }

This module verifies signature + freshness and exposes a FastAPI dependency
that hands the claims to the endpoint handler.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal

import jwt  # PyJWT
from fastapi import Depends, HTTPException, Request, status

STRATEGOS_JWT_ISSUER = "strategos-dashboard"
STRATEGOS_JWT_ALGORITHMS = ["HS256"]

Role = Literal["strategos_admin", "client_owner", "client_member", "client_viewer"]
_ROLE_RANK: dict[Role, int] = {
    "client_viewer": 1,
    "client_member": 2,
    "client_owner": 3,
    "strategos_admin": 4,
}


@dataclass(frozen=True)
class StrategosClaims:
    """Parsed, verified claims from a Strategos-issued JWT."""

    email: str
    org_slug: str
    role: Role

    def has_at_least(self, min_role: Role) -> bool:
        return _ROLE_RANK[self.role] >= _ROLE_RANK[min_role]


def _secret() -> str:
    """Return the shared JWT secret, or raise if not configured."""
    secret = os.environ.get("STRATEGOS_JWT_SECRET")
    if not secret:
        raise HTTPException(
            status_code=500,
            detail="STRATEGOS_JWT_SECRET is not configured on this server.",
        )
    return secret


def _extract_bearer(request: Request) -> str:
    auth = request.headers.get("authorization") or request.headers.get("Authorization")
    if not auth or not auth.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return auth.split(" ", 1)[1].strip()


def require_claims(request: Request) -> StrategosClaims:
    """FastAPI dependency: verify the JWT and return its claims."""
    token = _extract_bearer(request)
    try:
        payload = jwt.decode(
            token,
            _secret(),
            algorithms=STRATEGOS_JWT_ALGORITHMS,
            issuer=STRATEGOS_JWT_ISSUER,
            options={"require": ["exp", "iat", "iss"]},
        )
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except jwt.InvalidTokenError as e:
        raise HTTPException(status_code=401, detail=f"Invalid token: {e}")

    email = payload.get("email") or payload.get("sub")
    org_slug = payload.get("org_slug")
    role = payload.get("role")

    if not email or not org_slug or role not in _ROLE_RANK:
        raise HTTPException(status_code=401, detail="Malformed claims")

    return StrategosClaims(email=email, org_slug=org_slug, role=role)  # type: ignore[arg-type]


def require_org_match(claims: StrategosClaims, path_org_slug: str) -> None:
    """
    Ensure the JWT's org_slug matches the one in the URL.
    strategos_admin tokens may access any org.
    """
    if claims.role == "strategos_admin":
        return
    if claims.org_slug != path_org_slug:
        raise HTTPException(
            status_code=403,
            detail=f"Token not valid for org '{path_org_slug}'",
        )


def require_role(claims: StrategosClaims, min_role: Role) -> None:
    if not claims.has_at_least(min_role):
        raise HTTPException(
            status_code=403,
            detail=f"Requires role >= {min_role}",
        )


def access_guard(path_org_slug: str, claims: StrategosClaims = Depends(require_claims)) -> StrategosClaims:
    """Combined guard: valid JWT + org match. Use for read endpoints."""
    require_org_match(claims, path_org_slug)
    return claims
