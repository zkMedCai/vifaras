"""Auth tests — tier 0 anonymous onboarding (brief task 2.1).

We monkeypatch `verify_registration_response` / `verify_authentication_response`
at the auth_service import site (the boundary). py-webauthn 2.7.1 doesn't
ship a fake authenticator helper, so building a synthetic CBOR/COSE rig
would be out of scope for 2.1. The tests cover *our* flow:
  - User row is persisted at tier=0 with nullifier_hash IS NULL,
  - the access_token decodes to (sub=user_id, tier=0),
  - duplicate email returns 409.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.core.security import decode_access_token, decode_refresh_token
from app.models.schema import User


def _fake_verified_registration() -> SimpleNamespace:
    """Stand-in for `webauthn.registration.VerifiedRegistration`.

    The auth_service reads `.credential_id`, `.credential_public_key`,
    `.sign_count`. The b64url encoding done downstream is byte-safe.
    """
    return SimpleNamespace(
        credential_id=b"mock-credential-id-bytes",
        credential_public_key=b"mock-cose-encoded-public-key-bytes",
        sign_count=0,
    )


def _fake_credential_payload() -> dict:
    """Shape of the JSON the browser would POST. Content irrelevant —
    `verify_registration_response` is patched to ignore it."""
    return {
        "id": "mock-cred",
        "rawId": "mock-cred",
        "type": "public-key",
        "response": {
            "attestationObject": "mock",
            "clientDataJSON": "mock",
        },
    }


@pytest.mark.db
async def test_register_tier_0_returns_jwt_and_persists_anonymous_user(
    http_client, async_db_session, monkeypatch
) -> None:
    monkeypatch.setattr(
        "app.services.auth_service.verify_registration_response",
        lambda **_: _fake_verified_registration(),
    )

    # /register/begin — receive options + challenge token
    begin = await http_client.post(
        "/api/auth/register/begin",
        json={"email": "alice@example.com"},
    )
    assert begin.status_code == 200
    begin_body = begin.json()
    assert "options" in begin_body
    assert "challenge" in begin_body["options"]
    challenge_token = begin_body["challenge_token"]

    # /register/complete — verify (mocked), persist user, get JWTs
    complete = await http_client.post(
        "/api/auth/register/complete",
        json={
            "credential": _fake_credential_payload(),
            "challenge_token": challenge_token,
        },
    )
    assert complete.status_code == 200
    body = complete.json()
    assert body["token_type"] == "bearer"
    assert body["user_id"]
    assert body["access_token"]
    assert body["refresh_token"]

    # User row is tier=0 with NO nullifier (the founder's critical assertion)
    user = await async_db_session.scalar(
        select(User).where(User.notification_email == "alice@example.com")
    )
    assert user is not None
    assert user.id == body["user_id"]
    assert user.tier == 0
    assert user.nullifier_hash is None
    assert user.attributes_proven == {}  # placeholder, will be overwritten at tier=1

    # Access token is valid and carries (sub=user_id, tier=0)
    access = decode_access_token(body["access_token"])
    assert access["sub"] == body["user_id"]
    assert access["tier"] == 0
    assert access["kind"] == "access"

    # Refresh token decodes too
    refresh = decode_refresh_token(body["refresh_token"])
    assert refresh["sub"] == body["user_id"]
    assert refresh["kind"] == "refresh"
    assert refresh["jti"]


@pytest.mark.db
async def test_register_rejects_duplicate_email(
    http_client, async_db_session, monkeypatch
) -> None:
    monkeypatch.setattr(
        "app.services.auth_service.verify_registration_response",
        lambda **_: _fake_verified_registration(),
    )

    # First registration — succeeds
    begin1 = await http_client.post(
        "/api/auth/register/begin",
        json={"email": "bob@example.com"},
    )
    assert begin1.status_code == 200
    complete1 = await http_client.post(
        "/api/auth/register/complete",
        json={
            "credential": _fake_credential_payload(),
            "challenge_token": begin1.json()["challenge_token"],
        },
    )
    assert complete1.status_code == 200

    # Second begin with the same email — 409 at /begin (early reject)
    begin2 = await http_client.post(
        "/api/auth/register/begin",
        json={"email": "bob@example.com"},
    )
    assert begin2.status_code == 409
    assert begin2.json()["detail"]["code"] == "email_already_registered"


@pytest.mark.db
async def test_login_flow_returns_valid_jwt(
    http_client, async_db_session, monkeypatch
) -> None:
    """register → login → JWT access carries (sub=user_id, tier=0, kind=access).

    Recovery of the login coverage that was wired but not tested in 2.1.
    Founder asked to add this in 2.2 since the gating tests rely implicitly
    on the same JWT-mint path.
    """
    monkeypatch.setattr(
        "app.services.auth_service.verify_registration_response",
        lambda **_: _fake_verified_registration(),
    )
    monkeypatch.setattr(
        "app.services.auth_service.verify_authentication_response",
        lambda **_: SimpleNamespace(new_sign_count=1),
    )
    email = "carol@example.com"

    # Register
    begin_r = await http_client.post(
        "/api/auth/register/begin", json={"email": email}
    )
    assert begin_r.status_code == 200
    complete_r = await http_client.post(
        "/api/auth/register/complete",
        json={
            "credential": _fake_credential_payload(),
            "challenge_token": begin_r.json()["challenge_token"],
        },
    )
    assert complete_r.status_code == 200
    user_id = complete_r.json()["user_id"]

    # Login with same email
    begin_l = await http_client.post(
        "/api/auth/login/begin", json={"email": email}
    )
    assert begin_l.status_code == 200
    assert "options" in begin_l.json()
    complete_l = await http_client.post(
        "/api/auth/login/complete",
        json={
            "credential": _fake_credential_payload(),
            "challenge_token": begin_l.json()["challenge_token"],
        },
    )
    assert complete_l.status_code == 200
    body = complete_l.json()
    assert body["user_id"] == user_id

    payload = decode_access_token(body["access_token"])
    assert payload["sub"] == user_id
    assert payload["tier"] == 0
    assert payload["kind"] == "access"


@pytest.mark.db
async def test_email_normalization_lowercase_strip(
    http_client, async_db_session, monkeypatch
) -> None:
    """Mixed-case + whitespace registration collapses to one canonical user."""
    monkeypatch.setattr(
        "app.services.auth_service.verify_registration_response",
        lambda **_: _fake_verified_registration(),
    )
    # Register with weird casing + leading whitespace
    begin1 = await http_client.post(
        "/api/auth/register/begin",
        json={"email": "  Dario@Example.COM"},
    )
    assert begin1.status_code == 200
    complete1 = await http_client.post(
        "/api/auth/register/complete",
        json={
            "credential": _fake_credential_payload(),
            "challenge_token": begin1.json()["challenge_token"],
        },
    )
    assert complete1.status_code == 200

    # User stored as the normalized form
    user = await async_db_session.scalar(
        select(User).where(User.notification_email == "dario@example.com")
    )
    assert user is not None

    # Second registration with canonical form is rejected as duplicate
    begin2 = await http_client.post(
        "/api/auth/register/begin",
        json={"email": "dario@example.com"},
    )
    assert begin2.status_code == 409
