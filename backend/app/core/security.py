"""JWT-backed tokens + tier-gating dependency for FastAPI.

All tokens encode with the same secret/algorithm from settings but carry
distinct `kind` claims and TTLs:

- access     (15 min) — `{sub, tier, kind="access", iat, exp}`
- refresh    (30 days) — `{sub, kind="refresh", jti, iat, exp}`
- challenge  (5 min) — `{challenge, user_id, email, purpose, kind="challenge", iat, exp}`

The challenge token is the stateless seam between WebAuthn begin/complete
endpoints (and, in 2.5, the step-up signature flow under `kind="step_up"`).
The `kind` claim is the discriminator: every `decode_*_token` verifies it,
so a refresh used as access (or vice-versa) fails at the boundary instead of
propagating downstream.

`require_tier(min_tier)` is the FastAPI dependency that gates routes by
`User.tier`. Tier-insufficient → 402 with the next-step payload (matches
brief §2.5 "402 Tier Upgrade Required"). Invalid/expired token → 401,
which is a different failure class.
"""
from __future__ import annotations

import base64
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any

import jwt
from fastapi import Header, HTTPException

from app.core.config import settings

_KIND_ACCESS = "access"
_KIND_REFRESH = "refresh"
_KIND_CHALLENGE = "challenge"

CHALLENGE_TTL_SECONDS = 300


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _encode(payload: dict[str, Any]) -> str:
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_alg)


def _decode(token: str, *, expected_kind: str) -> dict[str, Any]:
    payload = jwt.decode(
        token,
        settings.jwt_secret,
        algorithms=[settings.jwt_alg],
    )
    if payload.get("kind") != expected_kind:
        raise jwt.InvalidTokenError(
            f"expected kind={expected_kind!r}, got {payload.get('kind')!r}"
        )
    return payload


# ---------------------------------------------------------------------------
# Access / refresh
# ---------------------------------------------------------------------------


def create_access_token(*, user_id: str, tier: int) -> str:
    now = _now()
    return _encode(
        {
            "sub": user_id,
            "tier": tier,
            "kind": _KIND_ACCESS,
            "iat": int(now.timestamp()),
            "exp": int(
                (now + timedelta(minutes=settings.jwt_access_ttl_min)).timestamp()
            ),
        }
    )


def decode_access_token(token: str) -> dict[str, Any]:
    return _decode(token, expected_kind=_KIND_ACCESS)


def create_refresh_token(*, user_id: str) -> str:
    now = _now()
    return _encode(
        {
            "sub": user_id,
            "kind": _KIND_REFRESH,
            "jti": secrets.token_urlsafe(16),
            "iat": int(now.timestamp()),
            "exp": int(
                (now + timedelta(days=settings.jwt_refresh_ttl_days)).timestamp()
            ),
        }
    )


def decode_refresh_token(token: str) -> dict[str, Any]:
    return _decode(token, expected_kind=_KIND_REFRESH)


# ---------------------------------------------------------------------------
# WebAuthn challenge token
# ---------------------------------------------------------------------------


def create_challenge_token(
    *,
    challenge: bytes,
    user_id: str,
    email: str | None,
    purpose: str,
) -> str:
    if purpose not in ("register", "login"):
        raise ValueError(f"unknown challenge purpose: {purpose!r}")
    now = _now()
    return _encode(
        {
            "challenge": base64.urlsafe_b64encode(challenge).decode("ascii"),
            "user_id": user_id,
            "email": email,
            "purpose": purpose,
            "kind": _KIND_CHALLENGE,
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(seconds=CHALLENGE_TTL_SECONDS)).timestamp()),
        }
    )


def decode_challenge_token(
    token: str, *, expected_purpose: str
) -> dict[str, Any]:
    payload = _decode(token, expected_kind=_KIND_CHALLENGE)
    if payload.get("purpose") != expected_purpose:
        raise jwt.InvalidTokenError(
            f"expected purpose={expected_purpose!r}, got {payload.get('purpose')!r}"
        )
    return payload


def challenge_bytes_from_token_payload(payload: dict[str, Any]) -> bytes:
    """Return the raw challenge bytes for `expected_challenge=` in py-webauthn."""
    return base64.urlsafe_b64decode(payload["challenge"].encode("ascii"))


# ---------------------------------------------------------------------------
# Tier gating (FastAPI dependency)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CurrentUser:
    """Minimal authenticated context — no DB hit, just JWT claims."""

    user_id: str
    tier: int


# Where the mobile app should send the user to upgrade to the next tier.
# Static for V0; can become dynamic later (e.g. localized copy, A/B
# experiments). Keep keys = current tier (0, 1) — there is no "next step"
# from tier 2.
_NEXT_STEP_BY_TIER: dict[int, dict[str, str]] = {
    0: {
        "path": "/api/identity/verify-self",
        "description": (
            "Verifica la tua identità con la carta d'identità per attivare "
            "il tuo agente e iniziare a negoziare. 60 secondi, niente foto."
        ),
    },
    1: {
        "path": "/api/mandates/draft",
        "description": (
            "Autorizza il tuo agente con Face ID per finalizzare il primo "
            "deal. Decidi tu i limiti."
        ),
    },
}


def _bearer_token(authorization: str | None) -> str:
    if not authorization:
        raise HTTPException(
            status_code=401,
            detail={"code": "missing_token", "message": "Bearer token required"},
        )
    parts = authorization.strip().split(maxsplit=1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1]:
        raise HTTPException(
            status_code=401,
            detail={"code": "invalid_authorization_header", "message": "Expected `Bearer <token>`"},
        )
    return parts[1]


def try_extract_user_id(authorization: str | None) -> str | None:
    """Best-effort user_id from a bearer header, never raises (7.1.5).

    Used by exception handlers (rate-limit, moderation) that want the
    user_id when present but must continue without it on a missing or
    malformed header. Distinct from `require_tier` / `_bearer_token`,
    which raise; this returns `None`.
    """
    if not authorization:
        return None
    parts = authorization.strip().split(maxsplit=1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1]:
        return None
    try:
        payload = decode_access_token(parts[1])
        sub = payload.get("sub")
        return sub if isinstance(sub, str) and sub else None
    except Exception:
        return None


def require_tier(min_tier: int):
    """Factory: returns a FastAPI dependency that requires `tier >= min_tier`.

    Usage:
        @router.get("/something")
        async def handler(user: CurrentUser = Depends(require_tier(1))):
            ...

    Failure modes:
      - missing/invalid Authorization header → 401 (`missing_token` /
        `invalid_authorization_header`).
      - token expired or signature invalid → 401 (`token_expired` /
        `invalid_token`).
      - token valid but `tier < min_tier` → 402 with payload
        `{code, required_tier, current_tier, next_step}`.
    """

    async def dependency(
        authorization: Annotated[str | None, Header()] = None,
    ) -> CurrentUser:
        token = _bearer_token(authorization)
        try:
            payload = decode_access_token(token)
        except jwt.ExpiredSignatureError as exc:
            raise HTTPException(
                status_code=401,
                detail={"code": "token_expired", "message": "Access token expired"},
            ) from exc
        except jwt.InvalidTokenError as exc:
            raise HTTPException(
                status_code=401,
                detail={"code": "invalid_token", "message": str(exc)},
            ) from exc

        user_id = payload.get("sub")
        current_tier = payload.get("tier")
        if user_id is None or current_tier is None:
            raise HTTPException(
                status_code=401,
                detail={"code": "invalid_token", "message": "token missing required claims"},
            )

        if current_tier < min_tier:
            raise HTTPException(
                status_code=402,
                detail={
                    "code": "tier_upgrade_required",
                    "required_tier": min_tier,
                    "current_tier": current_tier,
                    "next_step": _NEXT_STEP_BY_TIER.get(current_tier, {}),
                },
            )

        return CurrentUser(user_id=user_id, tier=current_tier)

    return dependency
