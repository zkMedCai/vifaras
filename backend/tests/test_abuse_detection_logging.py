"""Abuse detection audit logging (brief task 7.1.5).

7 tests verify that:
  - Rate-limit 429s emit `RATE_LIMIT_API_HIT` audit rows (authenticated
    + anonymous variants)
  - Moderation 422s emit `MODERATION_REJECTED` rows
  - `complete_registration` always emits `register_complete` (and
    `SEQUENTIAL_EMAIL_DETECTED` when the prefix-burst threshold is met)

The audit table is queried directly via `async_db_session` rather than
through any service helper — these tests are about wire-level
observability, so the assertion is "the row landed in the DB with the
shape we expect."

The tests for sequential-email detection drive `complete_registration`
directly (bypassing the HTTP route + WebAuthn flow) so we can supply
arbitrary `actor_ip` per call.
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.rate_limit import limiter
from app.models.schema import AuditLog
from app.services import auth_service
from app.services.audit_service import AuthActions, SecurityActions


def _fake_verified_registration() -> SimpleNamespace:
    """Stand-in for webauthn `VerifiedRegistration` (mirrors test_auth.py)."""
    return SimpleNamespace(
        credential_id=b"mock-credential-id-bytes",
        credential_public_key=b"mock-cose-encoded-public-key-bytes",
        sign_count=0,
    )


def _fake_credential_payload() -> dict:
    return {
        "id": "mock-cred",
        "rawId": "mock-cred",
        "type": "public-key",
        "response": {"attestationObject": "mock", "clientDataJSON": "mock"},
    }


# ---------------------------------------------------------------------------
# Rate limit audit (RATE_LIMIT_API_HIT)
# ---------------------------------------------------------------------------


@pytest.mark.db
async def test_rate_limit_429_emits_audit_log_authenticated(
    enable_limiter,
    monkeypatch,
    authenticated_client,
    async_db_session: AsyncSession,
) -> None:
    """Authenticated 429 → AuditLog row with `user_id` from the JWT."""
    monkeypatch.setattr(settings, "rate_limit_post_strict", "1/minute")
    limiter.reset()

    client, ctx = authenticated_client(tier=2)
    body = {
        "side": "buy",
        "title": "x",
        "category": "electronics_laptops",
        "reservation_price_eur": 100.0,
        "ideal_price_eur": 80.0,
    }
    await client.post("/api/intents", json=body)
    blocked = await client.post("/api/intents", json=body)
    assert blocked.status_code == 429

    # The handler mints its own session + commits, so the audit row is
    # outside the test's `async_db_session` outer transaction. We must
    # query for it directly; expire to bypass any stale session cache.
    row = await async_db_session.scalar(
        select(AuditLog)
        .where(AuditLog.action == SecurityActions.RATE_LIMIT_API_HIT)
        .where(AuditLog.user_id == ctx["user_id"])
        .order_by(AuditLog.timestamp.desc())
    )
    assert row is not None, "expected RATE_LIMIT_API_HIT row for authenticated user"
    assert row.success is False
    assert row.error_code == "rate_limited"
    assert row.params["endpoint"] == "/api/intents"
    assert row.params["method"] == "POST"
    # Cleanup so the row doesn't pollute downstream tests (the handler's
    # session is outside the test rollback scope).
    await async_db_session.delete(row)
    await async_db_session.commit()


@pytest.mark.db
async def test_rate_limit_429_emits_audit_log_anonymous(
    enable_limiter,
    monkeypatch,
    http_client,
    async_db_session: AsyncSession,
) -> None:
    """Pre-auth 429 → AuditLog row with NULL user_id, IP populated."""
    monkeypatch.setattr(settings, "rate_limit_auth_strict", "1/minute")
    limiter.reset()

    body = {"email": f"rl-{uuid.uuid4().hex[:6]}@example.com"}
    await http_client.post("/api/auth/register/begin", json=body)
    blocked = await http_client.post("/api/auth/register/begin", json=body)
    assert blocked.status_code == 429

    row = await async_db_session.scalar(
        select(AuditLog)
        .where(AuditLog.action == SecurityActions.RATE_LIMIT_API_HIT)
        .where(AuditLog.user_id.is_(None))
        .order_by(AuditLog.timestamp.desc())
    )
    assert row is not None, "expected RATE_LIMIT_API_HIT row for anonymous"
    assert row.user_id is None
    assert row.actor_ip is not None
    assert row.params["endpoint"] == "/api/auth/register/begin"
    await async_db_session.delete(row)
    await async_db_session.commit()


# ---------------------------------------------------------------------------
# Moderation audit (MODERATION_REJECTED)
# ---------------------------------------------------------------------------


@pytest.mark.db
async def test_moderation_422_emits_audit_log(
    authenticated_client, async_db_session: AsyncSession
) -> None:
    """ProfanityDetected on /api/intents → MODERATION_REJECTED row with
    `field` in params."""
    client, ctx = authenticated_client(tier=2)
    body = {
        "side": "buy",
        "title": "fuck this listing",
        "category": "electronics_laptops",
        "reservation_price_eur": 100.0,
        "ideal_price_eur": 80.0,
    }
    r = await client.post("/api/intents", json=body)
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "profanity_detected"

    row = await async_db_session.scalar(
        select(AuditLog)
        .where(AuditLog.action == SecurityActions.MODERATION_REJECTED)
        .where(AuditLog.user_id == ctx["user_id"])
        .order_by(AuditLog.timestamp.desc())
    )
    assert row is not None
    assert row.success is False
    assert row.error_code == "profanity_detected"
    assert row.params["field"] == "title"
    assert row.params["endpoint"] == "/api/intents"
    await async_db_session.delete(row)
    await async_db_session.commit()


# ---------------------------------------------------------------------------
# Sequential-email detection
# ---------------------------------------------------------------------------


async def _register_via_service(
    db: AsyncSession,
    monkeypatch,
    *,
    email: str,
    actor_ip: str,
) -> str:
    """Helper: drive `complete_registration` directly with a mocked
    WebAuthn verify and an explicit `actor_ip`. Returns the new user_id.

    Goes around the HTTP route so each call can supply a distinct IP.
    The route always uses `request.client.host`, which is fixed for
    the test transport; this helper is the only way to vary it."""
    monkeypatch.setattr(
        "app.services.auth_service.verify_registration_response",
        lambda **_: _fake_verified_registration(),
    )
    # /begin to mint a real challenge_token
    _options, challenge_token = await auth_service.begin_registration(
        db, email=email
    )
    user_id, _access, _refresh = await auth_service.complete_registration(
        db,
        credential=_fake_credential_payload(),
        challenge_token=challenge_token,
        actor_ip=actor_ip,
    )
    return user_id


@pytest.mark.db
async def test_register_complete_emits_audit_log(
    monkeypatch, async_db_session: AsyncSession
) -> None:
    """Every successful register emits an `AuthActions.REGISTER_COMPLETE`
    row tagged with the actor IP and (when applicable) email_prefix.

    Note: the local-part must match `^[a-z]+\\d+` exactly to extract a
    prefix (`alice1` matches, `alice-1` does not). The fixture's outer
    transaction rolls back at teardown, so a fixed-name email like
    `alice1@example.com` doesn't pollute downstream tests."""
    user_id = await _register_via_service(
        async_db_session,
        monkeypatch,
        email="alice1@example.com",
        actor_ip="203.0.113.10",
    )

    row = await async_db_session.scalar(
        select(AuditLog)
        .where(AuditLog.action == AuthActions.REGISTER_COMPLETE)
        .where(AuditLog.user_id == user_id)
    )
    assert row is not None
    assert row.actor_ip == "203.0.113.10"
    # `alice1` → leading-letters "alice"
    assert row.params["email_prefix"] == "alice"


@pytest.mark.db
async def test_sequential_email_third_attempt_triggers_detection(
    monkeypatch, async_db_session: AsyncSession
) -> None:
    """3 successful registers, same prefix + same IP, within window →
    `SEQUENTIAL_EMAIL_DETECTED` row appears on the 3rd."""
    ip = "198.51.100.42"
    prefix = "attacker"  # letters only — must match `^[a-z]+\\d+`

    for n in (1, 2, 3):
        await _register_via_service(
            async_db_session,
            monkeypatch,
            email=f"{prefix}{n}@example.com",
            actor_ip=ip,
        )

    detected = await async_db_session.scalars(
        select(AuditLog)
        .where(AuditLog.action == SecurityActions.SEQUENTIAL_EMAIL_DETECTED)
        .where(AuditLog.actor_ip == ip)
    )
    rows = list(detected)
    assert len(rows) == 1, f"expected exactly 1 DETECTED row, got {len(rows)}"
    assert rows[0].params["email_prefix"] == prefix
    assert rows[0].params["matching_count"] == 3


@pytest.mark.db
async def test_sequential_email_below_threshold_no_detection(
    monkeypatch, async_db_session: AsyncSession
) -> None:
    """2 registers (under threshold=3) → NO `SEQUENTIAL_EMAIL_DETECTED`
    row. Only 2 `REGISTER_COMPLETE` rows."""
    ip = "198.51.100.43"
    prefix = "underthr"

    for n in (1, 2):
        await _register_via_service(
            async_db_session,
            monkeypatch,
            email=f"{prefix}{n}@example.com",
            actor_ip=ip,
        )

    detected = await async_db_session.scalars(
        select(AuditLog)
        .where(AuditLog.action == SecurityActions.SEQUENTIAL_EMAIL_DETECTED)
        .where(AuditLog.actor_ip == ip)
    )
    assert list(detected) == [], "below-threshold burst must NOT trigger DETECTED"


@pytest.mark.db
async def test_sequential_email_skips_complex_local_part(
    monkeypatch, async_db_session: AsyncSession
) -> None:
    """Emails with dot/underscore/dash in the local part are NOT pattern-
    matched (legitimate non-burst names like `john.doe`, `mario_rossi`,
    `anna-bianchi`). The detection regex `^[a-z]+\\d+@` only fires on
    `<letters><digits>@` shape — the others skip detection entirely.

    3 register attempts with `john.doe<n>@` from the same IP → no
    SEQUENTIAL_EMAIL_DETECTED row. The audit `register_complete` rows
    are still emitted, but with `email_prefix` absent in params (the
    helper writes params=None when prefix can't be extracted)."""
    ip = "203.0.113.99"
    for n in (1, 2, 3):
        await _register_via_service(
            async_db_session,
            monkeypatch,
            email=f"john.doe{n}@example.com",  # dot — won't match regex
            actor_ip=ip,
        )

    detected = await async_db_session.scalars(
        select(AuditLog)
        .where(AuditLog.action == SecurityActions.SEQUENTIAL_EMAIL_DETECTED)
        .where(AuditLog.actor_ip == ip)
    )
    assert list(detected) == [], "complex local-part emails must skip detection"


@pytest.mark.db
async def test_sequential_email_different_ips_no_aggregation(
    monkeypatch, async_db_session: AsyncSession
) -> None:
    """3 registers, same prefix, but distinct IPs → NO DETECTED row.
    The IP axis is part of the detection key by design (same prefix
    from a residential pool of distinct addresses isn't a sequential-
    burst signal — could be organic)."""
    prefix = "diffip"
    ips = ["203.0.113.50", "203.0.113.51", "203.0.113.52"]

    for n, ip in zip((1, 2, 3), ips):
        await _register_via_service(
            async_db_session,
            monkeypatch,
            email=f"{prefix}{n}@example.com",
            actor_ip=ip,
        )

    detected = await async_db_session.scalars(
        select(AuditLog)
        .where(AuditLog.action == SecurityActions.SEQUENTIAL_EMAIL_DETECTED)
        .where(AuditLog.params["email_prefix"].astext == prefix)
    )
    assert list(detected) == [], "burst across distinct IPs must NOT trigger"
