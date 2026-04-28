"""Mandate creation + signing tests (brief task 2.4).

12 tests covering the security-critical flow of `/api/mandates/draft`
+ `/api/mandates/submit`:

  1. draft creation with default limits             → 200 + payload + summary
  2. draft rejects limits above platform caps       → 422 limits_exceed
  3. draft rejects invalid geo_scope                → 422 invalid_geo_scope
  4. submit with valid signature activates agent    → 200 + tier=2 + agent.active
  5. submit with invalid signature                  → 422 webauthn_verification_failed
  6. submit with expired draft                      → 410 draft_expired
  7. submit with consumed draft (replay)            → 409 draft_already_consumed
  8. submit when user already tier 2                → 409 invalid_tier_transition
  9. submit returns new access_token (tier=2)      → JWT decodes with tier=2
 10. canonicalization deterministic                 → byte-identical for same input
 11. webauthn replay protection                     → assertion-side raise → 422
 12. audit_service.log_mandate_signed called        → recorded post-commit

The Self verifier and WebAuthn boundary functions are mocked. The mandate
service runs against real Postgres + JCS canonicalization.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import select

from app.core import canonicalization
from app.core.security import create_access_token, decode_access_token
from app.models.schema import Agent, Mandate, MandateDraft, User
from app.services.auth_service import _b64url


# ---------------------------------------------------------------------------
# Helpers — direct DB setup avoids re-running the full register + verify-self
# pipeline for every test, while still hitting the real schema/constraints.
# ---------------------------------------------------------------------------


def _fake_credential_id_bytes() -> bytes:
    return b"mock-credential-id-bytes-for-mandate-tests"


def _fake_pubkey_bytes() -> bytes:
    return b"mock-cose-encoded-pubkey-bytes"


async def _create_tier_1_user_and_agent(
    db_session,
    *,
    email: str,
    user_id: str | None = None,
    agent_id: str | None = None,
) -> tuple[str, str]:
    """Insert a tier=1 User + a pending_mandate Agent. Returns (user_id, agent_id)."""
    import uuid

    user_id = user_id or str(uuid.uuid4())
    agent_id = agent_id or str(uuid.uuid4())
    now = datetime.utcnow()

    user = User(
        id=user_id,
        tier=1,
        nullifier_hash=f"nullifier-{user_id}",
        passkey_credential_id=_b64url(_fake_credential_id_bytes()),
        passkey_pubkey=_b64url(_fake_pubkey_bytes()),
        passkey_sign_count=0,
        notification_email=email,
        status="active",
        created_at=now,
        last_active_at=now,
        attributes_proven={
            "isAdult": True,
            "issuingState": "IT",
            "documentValid": True,
            "documentExpiry": "2030-04-15",
        },
        attributes_verified_at=now,
        attributes_expires_at=now + timedelta(days=365 * 5),
    )
    db_session.add(user)

    agent = Agent(
        id=agent_id,
        user_id=user_id,
        pubkey="mock-agent-pubkey-b64",
        privkey_kms_ref="file:.secrets/agent_keys/mock.json",
        status="pending_mandate",
        created_at=now,
    )
    db_session.add(agent)
    await db_session.commit()
    return user_id, agent_id


def _patch_webauthn_authenticate_ok(monkeypatch, *, new_sign_count: int = 1) -> None:
    monkeypatch.setattr(
        "app.services.mandate_service.verify_authentication_response",
        lambda **_: SimpleNamespace(new_sign_count=new_sign_count),
    )


def _patch_webauthn_authenticate_raise(monkeypatch, message: str) -> None:
    def _raise(**_: Any) -> None:
        raise Exception(message)

    monkeypatch.setattr(
        "app.services.mandate_service.verify_authentication_response", _raise
    )


def _fake_assertion_payload() -> dict[str, Any]:
    return {
        "id": _b64url(_fake_credential_id_bytes()),
        "rawId": _b64url(_fake_credential_id_bytes()),
        "type": "public-key",
        "response": {
            "authenticatorData": "mock-auth-data",
            "clientDataJSON": "mock-client-data",
            "signature": "mock-signature",
            "userHandle": "mock-user-handle",
        },
    }


def _bearer(client, user_id: str, tier: int) -> None:
    """Inject Authorization header on the shared http_client."""
    client.headers["Authorization"] = f"Bearer {create_access_token(user_id=user_id, tier=tier)}"


# ===========================================================================
# 1. draft creation with default limits
# ===========================================================================


@pytest.mark.db
async def test_draft_creation_with_default_limits(
    http_client, async_db_session
) -> None:
    user_id, agent_id = await _create_tier_1_user_and_agent(
        async_db_session, email="draft1@example.com"
    )
    _bearer(http_client, user_id, tier=1)

    response = await http_client.post(
        "/api/mandates/draft",
        json={"agent_id": agent_id},
    )
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["draft_id"]
    assert body["challenge"]  # b64url
    payload = body["payload"]
    assert payload["version"] == "1.0"
    assert payload["principal"]["user_id"] == user_id
    assert payload["principal"]["tier"] == 1
    assert payload["agent"]["agent_id"] == agent_id
    assert payload["limits"]["max_price_per_deal_eur"] == 100  # V0 default
    assert payload["limits"]["max_total_volume_eur_per_mandate"] == 500
    assert payload["limits"]["max_deals_per_day"] == 3
    assert payload["constraints"]["geo_scope"] == ["IT"]
    # 9 actions in V0_DEFAULT_ALLOWED_ACTIONS
    assert len(payload["scope"]["allowed_actions"]) == 9
    assert "send_offer" in payload["scope"]["allowed_actions"]
    assert "delete_account" in payload["scope"]["forbidden_actions"]

    # Italian summary present
    summary = body["payload_summary"]
    assert "€100" in summary["human_readable"]
    assert "€500" in summary["human_readable"]
    assert any(f["label"] == "Geo" and f["value"] == "Italia" for f in summary["key_fields"])

    # Draft row persisted
    draft = await async_db_session.scalar(
        select(MandateDraft).where(MandateDraft.id == body["draft_id"])
    )
    assert draft is not None
    assert draft.consumed is False
    assert len(draft.canonical_payload) > 100
    assert len(draft.challenge) == 32


# ===========================================================================
# 2. draft rejects limits above platform caps
# ===========================================================================


@pytest.mark.db
async def test_draft_rejects_limits_above_platform_caps(
    http_client, async_db_session
) -> None:
    user_id, agent_id = await _create_tier_1_user_and_agent(
        async_db_session, email="cap@example.com"
    )
    _bearer(http_client, user_id, tier=1)

    response = await http_client.post(
        "/api/mandates/draft",
        json={
            "agent_id": agent_id,
            "limits": {"max_price_per_deal_eur": 2000},  # > 1000 cap
        },
    )
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["code"] == "limits_exceed_platform_cap"
    assert "max_price_per_deal_eur" in detail["message"]


# ===========================================================================
# 3. draft rejects invalid geo_scope
# ===========================================================================


@pytest.mark.db
async def test_draft_rejects_invalid_geo_scope(
    http_client, async_db_session
) -> None:
    user_id, agent_id = await _create_tier_1_user_and_agent(
        async_db_session, email="geo@example.com"
    )
    _bearer(http_client, user_id, tier=1)

    response = await http_client.post(
        "/api/mandates/draft",
        json={
            "agent_id": agent_id,
            "constraints": {"geo_scope": ["FR"]},
        },
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "invalid_geo_scope"


# ===========================================================================
# 4. submit with valid signature activates agent
# ===========================================================================


@pytest.mark.db
async def test_submit_with_valid_signature_activates_agent(
    http_client, async_db_session, monkeypatch
) -> None:
    user_id, agent_id = await _create_tier_1_user_and_agent(
        async_db_session, email="submit_ok@example.com"
    )
    _bearer(http_client, user_id, tier=1)
    _patch_webauthn_authenticate_ok(monkeypatch, new_sign_count=1)

    draft_resp = await http_client.post(
        "/api/mandates/draft", json={"agent_id": agent_id}
    )
    assert draft_resp.status_code == 200
    draft_id = draft_resp.json()["draft_id"]

    submit_resp = await http_client.post(
        "/api/mandates/submit",
        json={
            "draft_id": draft_id,
            "webauthn_assertion": _fake_assertion_payload(),
        },
    )
    assert submit_resp.status_code == 200, submit_resp.text
    body = submit_resp.json()
    assert body["mandate_id"]
    assert body["agent_id"] == agent_id
    assert body["agent_status"] == "active"
    assert body["new_access_token"]
    assert body["next_step"]["action"] == "create_first_intent"

    # DB-side: mandate persisted, agent.active, user.tier=2
    mandate = await async_db_session.scalar(
        select(Mandate).where(Mandate.id == body["mandate_id"])
    )
    assert mandate is not None
    assert mandate.user_id == user_id
    assert mandate.agent_id == agent_id
    assert mandate.signature["algorithm"] == "webauthn"
    assert mandate.canonical_payload  # text, the bytes that were signed
    assert mandate.scope["allowed_actions"]
    assert mandate.limits["max_price_per_deal_eur"] == 100

    agent = await async_db_session.scalar(select(Agent).where(Agent.id == agent_id))
    assert agent.status == "active"

    user = await async_db_session.scalar(select(User).where(User.id == user_id))
    assert user.tier == 2
    assert user.passkey_sign_count == 1  # bumped from 0

    draft = await async_db_session.scalar(
        select(MandateDraft).where(MandateDraft.id == draft_id)
    )
    assert draft.consumed is True


# ===========================================================================
# 5. submit with invalid signature
# ===========================================================================


@pytest.mark.db
async def test_submit_with_invalid_signature_fails(
    http_client, async_db_session, monkeypatch
) -> None:
    user_id, agent_id = await _create_tier_1_user_and_agent(
        async_db_session, email="badsig@example.com"
    )
    _bearer(http_client, user_id, tier=1)

    # First create a valid draft (verify still passes here for /draft).
    _patch_webauthn_authenticate_ok(monkeypatch)
    draft_resp = await http_client.post(
        "/api/mandates/draft", json={"agent_id": agent_id}
    )
    draft_id = draft_resp.json()["draft_id"]

    # Now make WebAuthn fail on the submit.
    _patch_webauthn_authenticate_raise(monkeypatch, "signature mismatch")

    submit_resp = await http_client.post(
        "/api/mandates/submit",
        json={
            "draft_id": draft_id,
            "webauthn_assertion": _fake_assertion_payload(),
        },
    )
    assert submit_resp.status_code == 422
    assert submit_resp.json()["detail"]["code"] == "webauthn_verification_failed"

    # Nothing committed: no mandate, agent still pending, user still tier=1, draft not consumed.
    mandates = (await async_db_session.scalars(select(Mandate))).all()
    assert mandates == []
    agent = await async_db_session.scalar(select(Agent).where(Agent.id == agent_id))
    assert agent.status == "pending_mandate"
    user = await async_db_session.scalar(select(User).where(User.id == user_id))
    assert user.tier == 1
    draft = await async_db_session.scalar(
        select(MandateDraft).where(MandateDraft.id == draft_id)
    )
    assert draft.consumed is False


# ===========================================================================
# 6. submit with expired draft
# ===========================================================================


@pytest.mark.db
async def test_submit_with_expired_draft_fails(
    http_client, async_db_session, monkeypatch
) -> None:
    user_id, agent_id = await _create_tier_1_user_and_agent(
        async_db_session, email="expired@example.com"
    )
    _bearer(http_client, user_id, tier=1)
    _patch_webauthn_authenticate_ok(monkeypatch)

    draft_resp = await http_client.post(
        "/api/mandates/draft", json={"agent_id": agent_id}
    )
    draft_id = draft_resp.json()["draft_id"]

    # Expire the draft directly in DB.
    draft = await async_db_session.scalar(
        select(MandateDraft).where(MandateDraft.id == draft_id)
    )
    draft.expires_at = datetime.utcnow() - timedelta(seconds=1)
    await async_db_session.commit()

    submit_resp = await http_client.post(
        "/api/mandates/submit",
        json={
            "draft_id": draft_id,
            "webauthn_assertion": _fake_assertion_payload(),
        },
    )
    assert submit_resp.status_code == 410
    assert submit_resp.json()["detail"]["code"] == "draft_expired"


# ===========================================================================
# 7. submit with already-consumed draft (replay)
# ===========================================================================


@pytest.mark.db
async def test_submit_with_consumed_draft_fails(
    http_client, async_db_session, monkeypatch
) -> None:
    user_id, agent_id = await _create_tier_1_user_and_agent(
        async_db_session, email="replay@example.com"
    )
    _bearer(http_client, user_id, tier=1)
    _patch_webauthn_authenticate_ok(monkeypatch)

    draft_resp = await http_client.post(
        "/api/mandates/draft", json={"agent_id": agent_id}
    )
    draft_id = draft_resp.json()["draft_id"]

    # First submit — succeeds.
    first = await http_client.post(
        "/api/mandates/submit",
        json={
            "draft_id": draft_id,
            "webauthn_assertion": _fake_assertion_payload(),
        },
    )
    assert first.status_code == 200, first.text

    # Second submit on same draft — replay → 409 draft_already_consumed.
    second = await http_client.post(
        "/api/mandates/submit",
        json={
            "draft_id": draft_id,
            "webauthn_assertion": _fake_assertion_payload(),
        },
    )
    # The draft is consumed; user is also tier=2 now. The draft check
    # fires first in the service, so we expect draft_already_consumed.
    assert second.status_code == 409
    assert second.json()["detail"]["code"] == "draft_already_consumed"


# ===========================================================================
# 8. submit when user already tier 2 (separate draft path)
# ===========================================================================


@pytest.mark.db
async def test_submit_idempotent_for_already_tier_2(
    http_client, async_db_session, monkeypatch
) -> None:
    """Even with a fresh, valid draft row, a tier-2 user can't sign another mandate.

    Construct the state directly: tier-2 user + a manually-inserted
    unconsumed draft. Submit must reject with invalid_tier_transition (409).
    """
    import uuid

    user_id, agent_id = await _create_tier_1_user_and_agent(
        async_db_session, email="alreadytier2@example.com"
    )
    # Bump to tier 2 directly.
    user = await async_db_session.scalar(select(User).where(User.id == user_id))
    user.tier = 2
    await async_db_session.commit()

    _bearer(http_client, user_id, tier=2)
    _patch_webauthn_authenticate_ok(monkeypatch)

    # Insert a draft row directly (bypassing /draft which would also reject).
    draft_id = str(uuid.uuid4())
    fake_canonical = b'{"version":"1.0"}'
    fake_challenge = b"\x00" * 32
    draft = MandateDraft(
        id=draft_id,
        user_id=user_id,
        agent_id=agent_id,
        canonical_payload=fake_canonical,
        challenge=fake_challenge,
        expires_at=datetime.utcnow() + timedelta(minutes=5),
        consumed=False,
        created_at=datetime.utcnow(),
    )
    async_db_session.add(draft)
    await async_db_session.commit()

    submit_resp = await http_client.post(
        "/api/mandates/submit",
        json={
            "draft_id": draft_id,
            "webauthn_assertion": _fake_assertion_payload(),
        },
    )
    assert submit_resp.status_code == 409
    assert submit_resp.json()["detail"]["code"] == "invalid_tier_transition"


# ===========================================================================
# 9. submit returns new access_token with tier=2
# ===========================================================================


@pytest.mark.db
async def test_submit_returns_new_access_token_with_tier_2(
    http_client, async_db_session, monkeypatch
) -> None:
    user_id, agent_id = await _create_tier_1_user_and_agent(
        async_db_session, email="tokentier2@example.com"
    )
    _bearer(http_client, user_id, tier=1)
    _patch_webauthn_authenticate_ok(monkeypatch)

    draft_resp = await http_client.post(
        "/api/mandates/draft", json={"agent_id": agent_id}
    )
    draft_id = draft_resp.json()["draft_id"]

    submit_resp = await http_client.post(
        "/api/mandates/submit",
        json={
            "draft_id": draft_id,
            "webauthn_assertion": _fake_assertion_payload(),
        },
    )
    body = submit_resp.json()
    assert body["token_type"] == "bearer"
    decoded = decode_access_token(body["new_access_token"])
    assert decoded["sub"] == user_id
    assert decoded["tier"] == 2
    assert decoded["kind"] == "access"


# ===========================================================================
# 10. canonicalization deterministic
# ===========================================================================


def test_canonicalization_deterministic() -> None:
    """RFC 8785 invariant: same input dict ⇒ byte-identical output, twice."""
    payload = {
        "b": 2,
        "a": 1,
        "c": [3, 1, 2],
        "nested": {"z": "last", "a": "first"},
        "challenge": "abc123",
    }
    bytes1 = canonicalization.canonicalize(payload)
    bytes2 = canonicalization.canonicalize(payload)
    assert bytes1 == bytes2
    # Lex-sorted keys, no whitespace.
    assert bytes1 == b'{"a":1,"b":2,"c":[3,1,2],"challenge":"abc123","nested":{"a":"first","z":"last"}}'


# ===========================================================================
# 11. webauthn replay protection (simulated)
# ===========================================================================


@pytest.mark.db
async def test_webauthn_replay_protection(
    http_client, async_db_session, monkeypatch
) -> None:
    """An assertion with a stale sign_count is rejected by py-webauthn.

    We can't construct a real authenticator in tests, so we simulate the
    rejection at the boundary — the same code path as a corrupted
    signature, but the test docstring records intent.
    """
    user_id, agent_id = await _create_tier_1_user_and_agent(
        async_db_session, email="replay_sc@example.com"
    )
    _bearer(http_client, user_id, tier=1)

    _patch_webauthn_authenticate_ok(monkeypatch)
    draft_resp = await http_client.post(
        "/api/mandates/draft", json={"agent_id": agent_id}
    )
    draft_id = draft_resp.json()["draft_id"]

    # Stash the current sign_count — replay would reuse it.
    user = await async_db_session.scalar(select(User).where(User.id == user_id))
    assert user.passkey_sign_count == 0

    # Simulate the library raising "sign_count not incremented".
    _patch_webauthn_authenticate_raise(
        monkeypatch, "sign_count did not increment (replay attempt)"
    )

    submit_resp = await http_client.post(
        "/api/mandates/submit",
        json={
            "draft_id": draft_id,
            "webauthn_assertion": _fake_assertion_payload(),
        },
    )
    assert submit_resp.status_code == 422
    assert submit_resp.json()["detail"]["code"] == "webauthn_verification_failed"

    # State unchanged.
    user_after = await async_db_session.scalar(
        select(User).where(User.id == user_id)
    )
    assert user_after.tier == 1
    assert user_after.passkey_sign_count == 0


# ===========================================================================
# 12. audit_service.log_mandate_signed called post-commit
# ===========================================================================


@pytest.mark.db
async def test_audit_log_records_mandate_signed(
    http_client, async_db_session, monkeypatch
) -> None:
    user_id, agent_id = await _create_tier_1_user_and_agent(
        async_db_session, email="audit@example.com"
    )
    _bearer(http_client, user_id, tier=1)
    _patch_webauthn_authenticate_ok(monkeypatch)

    captured: list[dict[str, Any]] = []

    async def _spy(**kwargs: Any) -> None:
        captured.append(kwargs)

    monkeypatch.setattr(
        "app.services.mandate_service.audit_service.log_mandate_signed", _spy
    )

    draft_resp = await http_client.post(
        "/api/mandates/draft", json={"agent_id": agent_id}
    )
    draft_id = draft_resp.json()["draft_id"]

    submit_resp = await http_client.post(
        "/api/mandates/submit",
        json={
            "draft_id": draft_id,
            "webauthn_assertion": _fake_assertion_payload(),
        },
    )
    assert submit_resp.status_code == 200, submit_resp.text

    assert len(captured) == 1
    rec = captured[0]
    assert rec["user_id"] == user_id
    assert rec["agent_id"] == agent_id
    assert rec["mandate_id"] == submit_resp.json()["mandate_id"]
