"""Step-up tests (brief task 2.5).

6 tests covering the StepUpRequest lifecycle:
  6. created when an action exceeds a step-up rule (via tool_layer)
  7. signing with a valid WebAuthn assertion → status='approved'
  8. signing with an invalid assertion → 422, status untouched
  9. explicit reject → status='rejected'
 10. expiry sweep marks pending TTL'd rows as 'expired'
 11. resume action: MandateVerifier with step_up_signature in params
     bypasses the step-up gate (returns mandate, no exception)

Tests 6 + 11 use the §5 sync scaffold (`tool_layer.py`, `MandateVerifier`)
and therefore the `db_session` (sync) fixture. The rest use async path.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import select

from app.agents.tool_layer import ToolHandler
from app.core.security import create_access_token
from app.models.schema import StepUpRequest
from app.services import step_up_service
from app.services.mandate_verifier import MandateVerifier
from .factories import (
    fake_assertion_payload,
    setup_active_mandate_async,
    setup_active_mandate_sync,
)


def _bearer(client, user_id: str, tier: int = 2) -> None:
    client.headers["Authorization"] = (
        f"Bearer {create_access_token(user_id=user_id, tier=tier)}"
    )


def _patch_webauthn_ok_sync(monkeypatch, *, new_sign_count: int = 1) -> None:
    """Sync — for tests via the API endpoint (which uses step_up_service)."""
    monkeypatch.setattr(
        "app.services.step_up_service.verify_authentication_response",
        lambda **_: SimpleNamespace(new_sign_count=new_sign_count),
    )


def _patch_webauthn_raise(monkeypatch, message: str) -> None:
    def _raise(**_: Any) -> None:
        raise Exception(message)

    monkeypatch.setattr(
        "app.services.step_up_service.verify_authentication_response", _raise
    )


# ===========================================================================
# 6. step_up_request created when action above threshold
# ===========================================================================


@pytest.mark.db
def test_step_up_request_created_when_action_above_threshold(
    db_session, monkeypatch
) -> None:
    """Tool layer integration: send_offer above €50 threshold creates a row.

    Goes through the §5 sync `ToolHandler.execute` → MandateVerifier
    (raises StepUpRequired) → `_queue_step_up` (creates the row).
    """
    user_id, agent_id, mandate_id = setup_active_mandate_sync(
        db_session,
        email="step_create@example.com",
        step_up_rules=[{"action": "send_offer", "above_eur": 50}],
    )

    # Stub notification_service so we don't try to push without an SDK.
    monkeypatch.setattr(
        "app.services.notification_service.push_step_up_request",
        lambda *args, **kwargs: None,
    )

    handler = ToolHandler(db_session, agent_id)
    result = handler.execute(
        "send_offer",
        {"match_id": "fake-match-id", "price_cents": 10_000},  # €100 > €50
    )

    assert result["status"] == "step_up_required"
    assert result["step_up_id"]
    request = (
        db_session.query(StepUpRequest)
        .filter(StepUpRequest.id == result["step_up_id"])
        .first()
    )
    assert request is not None
    assert request.user_id == user_id
    assert request.agent_id == agent_id
    assert request.mandate_id == mandate_id
    assert request.action == "send_offer"
    assert request.action_params["price_cents"] == 10_000
    assert request.status == "pending"
    assert request.canonical_payload  # non-empty
    assert len(request.challenge) == 32


# ===========================================================================
# 7. sign with valid signature → approved
# ===========================================================================


@pytest.mark.db
async def test_step_up_sign_with_valid_signature_approves(
    http_client, async_db_session, monkeypatch
) -> None:
    user_id, agent_id, mandate_id = await setup_active_mandate_async(
        async_db_session,
        email="step_sign@example.com",
        step_up_rules=[{"action": "send_offer", "above_eur": 50}],
    )
    _bearer(http_client, user_id, tier=2)
    _patch_webauthn_ok_sync(monkeypatch)

    # Insert a pending step-up row directly (skip the tool_layer path).
    import secrets
    import uuid

    from app.core import canonicalization

    challenge = secrets.token_bytes(32)
    payload = {
        "version": "1.0",
        "action": "step_up_approval",
        "step_up_id": str(uuid.uuid4()),
        "principal": {"user_id": user_id, "nullifier_hash": f"nullifier-step_sign@example.com"},
        "agent_id": agent_id,
        "mandate_id": mandate_id,
        "approved_action": {"action_name": "send_offer", "params": {"price_cents": 10_000}},
        "issued_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "challenge": challenge.hex(),
    }
    canonical = canonicalization.canonicalize(payload)
    request = StepUpRequest(
        id=payload["step_up_id"],
        agent_id=agent_id,
        mandate_id=mandate_id,
        user_id=user_id,
        action="send_offer",
        action_params={"price_cents": 10_000},
        reason="Price €100 above threshold €50",
        challenge=challenge,
        canonical_payload=canonical,
        status="pending",
        expires_at=datetime.utcnow() + timedelta(minutes=10),
        created_at=datetime.utcnow(),
    )
    async_db_session.add(request)
    await async_db_session.commit()

    sign_resp = await http_client.post(
        f"/api/step-up/{request.id}/sign",
        json={"webauthn_assertion": fake_assertion_payload()},
    )
    assert sign_resp.status_code == 200, sign_resp.text
    body = sign_resp.json()
    assert body["status"] == "approved"
    assert body["step_up_id"] == request.id

    # The API session updated the row on the shared connection. Force the
    # test session to refresh from DB instead of returning the identity-map
    # cached version (which still has status='pending').
    row = await async_db_session.scalar(
        select(StepUpRequest)
        .where(StepUpRequest.id == request.id)
        .execution_options(populate_existing=True)
    )
    assert row.status == "approved"
    assert row.resolved_at is not None
    assert row.signature["algorithm"] == "webauthn"


# ===========================================================================
# 8. sign with invalid signature → 422
# ===========================================================================


@pytest.mark.db
async def test_step_up_sign_with_invalid_signature_fails(
    http_client, async_db_session, monkeypatch
) -> None:
    import secrets
    import uuid

    user_id, agent_id, mandate_id = await setup_active_mandate_async(
        async_db_session,
        email="step_badsig@example.com",
        step_up_rules=[{"action": "send_offer", "above_eur": 50}],
    )
    _bearer(http_client, user_id, tier=2)

    request = StepUpRequest(
        id=str(uuid.uuid4()),
        agent_id=agent_id,
        mandate_id=mandate_id,
        user_id=user_id,
        action="send_offer",
        action_params={"price_cents": 10_000},
        reason="Price above threshold",
        challenge=secrets.token_bytes(32),
        canonical_payload=b'{"version":"1.0"}',
        status="pending",
        expires_at=datetime.utcnow() + timedelta(minutes=10),
        created_at=datetime.utcnow(),
    )
    async_db_session.add(request)
    await async_db_session.commit()

    _patch_webauthn_raise(monkeypatch, "signature mismatch")
    resp = await http_client.post(
        f"/api/step-up/{request.id}/sign",
        json={"webauthn_assertion": fake_assertion_payload()},
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "step_up_verification_failed"

    row = await async_db_session.scalar(
        select(StepUpRequest).where(StepUpRequest.id == request.id)
    )
    assert row.status == "pending"  # untouched
    assert row.resolved_at is None


# ===========================================================================
# 9. reject marks as rejected
# ===========================================================================


@pytest.mark.db
async def test_step_up_reject_marks_as_rejected_and_cancels_action(
    http_client, async_db_session
) -> None:
    import secrets
    import uuid

    user_id, agent_id, mandate_id = await setup_active_mandate_async(
        async_db_session,
        email="step_reject@example.com",
        step_up_rules=[{"action": "send_offer", "above_eur": 50}],
    )
    _bearer(http_client, user_id, tier=2)

    request = StepUpRequest(
        id=str(uuid.uuid4()),
        agent_id=agent_id,
        mandate_id=mandate_id,
        user_id=user_id,
        action="send_offer",
        action_params={"price_cents": 10_000},
        reason="Price above threshold",
        challenge=secrets.token_bytes(32),
        canonical_payload=b'{"version":"1.0"}',
        status="pending",
        expires_at=datetime.utcnow() + timedelta(minutes=10),
        created_at=datetime.utcnow(),
    )
    async_db_session.add(request)
    await async_db_session.commit()

    resp = await http_client.post(f"/api/step-up/{request.id}/reject")
    assert resp.status_code == 200
    assert resp.json()["status"] == "rejected"

    row = await async_db_session.scalar(
        select(StepUpRequest)
        .where(StepUpRequest.id == request.id)
        .execution_options(populate_existing=True)
    )
    assert row.status == "rejected"
    assert row.resolved_at is not None


# ===========================================================================
# 10. expiry sweep marks TTL'd pending requests as 'expired'
# ===========================================================================


@pytest.mark.db
async def test_step_up_expired_after_ttl(async_db_session) -> None:
    import secrets
    import uuid

    user_id, agent_id, mandate_id = await setup_active_mandate_async(
        async_db_session,
        email="step_expire@example.com",
        step_up_rules=[{"action": "send_offer", "above_eur": 50}],
    )

    # Two pending requests: one expired, one fresh.
    expired_request = StepUpRequest(
        id=str(uuid.uuid4()),
        agent_id=agent_id,
        mandate_id=mandate_id,
        user_id=user_id,
        action="send_offer",
        action_params={"price_cents": 10_000},
        reason="Price above threshold",
        challenge=secrets.token_bytes(32),
        canonical_payload=b'{"version":"1.0"}',
        status="pending",
        expires_at=datetime.utcnow() - timedelta(seconds=1),  # expired
        created_at=datetime.utcnow() - timedelta(minutes=15),
    )
    fresh_request = StepUpRequest(
        id=str(uuid.uuid4()),
        agent_id=agent_id,
        mandate_id=mandate_id,
        user_id=user_id,
        action="send_offer",
        action_params={"price_cents": 12_000},
        reason="Price above threshold",
        challenge=secrets.token_bytes(32),
        canonical_payload=b'{"version":"1.0"}',
        status="pending",
        expires_at=datetime.utcnow() + timedelta(minutes=10),
        created_at=datetime.utcnow(),
    )
    async_db_session.add(expired_request)
    async_db_session.add(fresh_request)
    await async_db_session.commit()

    swept = await step_up_service.mark_expired(async_db_session)
    assert swept == 1

    expired_row = await async_db_session.scalar(
        select(StepUpRequest).where(StepUpRequest.id == expired_request.id)
    )
    assert expired_row.status == "expired"
    assert expired_row.resolved_at is not None

    fresh_row = await async_db_session.scalar(
        select(StepUpRequest).where(StepUpRequest.id == fresh_request.id)
    )
    assert fresh_row.status == "pending"
    assert fresh_row.resolved_at is None


# ===========================================================================
# 11. resume action: MandateVerifier bypasses step-up when signature attached
# ===========================================================================


@pytest.mark.db
def test_step_up_resume_action_with_approved_signature(db_session) -> None:
    """The original action passes MandateVerifier when re-tried with `step_up_signature`.

    Mirrors what tool_layer does on its next tick: agent re-invokes the
    blocked tool with the signature blob from the approved step_up_request.
    MandateVerifier sees a truthy `step_up_signature` and skips the gate.
    """
    user_id, agent_id, mandate_id = setup_active_mandate_sync(
        db_session,
        email="step_resume@example.com",
        step_up_rules=[{"action": "send_offer", "above_eur": 50}],
    )

    verifier = MandateVerifier(db_session)

    # Without signature → StepUpRequired raised
    from app.services.mandate_verifier import StepUpRequired

    with pytest.raises(StepUpRequired):
        verifier.authorize(
            agent_id, "send_offer",
            {"match_id": "fake", "price_cents": 10_000},  # €100 > €50
        )

    # With signature attached → mandate returned, no exception
    mandate = verifier.authorize(
        agent_id,
        "send_offer",
        {
            "match_id": "fake",
            "price_cents": 10_000,
            "step_up_signature": {"algorithm": "webauthn", "signature": "blob"},
        },
    )
    assert mandate.id == mandate_id
