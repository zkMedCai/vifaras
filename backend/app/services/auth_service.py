"""Authentication service — tier 0 anonymous onboarding (brief task 2.1).

Async-first per brief §7. Uses AsyncSession + select() throughout. WebAuthn
flows are stateless: the challenge value travels in a server-signed JWT
between /begin and /complete (see core.security.create_challenge_token).

Public surface:
- begin_registration(db, email)        → (options_dict, challenge_token)
- complete_registration(db, ...)       → (user_id, access_jwt, refresh_jwt)
- begin_login(db, email)               → (options_dict, challenge_token)
- complete_login(db, ...)              → (user_id, access_jwt, refresh_jwt)

Errors raise AuthError subclasses; the API layer maps them to HTTP codes.
"""
from __future__ import annotations

import base64
import json
import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    UserVerificationRequirement,
)

from app.core.config import settings
from app.core.security import (
    challenge_bytes_from_token_payload,
    create_access_token,
    create_challenge_token,
    create_refresh_token,
    decode_challenge_token,
)
from app.models.schema import User


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class AuthError(Exception):
    code: str = "auth_error"
    http_status: int = 400


class EmailAlreadyRegistered(AuthError):
    code = "email_already_registered"
    http_status = 409


class UserNotFound(AuthError):
    code = "user_not_found"
    http_status = 404


class InvalidCredential(AuthError):
    code = "invalid_credential"
    http_status = 401


class InvalidChallengeToken(AuthError):
    code = "invalid_challenge_token"
    http_status = 401


class InvalidRefreshToken(AuthError):
    code = "invalid_refresh_token"
    http_status = 401


class UserNotActive(AuthError):
    code = "user_not_active"
    http_status = 403


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64url_decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode((data + padding).encode("ascii"))


def _tier_0_attribute_placeholders(now: datetime) -> dict[str, Any]:
    """Sentinel values for the schema's NOT NULL Self-attributes columns at tier=0.

    These are NOT meaningful data — they exist only to satisfy the existing
    NOT NULL constraints on `attributes_proven` / `attributes_verified_at` /
    `attributes_expires_at`, which were designed for tier=1+ users with a real
    Self ZK proof. At tier=0 there is no proof yet, so:

      - `attributes_proven={}` — read as "nothing proven", NOT as "user proved
        an empty set of attributes".
      - `attributes_verified_at=NOW` — placeholder timestamp, NOT the time of
        any real attribute verification.
      - `attributes_expires_at=NOW+1d` — placeholder; treats the placeholder
        cluster as "stale by tomorrow" so any accidental tier=0 read of these
        fields will at least look obviously suspicious to a debugger.

    All three are overwritten with real values in 2.3 (Self verification),
    which is the only place a downstream service should treat them as data.
    Until then, gating must check `User.tier` first.

    See DESIGN_QUESTIONS DQ-8 for the rationale of placeholder vs schema
    relax (5-alter migration was rejected to keep scope at 2 alters).
    """
    return {
        "attributes_proven": {},
        "attributes_verified_at": now,
        "attributes_expires_at": now + timedelta(days=1),
    }


def _normalize_email(email: str) -> str:
    """Lower-case + strip whitespace. Always called at the service boundary
    so `User@gmail.com` and `user@gmail.com` collapse to the same identity
    before any DB lookup or insert."""
    return email.strip().lower()


async def _email_taken(db: AsyncSession, email: str) -> bool:
    existing = await db.scalar(
        select(User).where(User.notification_email == email)
    )
    return existing is not None


# ---------------------------------------------------------------------------
# Registration (tier 0)
# ---------------------------------------------------------------------------


async def begin_registration(
    db: AsyncSession, *, email: str
) -> tuple[dict[str, Any], str]:
    """Return WebAuthn registration options + a stateless challenge token."""
    email = _normalize_email(email)
    if await _email_taken(db, email):
        raise EmailAlreadyRegistered(email)

    user_id = str(uuid.uuid4())

    options = generate_registration_options(
        rp_id=settings.webauthn_rp_id,
        rp_name=settings.webauthn_rp_name,
        user_name=email,
        user_id=user_id.encode("utf-8"),
        user_display_name=email,
        authenticator_selection=AuthenticatorSelectionCriteria(
            user_verification=UserVerificationRequirement.PREFERRED,
        ),
    )

    token = create_challenge_token(
        challenge=options.challenge,
        user_id=user_id,
        email=email,
        purpose="register",
    )
    return json.loads(options_to_json(options)), token


async def complete_registration(
    db: AsyncSession,
    *,
    credential: dict[str, Any] | str,
    challenge_token: str,
) -> tuple[str, str, str]:
    """Verify attestation; persist tier=0 user; return (user_id, access, refresh)."""
    try:
        payload = decode_challenge_token(
            challenge_token, expected_purpose="register"
        )
    except Exception as exc:
        raise InvalidChallengeToken(str(exc)) from exc

    expected_challenge = challenge_bytes_from_token_payload(payload)

    try:
        verified = verify_registration_response(
            credential=credential,
            expected_challenge=expected_challenge,
            expected_rp_id=settings.webauthn_rp_id,
            expected_origin=settings.webauthn_origin,
            require_user_verification=False,
        )
    except Exception as exc:
        raise InvalidCredential(str(exc)) from exc

    user_id: str = payload["user_id"]
    email: str = payload["email"]

    # Re-check email uniqueness — a parallel request could have beaten us
    # between /begin and /complete; this is the last app-level guard until
    # a DB-level partial-unique index is added.
    if await _email_taken(db, email):
        raise EmailAlreadyRegistered(email)

    now = datetime.utcnow()
    user = User(
        id=user_id,
        tier=0,
        nullifier_hash=None,
        passkey_credential_id=_b64url(verified.credential_id),
        passkey_pubkey=_b64url(verified.credential_public_key),
        passkey_sign_count=verified.sign_count,
        notification_email=email,
        status="active",
        created_at=now,
        last_active_at=now,
        **_tier_0_attribute_placeholders(now),
    )
    db.add(user)
    await db.commit()

    access = create_access_token(user_id=user_id, tier=0)
    refresh = create_refresh_token(user_id=user_id)
    return user_id, access, refresh


# ---------------------------------------------------------------------------
# Login (any tier)
# ---------------------------------------------------------------------------


async def begin_login(
    db: AsyncSession, *, email: str
) -> tuple[dict[str, Any], str]:
    email = _normalize_email(email)
    user = await db.scalar(
        select(User).where(User.notification_email == email)
    )
    if user is None:
        raise UserNotFound(email)

    options = generate_authentication_options(
        rp_id=settings.webauthn_rp_id,
        allow_credentials=[
            PublicKeyCredentialDescriptor(
                id=_b64url_decode(user.passkey_credential_id)
            )
        ],
        user_verification=UserVerificationRequirement.PREFERRED,
    )

    token = create_challenge_token(
        challenge=options.challenge,
        user_id=user.id,
        email=email,
        purpose="login",
    )
    return json.loads(options_to_json(options)), token


async def complete_login(
    db: AsyncSession,
    *,
    credential: dict[str, Any] | str,
    challenge_token: str,
) -> tuple[str, str, str]:
    try:
        payload = decode_challenge_token(
            challenge_token, expected_purpose="login"
        )
    except Exception as exc:
        raise InvalidChallengeToken(str(exc)) from exc

    expected_challenge = challenge_bytes_from_token_payload(payload)
    user_id: str = payload["user_id"]

    user = await db.scalar(select(User).where(User.id == user_id))
    if user is None:
        raise UserNotFound(user_id)

    try:
        verified = verify_authentication_response(
            credential=credential,
            expected_challenge=expected_challenge,
            expected_rp_id=settings.webauthn_rp_id,
            expected_origin=settings.webauthn_origin,
            credential_public_key=_b64url_decode(user.passkey_pubkey),
            credential_current_sign_count=user.passkey_sign_count or 0,
            require_user_verification=False,
        )
    except Exception as exc:
        raise InvalidCredential(str(exc)) from exc

    user.passkey_sign_count = verified.new_sign_count
    user.last_active_at = datetime.utcnow()
    await db.commit()

    access = create_access_token(user_id=user.id, tier=user.tier)
    refresh = create_refresh_token(user_id=user.id)
    return user.id, access, refresh


# ---------------------------------------------------------------------------
# Refresh access token (brief task 2.5)
# ---------------------------------------------------------------------------


async def refresh_access_token(
    db: AsyncSession, *, refresh_token: str
) -> tuple[str, int]:
    """Exchange a refresh token for a fresh access token.

    Returns `(new_access_token, ttl_seconds)`. The refresh token itself
    is unchanged in V0 (rotation deferred to V1 — DESIGN_QUESTIONS DQ-25).

    Crucially the new access token carries the *current* `user.tier` from
    the DB, not the tier embedded in the refresh JWT. Otherwise a user
    promoted to tier 1 mid-session via Self verification would keep being
    issued tier-0 tokens until their refresh expires.
    """
    from app.core.security import decode_refresh_token

    try:
        payload = decode_refresh_token(refresh_token)
    except Exception as exc:
        raise InvalidRefreshToken(str(exc)) from exc

    user_id: str | None = payload.get("sub")
    if not user_id:
        raise InvalidRefreshToken("missing sub claim")

    user = await db.scalar(select(User).where(User.id == user_id))
    if user is None:
        raise InvalidRefreshToken("user no longer exists")
    if user.status != "active":
        raise UserNotActive(f"user.status={user.status!r}")

    new_access = create_access_token(user_id=user.id, tier=user.tier)
    ttl_seconds = settings.jwt_access_ttl_min * 60
    return new_access, ttl_seconds
