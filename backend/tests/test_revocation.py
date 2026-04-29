"""Mandate revocation tests (brief task 2.5).

5 tests covering the WebAuthn-signed `/revoke/draft` + `/revoke/submit`
flow plus the cascade of side-effects on the agent's active state.
"""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import select

from app.core.security import create_access_token
from app.models.schema import (
    Agent,
    Deal,
    Intent,
    Mandate,
    MandateRevocationDraft,
    Match,
    Negotiation,
    StepUpRequest,
    User,
)
from .factories import (
    fake_assertion_payload,
    setup_active_mandate_async,
)


def _bearer(client, user_id: str, tier: int = 2) -> None:
    client.headers["Authorization"] = (
        f"Bearer {create_access_token(user_id=user_id, tier=tier)}"
    )


def _patch_webauthn_ok(monkeypatch, *, new_sign_count: int = 1) -> None:
    monkeypatch.setattr(
        "app.services.mandate_revocation_service.verify_authentication_response",
        lambda **_: SimpleNamespace(new_sign_count=new_sign_count),
    )


def _patch_webauthn_raise(monkeypatch, message: str) -> None:
    def _raise(**_: Any) -> None:
        raise Exception(message)

    monkeypatch.setattr(
        "app.services.mandate_revocation_service.verify_authentication_response",
        _raise,
    )


# ===========================================================================
# 1. Revoke with valid signature succeeds
# ===========================================================================


@pytest.mark.db
async def test_revoke_mandate_with_valid_signature_succeeds(
    http_client, async_db_session, monkeypatch
) -> None:
    user_id, agent_id, mandate_id = await setup_active_mandate_async(
        async_db_session, email="rev_ok@example.com"
    )
    _bearer(http_client, user_id, tier=2)
    _patch_webauthn_ok(monkeypatch)

    # /draft
    draft_resp = await http_client.post(
        f"/api/mandates/{mandate_id}/revoke/draft",
        json={"reason": "user_requested"},
    )
    assert draft_resp.status_code == 200, draft_resp.text
    draft_body = draft_resp.json()
    assert draft_body["already_revoked"] is False
    assert draft_body["revocation_draft_id"]
    assert draft_body["payload"]["action"] == "revoke_mandate"
    assert draft_body["payload"]["reason"] == "user_requested"
    assert draft_body["payload"]["mandate_id"] == mandate_id

    # /submit
    submit_resp = await http_client.post(
        f"/api/mandates/{mandate_id}/revoke/submit",
        json={
            "revocation_draft_id": draft_body["revocation_draft_id"],
            "webauthn_assertion": fake_assertion_payload(),
        },
    )
    assert submit_resp.status_code == 200, submit_resp.text
    body = submit_resp.json()
    assert body["revoked"] is True
    assert body["already_revoked"] is False
    assert body["mandate_id"] == mandate_id
    assert body["agent_id"] == agent_id
    assert body["agent_status"] == "revoked"

    mandate = await async_db_session.scalar(
        select(Mandate).where(Mandate.id == mandate_id)
    )
    assert mandate.revoked_at is not None
    assert mandate.revocation_reason == "user_requested"
    agent = await async_db_session.scalar(select(Agent).where(Agent.id == agent_id))
    assert agent.status == "revoked"


# ===========================================================================
# 2. Revoke with invalid signature fails
# ===========================================================================


@pytest.mark.db
async def test_revoke_with_invalid_signature_fails(
    http_client, async_db_session, monkeypatch
) -> None:
    user_id, agent_id, mandate_id = await setup_active_mandate_async(
        async_db_session, email="rev_badsig@example.com"
    )
    _bearer(http_client, user_id, tier=2)

    # /draft is OK (no WebAuthn check yet)
    _patch_webauthn_ok(monkeypatch)
    draft_resp = await http_client.post(
        f"/api/mandates/{mandate_id}/revoke/draft",
        json={"reason": "user_requested"},
    )
    assert draft_resp.status_code == 200
    draft_id = draft_resp.json()["revocation_draft_id"]

    # /submit fails the signature check
    _patch_webauthn_raise(monkeypatch, "signature mismatch")
    submit_resp = await http_client.post(
        f"/api/mandates/{mandate_id}/revoke/submit",
        json={
            "revocation_draft_id": draft_id,
            "webauthn_assertion": fake_assertion_payload(),
        },
    )
    assert submit_resp.status_code == 422
    assert submit_resp.json()["detail"]["code"] == "revocation_verification_failed"

    mandate = await async_db_session.scalar(
        select(Mandate).where(Mandate.id == mandate_id)
    )
    assert mandate.revoked_at is None  # untouched
    agent = await async_db_session.scalar(select(Agent).where(Agent.id == agent_id))
    assert agent.status == "active"


# ===========================================================================
# 3. Revoke is idempotent on already-revoked mandate
# ===========================================================================


@pytest.mark.db
async def test_revoke_already_revoked_is_idempotent(
    http_client, async_db_session, monkeypatch
) -> None:
    user_id, agent_id, mandate_id = await setup_active_mandate_async(
        async_db_session, email="rev_idem@example.com"
    )
    _bearer(http_client, user_id, tier=2)
    _patch_webauthn_ok(monkeypatch)

    # First revoke
    draft1 = await http_client.post(
        f"/api/mandates/{mandate_id}/revoke/draft",
        json={"reason": "user_requested"},
    )
    assert draft1.status_code == 200
    submit1 = await http_client.post(
        f"/api/mandates/{mandate_id}/revoke/submit",
        json={
            "revocation_draft_id": draft1.json()["revocation_draft_id"],
            "webauthn_assertion": fake_assertion_payload(),
        },
    )
    assert submit1.status_code == 200
    first_revoked_at = submit1.json()["revoked_at"]

    # Second /draft: returns already_revoked=true with no draft body.
    draft2 = await http_client.post(
        f"/api/mandates/{mandate_id}/revoke/draft",
        json={"reason": "user_requested"},
    )
    assert draft2.status_code == 200
    body2 = draft2.json()
    assert body2["already_revoked"] is True
    assert body2["revocation_draft_id"] is None
    assert body2["payload"] == {}

    # State unchanged: revoked_at still equals the first commit timestamp.
    mandate = await async_db_session.scalar(
        select(Mandate).where(Mandate.id == mandate_id)
    )
    # Timestamps are naive UTC; compare via isoformat to dodge Pydantic
    # formatting differences.
    assert mandate.revoked_at.isoformat()[:19] == first_revoked_at[:19]


# ===========================================================================
# 4. Revoke cancels active negotiations + pending deals + pauses intents
# ===========================================================================


@pytest.mark.db
async def test_revoke_cancels_active_negotiations_and_pending_deals(
    http_client, async_db_session, monkeypatch
) -> None:
    import uuid

    user_id, agent_id, mandate_id = await setup_active_mandate_async(
        async_db_session, email="rev_cascade@example.com"
    )
    _bearer(http_client, user_id, tier=2)
    _patch_webauthn_ok(monkeypatch)

    # Counterparty user + agent (so a negotiation has both sides)
    cp_user_id, cp_agent_id, _ = await setup_active_mandate_async(
        async_db_session, email="rev_cp@example.com"
    )

    # Two intents, one for each agent — buy by us, sell by counterparty.
    buy_intent = Intent(
        id=str(uuid.uuid4()),
        user_id=user_id,
        agent_id=agent_id,
        side="buy",
        title="laptop",
        category="electronics",
        reservation_price_cents=200_000,
        ideal_price_cents=150_000,
        status="active",
        expires_at=datetime.utcnow().replace(year=2027),
    )
    sell_intent = Intent(
        id=str(uuid.uuid4()),
        user_id=cp_user_id,
        agent_id=cp_agent_id,
        side="sell",
        title="laptop",
        category="electronics",
        reservation_price_cents=140_000,
        ideal_price_cents=180_000,
        status="active",
        expires_at=datetime.utcnow().replace(year=2027),
    )
    async_db_session.add(buy_intent)
    async_db_session.add(sell_intent)
    await async_db_session.flush()

    match = Match(
        id=str(uuid.uuid4()),
        buy_intent_id=buy_intent.id,
        sell_intent_id=sell_intent.id,
        status="negotiating",
    )
    async_db_session.add(match)
    await async_db_session.flush()

    nego = Negotiation(
        id=str(uuid.uuid4()),
        match_id=match.id,
        state=[],
        rounds_used=1,
        max_rounds=6,
        status="active",
    )
    async_db_session.add(nego)
    await async_db_session.flush()  # FK target before children
    pending_deal = Deal(
        id=str(uuid.uuid4()),
        negotiation_id=nego.id,
        buyer_user_id=user_id,
        seller_user_id=cp_user_id,
        buy_intent_id=buy_intent.id,
        sell_intent_id=sell_intent.id,
        agreed_price_cents=160_000,
        status="pending_signatures",
        idempotency_key=str(uuid.uuid4()),
    )
    confirmed_deal = Deal(
        id=str(uuid.uuid4()),
        negotiation_id=nego.id,
        buyer_user_id=user_id,
        seller_user_id=cp_user_id,
        buy_intent_id=buy_intent.id,
        sell_intent_id=sell_intent.id,
        agreed_price_cents=170_000,
        status="confirmed",
        idempotency_key=str(uuid.uuid4()),
    )
    async_db_session.add(pending_deal)
    async_db_session.add(confirmed_deal)
    await async_db_session.commit()

    # Revoke
    draft_resp = await http_client.post(
        f"/api/mandates/{mandate_id}/revoke/draft",
        json={"reason": "lost_device"},
    )
    submit_resp = await http_client.post(
        f"/api/mandates/{mandate_id}/revoke/submit",
        json={
            "revocation_draft_id": draft_resp.json()["revocation_draft_id"],
            "webauthn_assertion": fake_assertion_payload(),
        },
    )
    assert submit_resp.status_code == 200, submit_resp.text
    counts = submit_resp.json()["cancellations"]
    assert counts["negotiations_cancelled"] == 1
    assert counts["deals_cancelled"] == 1  # pending only
    assert counts["intents_paused"] == 1  # the buy intent is ours

    # DB-side checks: force re-read from DB (the API session updated rows
    # on the shared connection; identity-map cache would otherwise return
    # the pre-revocation state).
    nego_row = await async_db_session.scalar(
        select(Negotiation)
        .where(Negotiation.id == nego.id)
        .execution_options(populate_existing=True)
    )
    assert nego_row.status == "cancelled_revoked"
    assert nego_row.closed_at is not None

    pending_row = await async_db_session.scalar(
        select(Deal)
        .where(Deal.id == pending_deal.id)
        .execution_options(populate_existing=True)
    )
    assert pending_row.status == "cancelled_revoked"

    buy_row = await async_db_session.scalar(
        select(Intent)
        .where(Intent.id == buy_intent.id)
        .execution_options(populate_existing=True)
    )
    assert buy_row.status == "paused"


# ===========================================================================
# 5. Revoke does not affect confirmed deals
# ===========================================================================


@pytest.mark.db
async def test_revoke_does_not_affect_confirmed_deals(
    http_client, async_db_session, monkeypatch
) -> None:
    """Subset of test 4 sliced for clarity: confirmed deals must survive."""
    import uuid

    user_id, agent_id, mandate_id = await setup_active_mandate_async(
        async_db_session, email="rev_keep_deals@example.com"
    )
    cp_user_id, cp_agent_id, _ = await setup_active_mandate_async(
        async_db_session, email="rev_keep_cp@example.com"
    )
    _bearer(http_client, user_id, tier=2)
    _patch_webauthn_ok(monkeypatch)

    buy_intent = Intent(
        id=str(uuid.uuid4()),
        user_id=user_id,
        agent_id=agent_id,
        side="buy",
        title="bike",
        category="bikes",
        reservation_price_cents=50_000,
        ideal_price_cents=40_000,
        status="active",
        expires_at=datetime.utcnow().replace(year=2027),
    )
    sell_intent = Intent(
        id=str(uuid.uuid4()),
        user_id=cp_user_id,
        agent_id=cp_agent_id,
        side="sell",
        title="bike",
        category="bikes",
        reservation_price_cents=35_000,
        ideal_price_cents=45_000,
        status="active",
        expires_at=datetime.utcnow().replace(year=2027),
    )
    async_db_session.add(buy_intent)
    async_db_session.add(sell_intent)
    await async_db_session.flush()
    match = Match(
        id=str(uuid.uuid4()),
        buy_intent_id=buy_intent.id,
        sell_intent_id=sell_intent.id,
        status="agreed",
    )
    async_db_session.add(match)
    await async_db_session.flush()
    nego = Negotiation(
        id=str(uuid.uuid4()),
        match_id=match.id,
        state=[],
        rounds_used=2,
        max_rounds=6,
        status="agreed",  # already closed
    )
    async_db_session.add(nego)
    await async_db_session.flush()
    confirmed = Deal(
        id=str(uuid.uuid4()),
        negotiation_id=nego.id,
        buyer_user_id=user_id,
        seller_user_id=cp_user_id,
        buy_intent_id=buy_intent.id,
        sell_intent_id=sell_intent.id,
        agreed_price_cents=42_000,
        status="confirmed",
        idempotency_key=str(uuid.uuid4()),
    )
    async_db_session.add(confirmed)
    await async_db_session.commit()

    # Revoke
    draft = await http_client.post(
        f"/api/mandates/{mandate_id}/revoke/draft",
        json={"reason": "user_requested"},
    )
    submit = await http_client.post(
        f"/api/mandates/{mandate_id}/revoke/submit",
        json={
            "revocation_draft_id": draft.json()["revocation_draft_id"],
            "webauthn_assertion": fake_assertion_payload(),
        },
    )
    assert submit.status_code == 200

    # Confirmed deal NOT touched.
    confirmed_row = await async_db_session.scalar(
        select(Deal).where(Deal.id == confirmed.id)
    )
    assert confirmed_row.status == "confirmed"
