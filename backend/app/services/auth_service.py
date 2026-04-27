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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64url_decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode((data + padding).encode("ascii"))


def _tier_0_attribute_placeholders(now: datetime) -> dict[str, Any]:
    """Tier=0 users have no Self proof yet; fields are NOT NULL by schema design.

    Values here are placeholders; they are overwritten in 2.3 when the Self
    ZK proof verification lands. See DESIGN_QUESTIONS DQ-8.
    """
    return {
        "attributes_proven": {},
        "attributes_verified_at": now,
        "attributes_expires_at": now + timedelta(days=1),
    }


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
