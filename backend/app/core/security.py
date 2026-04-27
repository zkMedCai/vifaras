"""JWT-backed tokens: access, refresh, WebAuthn challenge.

All three encode with the same secret/algorithm from settings but carry
distinct `kind` claims and TTLs:

- access     (15 min) — `{sub, tier, kind="access", iat, exp}`
- refresh    (30 days) — `{sub, kind="refresh", jti, iat, exp}`
- challenge  (5 min) — `{challenge, user_id, email, purpose, kind="challenge", iat, exp}`

The challenge token is the stateless seam between the WebAuthn begin/complete
endpoints: the server hands it back with `PublicKeyCredentialCreationOptions`
(or `PublicKeyCredentialRequestOptions`), the client returns it with the
attestation/assertion, and the server decodes it to recover the original
challenge bytes for `verify_*_response(expected_challenge=...)`. No
server-side state, no Redis, no race conditions.
"""
from __future__ import annotations

import base64
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

import jwt

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
