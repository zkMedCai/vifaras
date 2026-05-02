"""Refresh token rotation + reuse detection tests ([7.4.2]).

Coverage:
  1. issue_refresh_token shape: opaque plaintext returned, hash stored
  2. consume rotates atomically: old → consumed, new → active, parent_id link
  3. expired token raises RefreshTokenExpired
  4. revoked token raises RefreshTokenRevoked
  5. unknown token raises RefreshTokenNotFound
  6. reuse detection: replaying a consumed token revokes the whole user chain
     and the exception carries (user_id, revoked_count) metadata
  7. /api/auth/refresh endpoint emits security audit row + Prometheus counter
     bump on reuse hit, and returns 401 with code=refresh_token_reuse
"""
from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from app.models.schema import AuditLog, RefreshToken, User
from app.services.refresh_token_service import (
    RefreshTokenAlreadyConsumed,
    RefreshTokenExpired,
    RefreshTokenNotFound,
    RefreshTokenRevoked,
    consume_refresh_token,
    issue_refresh_token,
)
from tests.factories import default_user_kwargs


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _seed_user(db, *, email: str = "u@example.com") -> User:
    """Insert a tier-0 user fresh enough to satisfy the FK on `refresh_tokens.user_id`."""
    user = User(**default_user_kwargs(tier=0, email=email))
    db.add(user)
    await db.commit()
    return user


# ---------------------------------------------------------------------------
# refresh_token_service unit tests
# ---------------------------------------------------------------------------


@pytest.mark.db
async def test_issue_refresh_token_returns_plaintext_and_id(async_db_session):
    user = await _seed_user(async_db_session, email="issue@example.com")

    plain, token_id = await issue_refresh_token(async_db_session, user_id=user.id)
    await async_db_session.commit()

    # Plaintext is URL-safe base64 of 32 random bytes (~43 chars after padding strip).
    assert isinstance(plain, str)
    assert len(plain) >= 32

    row = await async_db_session.get(RefreshToken, token_id)
    assert row is not None
    assert row.status == "active"
    assert row.user_id == user.id
    assert row.parent_id is None
    # Plaintext MUST NOT be persisted — only the SHA-256 hex digest.
    assert row.token_hash != plain
    assert row.token_hash == hashlib.sha256(plain.encode("utf-8")).hexdigest()


@pytest.mark.db
async def test_consume_rotates_atomically(async_db_session):
    user = await _seed_user(async_db_session, email="rotate@example.com")
    plain_old, id_old = await issue_refresh_token(
        async_db_session, user_id=user.id
    )
    await async_db_session.commit()

    new_plain, new_id, returned_user_id = await consume_refresh_token(
        async_db_session, plain_old
    )
    await async_db_session.commit()

    assert returned_user_id == user.id
    assert new_plain != plain_old

    old_row = await async_db_session.get(RefreshToken, id_old)
    assert old_row.status == "consumed"
    assert old_row.consumed_at is not None

    new_row = await async_db_session.get(RefreshToken, new_id)
    assert new_row.status == "active"
    assert new_row.parent_id == id_old
    assert new_row.user_id == user.id


@pytest.mark.db
async def test_consume_expired_token_raises(async_db_session):
    user = await _seed_user(async_db_session, email="expired@example.com")
    plain, token_id = await issue_refresh_token(
        async_db_session, user_id=user.id
    )
    # Force expiry into the past.
    row = await async_db_session.get(RefreshToken, token_id)
    row.expires_at = datetime.utcnow() - timedelta(hours=1)
    await async_db_session.commit()

    with pytest.raises(RefreshTokenExpired):
        await consume_refresh_token(async_db_session, plain)


@pytest.mark.db
async def test_consume_revoked_token_raises(async_db_session):
    user = await _seed_user(async_db_session, email="revoked@example.com")
    plain, token_id = await issue_refresh_token(
        async_db_session, user_id=user.id
    )
    row = await async_db_session.get(RefreshToken, token_id)
    row.status = "revoked"
    await async_db_session.commit()

    with pytest.raises(RefreshTokenRevoked):
        await consume_refresh_token(async_db_session, plain)


@pytest.mark.db
async def test_consume_unknown_token_raises(async_db_session):
    fake = secrets.token_urlsafe(32)
    with pytest.raises(RefreshTokenNotFound):
        await consume_refresh_token(async_db_session, fake)


@pytest.mark.db
async def test_reuse_detection_invalidates_chain(async_db_session):
    user = await _seed_user(async_db_session, email="reuse@example.com")
    plain1, _ = await issue_refresh_token(async_db_session, user_id=user.id)
    await async_db_session.commit()

    # Legit rotation: plain1 → consumed, plain2 issued.
    plain2, _, _ = await consume_refresh_token(async_db_session, plain1)
    await async_db_session.commit()

    # Replay plain1 → reuse hit.
    with pytest.raises(RefreshTokenAlreadyConsumed) as exc_info:
        await consume_refresh_token(async_db_session, plain1)
    await async_db_session.commit()

    # Exception carries metadata for the audit hook.
    assert exc_info.value.user_id == user.id
    # plain1 was already 'consumed' before the reuse hit; plain2 was 'active'.
    # Both get flipped to 'revoked' by _invalidate_user_tokens.
    assert exc_info.value.revoked_count == 2

    rows = (
        await async_db_session.execute(
            select(RefreshToken).where(RefreshToken.user_id == user.id)
        )
    ).scalars().all()
    assert len(rows) == 2
    assert all(r.status == "revoked" for r in rows)


# ---------------------------------------------------------------------------
# /api/auth/refresh endpoint integration
# ---------------------------------------------------------------------------


def _fake_credential_payload() -> dict:
    """Mirror of test_auth.py helper — minimal credential dict for WebAuthn mock."""
    return {
        "id": "fake",
        "rawId": "fake",
        "type": "public-key",
        "response": {
            "clientDataJSON": "fake",
            "attestationObject": "fake",
        },
    }


def _fake_verified_registration():
    """Mirror of the verify_registration_response monkeypatch return shape."""
    from types import SimpleNamespace

    return SimpleNamespace(
        credential_id=b"fake-credential-id",
        credential_public_key=b"fake-pubkey-cose",
        sign_count=0,
    )


@pytest.mark.db
async def test_refresh_endpoint_emits_audit_and_metric_on_reuse(
    http_client, async_db_session, monkeypatch
):
    """Replaying a consumed refresh through the API yields 401, an audit row, and a counter bump."""
    from app.core.metrics import REFRESH_TOKEN_REUSE_TOTAL

    monkeypatch.setattr(
        "app.services.auth_service.verify_registration_response",
        lambda **_: _fake_verified_registration(),
    )

    # Register → first refresh token in hand.
    begin = await http_client.post(
        "/api/auth/register/begin", json={"email": "reuse-api@example.com"}
    )
    complete = await http_client.post(
        "/api/auth/register/complete",
        json={
            "credential": _fake_credential_payload(),
            "challenge_token": begin.json()["challenge_token"],
        },
    )
    body = complete.json()
    user_id = body["user_id"]
    initial_refresh = body["refresh_token"]

    # Legit rotation succeeds.
    rotate = await http_client.post(
        "/api/auth/refresh", json={"refresh_token": initial_refresh}
    )
    assert rotate.status_code == 200, rotate.text

    metric_before = REFRESH_TOKEN_REUSE_TOTAL._value.get()

    # Replay the original refresh → reuse detection.
    replay = await http_client.post(
        "/api/auth/refresh", json={"refresh_token": initial_refresh}
    )
    assert replay.status_code == 401
    assert replay.json()["detail"]["code"] == "refresh_token_reuse"

    metric_after = REFRESH_TOKEN_REUSE_TOTAL._value.get()
    assert metric_after == metric_before + 1

    # Audit row landed for this user.
    audit_rows = (
        await async_db_session.execute(
            select(AuditLog)
            .where(AuditLog.action == "refresh_token_reuse")
            .where(AuditLog.user_id == user_id)
        )
    ).scalars().all()
    assert len(audit_rows) == 1
    assert audit_rows[0].params["revoked_count"] >= 1
