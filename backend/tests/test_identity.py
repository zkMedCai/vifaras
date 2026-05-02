"""Identity service & verify-self endpoint tests (brief task 2.3).

Coverage of the 0→1 upgrade flow:

  1. happy path                — Self verifier OK, user becomes tier 1,
                                  agent created with status='pending_mandate'.
  2. invalid proof              — verifier returns verified=false → 422.
  3. minor user                 — verified=true but isAdult=false → 422.
  4. idempotent re-call         — second verify-self call on a tier-1 user
                                  returns 200 with already_upgraded=true.
  5. nullifier collision        — two distinct users, same document → 409
                                  on the second.
  6. verifier timeout           — httpx.TimeoutException from verifier
                                  → 500 verifier_unavailable, user stays
                                  at tier 0.
  7. atomic rollback on KMS err — KMS keygen raises after proof verified
                                  → 500 kms_error, user untouched, no
                                  agent row created.

The Self verifier itself is fully mocked via `self_verifier_mock` (which
auto-patches `identity_service._post_to_self_verifier`). py-webauthn is
also mocked at the boundary in registration so we can mint tier-0 users
without a real authenticator.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import select

from app.core.security import decode_access_token
from app.models.schema import Agent, User


# ---------------------------------------------------------------------------
# Helpers — register a tier-0 user via the auth API
# ---------------------------------------------------------------------------


def _fake_credential_payload() -> dict[str, Any]:
    return {
        "id": "mock-cred",
        "rawId": "mock-cred",
        "type": "public-key",
        "response": {"attestationObject": "mock", "clientDataJSON": "mock"},
    }


def _patch_webauthn_register(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.services.auth_service.verify_registration_response",
        lambda **_: SimpleNamespace(
            credential_id=b"mock-credential-id",
            credential_public_key=b"mock-cose-pubkey",
            sign_count=0,
        ),
    )


async def _register_tier_0(http_client, email: str) -> str:
    """Register a tier-0 user via the HTTP API. Returns user_id."""
    begin = await http_client.post(
        "/api/auth/register/begin", json={"email": email}
    )
    assert begin.status_code == 200, begin.text
    complete = await http_client.post(
        "/api/auth/register/complete",
        json={
            "credential": _fake_credential_payload(),
            "challenge_token": begin.json()["challenge_token"],
        },
    )
    assert complete.status_code == 200, complete.text
    return complete.json()["user_id"]


def _proof_request_body() -> dict[str, Any]:
    """Body the mobile app POSTs to /api/identity/verify-self.

    Content is irrelevant — `_post_to_self_verifier` is mocked, so the
    proof and publicSignals never reach a real verifier.
    """
    return {"proof": "mock-zk-proof-base64", "publicSignals": []}


# ---------------------------------------------------------------------------
# 1. Happy path
# ---------------------------------------------------------------------------


@pytest.mark.db
async def test_tier_0_can_upgrade_to_tier_1_happy_path(
    http_client,
    async_db_session,
    authenticated_client,
    self_verifier_mock,
    monkeypatch,
) -> None:
    _patch_webauthn_register(monkeypatch)
    user_id = await _register_tier_0(http_client, "happy@example.com")
    client, _ = authenticated_client(tier=0, user_id=user_id)
    self_verifier_mock.set_response(
        self_verifier_mock.valid_italian_adult_proof(user_identifier=user_id)
    )

    response = await client.post(
        "/api/identity/verify-self", json=_proof_request_body()
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["tier"] == 1
    assert body["user_id"] == user_id
    assert body["agent_id"]
    assert body["agent_pubkey"]
    assert body["nullifier_hash"] == "self_nullifier_valid_it_adult"
    assert body["already_upgraded"] is False
    assert body["attributes_proven"]["isAdult"] is True
    assert body["attributes_proven"]["issuingState"] == "IT"
    assert body["next_step"]["action"] == "configure_mandate"
    assert body["next_step"]["endpoint"] == "/api/mandates/draft"

    # Response carries a fresh access token bearing the new tier.
    assert body["access_token"]
    assert body["token_type"] == "bearer"
    refreshed = decode_access_token(body["access_token"])
    assert refreshed["sub"] == user_id
    assert refreshed["tier"] == 1
    assert refreshed["kind"] == "access"

    # DB-side: User row mutated as expected.
    user = await async_db_session.scalar(select(User).where(User.id == user_id))
    assert user is not None
    assert user.tier == 1
    assert user.nullifier_hash == "self_nullifier_valid_it_adult"
    assert user.attributes_proven["isAdult"] is True
    assert user.attributes_proven["issuingState"] == "IT"
    assert user.attributes_proven["documentValid"] is True

    # Agent row created with status=pending_mandate, holding kms_ref.
    agent = await async_db_session.scalar(
        select(Agent).where(Agent.user_id == user_id)
    )
    assert agent is not None
    assert agent.status == "pending_mandate"
    assert agent.pubkey == body["agent_pubkey"]
    assert agent.privkey_kms_ref.startswith("db:")

    # Verifier was called exactly once with the expected scope and userIdentifier.
    assert len(self_verifier_mock.calls) == 1
    sent = self_verifier_mock.calls[0]
    assert sent["scope"] == "marketplace-it-v0"
    assert sent["userIdentifier"] == user_id
    assert sent["disclosureRequirements"]["minimumAge"] == 18
    assert sent["disclosureRequirements"]["issuingState"] == ["IT"]


# ---------------------------------------------------------------------------
# 2. Invalid proof
# ---------------------------------------------------------------------------


@pytest.mark.db
async def test_upgrade_fails_with_invalid_proof(
    http_client,
    async_db_session,
    authenticated_client,
    self_verifier_mock,
    monkeypatch,
) -> None:
    _patch_webauthn_register(monkeypatch)
    user_id = await _register_tier_0(http_client, "invalid@example.com")
    client, _ = authenticated_client(tier=0, user_id=user_id)
    self_verifier_mock.set_response(
        self_verifier_mock.invalid_proof(user_identifier=user_id)
    )

    response = await client.post(
        "/api/identity/verify-self", json=_proof_request_body()
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "self.proof_invalid"

    # User row unchanged.
    user = await async_db_session.scalar(select(User).where(User.id == user_id))
    assert user is not None
    assert user.tier == 0
    assert user.nullifier_hash is None

    # No agent row was created.
    agent = await async_db_session.scalar(
        select(Agent).where(Agent.user_id == user_id)
    )
    assert agent is None


# ---------------------------------------------------------------------------
# 3. Minor user
# ---------------------------------------------------------------------------


@pytest.mark.db
async def test_upgrade_fails_with_minor_user(
    http_client,
    async_db_session,
    authenticated_client,
    self_verifier_mock,
    monkeypatch,
) -> None:
    _patch_webauthn_register(monkeypatch)
    user_id = await _register_tier_0(http_client, "minor@example.com")
    client, _ = authenticated_client(tier=0, user_id=user_id)
    self_verifier_mock.set_response(
        self_verifier_mock.minor_proof(user_identifier=user_id)
    )

    response = await client.post(
        "/api/identity/verify-self", json=_proof_request_body()
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "self.isadult_required"

    user = await async_db_session.scalar(select(User).where(User.id == user_id))
    assert user.tier == 0
    assert user.nullifier_hash is None


# ---------------------------------------------------------------------------
# 4. Idempotent re-call
# ---------------------------------------------------------------------------


@pytest.mark.db
async def test_upgrade_idempotent_for_already_tier_1(
    http_client,
    async_db_session,
    authenticated_client,
    self_verifier_mock,
    monkeypatch,
) -> None:
    _patch_webauthn_register(monkeypatch)
    user_id = await _register_tier_0(http_client, "idem@example.com")
    client, _ = authenticated_client(tier=0, user_id=user_id)
    self_verifier_mock.set_response(
        self_verifier_mock.valid_italian_adult_proof(
            user_identifier=user_id, nullifier="self_nullifier_idem"
        )
    )

    first = await client.post("/api/identity/verify-self", json=_proof_request_body())
    assert first.status_code == 200, first.text
    assert first.json()["already_upgraded"] is False
    first_agent_id = first.json()["agent_id"]

    # Second call with the same proof — service short-circuits.
    self_verifier_mock.set_response(
        self_verifier_mock.valid_italian_adult_proof(
            user_identifier=user_id, nullifier="self_nullifier_idem"
        )
    )
    second = await client.post("/api/identity/verify-self", json=_proof_request_body())
    assert second.status_code == 200, second.text
    body = second.json()
    assert body["already_upgraded"] is True
    assert body["tier"] == 1
    assert body["agent_id"] == first_agent_id  # same agent, not a duplicate

    # DB still has exactly one agent for this user.
    agents = (
        await async_db_session.scalars(
            select(Agent).where(Agent.user_id == user_id)
        )
    ).all()
    assert len(agents) == 1


# ---------------------------------------------------------------------------
# 5. Nullifier collision
# ---------------------------------------------------------------------------


@pytest.mark.db
async def test_nullifier_collision_returns_409(
    http_client,
    async_db_session,
    authenticated_client,
    self_verifier_mock,
    monkeypatch,
) -> None:
    _patch_webauthn_register(monkeypatch)
    user_a = await _register_tier_0(http_client, "alice@example.com")
    user_b = await _register_tier_0(http_client, "bob@example.com")
    shared_nullifier = "self_nullifier_shared_doc"

    # User A upgrades — claims the nullifier.
    client_a, _ = authenticated_client(tier=0, user_id=user_a)
    self_verifier_mock.set_response(
        self_verifier_mock.valid_italian_adult_proof(
            user_identifier=user_a, nullifier=shared_nullifier
        )
    )
    a_resp = await client_a.post(
        "/api/identity/verify-self", json=_proof_request_body()
    )
    assert a_resp.status_code == 200, a_resp.text

    # User B tries with the same document → same nullifier from Self → 409.
    client_b, _ = authenticated_client(tier=0, user_id=user_b)
    self_verifier_mock.set_response(
        self_verifier_mock.valid_italian_adult_proof(
            user_identifier=user_b, nullifier=shared_nullifier
        )
    )
    b_resp = await client_b.post(
        "/api/identity/verify-self", json=_proof_request_body()
    )
    assert b_resp.status_code == 409, b_resp.text
    detail = b_resp.json()["detail"]
    assert detail["code"] == "nullifier_collision"
    assert detail["next_step"]["action"] == "login_with_existing_account"

    # User B remains tier 0; no agent for user B.
    user_b_row = await async_db_session.scalar(
        select(User).where(User.id == user_b)
    )
    assert user_b_row.tier == 0
    assert user_b_row.nullifier_hash is None
    user_b_agent = await async_db_session.scalar(
        select(Agent).where(Agent.user_id == user_b)
    )
    assert user_b_agent is None


# ---------------------------------------------------------------------------
# 6. Verifier timeout
# ---------------------------------------------------------------------------


@pytest.mark.db
async def test_verifier_timeout_returns_500(
    http_client,
    async_db_session,
    authenticated_client,
    self_verifier_mock,
    monkeypatch,
) -> None:
    _patch_webauthn_register(monkeypatch)
    user_id = await _register_tier_0(http_client, "timeout@example.com")
    client, _ = authenticated_client(tier=0, user_id=user_id)
    self_verifier_mock.set_error(
        self_verifier_mock.TimeoutException("simulated verifier timeout")
    )

    response = await client.post(
        "/api/identity/verify-self", json=_proof_request_body()
    )
    assert response.status_code == 500, response.text
    assert response.json()["detail"]["code"] == "verifier_unavailable"

    user = await async_db_session.scalar(select(User).where(User.id == user_id))
    assert user.tier == 0
    assert user.nullifier_hash is None
    agent = await async_db_session.scalar(
        select(Agent).where(Agent.user_id == user_id)
    )
    assert agent is None


# ---------------------------------------------------------------------------
# 7. Atomic rollback on KMS failure
# ---------------------------------------------------------------------------


@pytest.mark.db
async def test_atomic_rollback_on_agent_creation_failure(
    http_client,
    async_db_session,
    authenticated_client,
    self_verifier_mock,
    monkeypatch,
) -> None:
    _patch_webauthn_register(monkeypatch)
    user_id = await _register_tier_0(http_client, "kmsfail@example.com")
    client, _ = authenticated_client(tier=0, user_id=user_id)
    self_verifier_mock.set_response(
        self_verifier_mock.valid_italian_adult_proof(user_identifier=user_id)
    )

    # KMS keygen explodes. The proof has already been verified by Self,
    # but no user fields should have been mutated and no agent should exist.
    from app.services.kms import KMSError

    async def _boom(self, db) -> tuple[str, str]:
        raise KMSError("simulated KMS outage")

    monkeypatch.setattr(
        "app.services.kms.local_db_provider.LocalDBProvider.generate_agent_keypair",
        _boom,
    )

    response = await client.post(
        "/api/identity/verify-self", json=_proof_request_body()
    )
    assert response.status_code == 500, response.text
    assert response.json()["detail"]["code"] == "kms_error"

    user = await async_db_session.scalar(select(User).where(User.id == user_id))
    assert user.tier == 0
    assert user.nullifier_hash is None
    agent = await async_db_session.scalar(
        select(Agent).where(Agent.user_id == user_id)
    )
    assert agent is None
