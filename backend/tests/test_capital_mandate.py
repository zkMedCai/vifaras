"""Autonomous Capital Mandate V0 tests."""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import select

from app.agents.tool_layer import AsyncToolHandler
from app.core.security import create_access_token
from app.models.schema import (
    CapitalLedgerEntry,
    CapitalMandate,
    CapitalPosition,
    Deal,
    Intent,
    Match,
)
from app.services import (
    capital_ledger_service,
    capital_mandate_service,
    capital_mandate_verifier,
    deal_service,
    embedding_service,
    negotiation_service,
)
from app.services.mandate_service import WebAuthnAssertionPayload
from tests.factories import fake_assertion_payload, setup_active_mandate_async


@pytest.fixture(autouse=True)
def _force_fake_embedding(monkeypatch):
    monkeypatch.setenv("EMBEDDING_BACKEND", "fake")
    embedding_service._reset_singleton_for_tests()
    yield
    embedding_service._reset_singleton_for_tests()


@pytest.fixture
def capital_webauthn_ok(monkeypatch):
    monkeypatch.setattr(
        "app.services.capital_mandate_service.verify_authentication_response",
        lambda **_: SimpleNamespace(new_sign_count=1),
    )


@pytest.fixture
def deal_webauthn_ok(monkeypatch):
    monkeypatch.setattr(
        "app.services.deal_service.verify_authentication_response",
        lambda **_: SimpleNamespace(new_sign_count=1),
    )


class FakeBaseVerifier:
    def __init__(self) -> None:
        self.authorized_actions: list[str] = []

    async def authorize_async(self, agent_id: str, action: str, params: dict) -> Any:
        self.authorized_actions.append(action)
        return SimpleNamespace(agent_id=agent_id)

    async def record_usage_async(
        self,
        mandate: Any,
        action: str,
        params: dict,
        success: bool,
        result: dict | None = None,
        error_code: str | None = None,
    ) -> None:
        return None

    async def log_failed_async(self, agent_id: str, action: str, error: Exception) -> None:
        return None


@dataclass
class NegotiationSetup:
    seller_user_id: str
    seller_agent_id: str
    buyer_user_id: str
    buyer_agent_id: str
    sell_intent_id: str
    buy_intent_id: str
    match_id: str
    negotiation_id: str
    proposal_hash: str | None


async def _create_capital_mandate(
    db,
    *,
    user_id: str,
    agent_id: str,
    budget_total_cents: int = 50_000,
    max_single_purchase_cents: int = 10_000,
    max_open_positions: int = 5,
    min_expected_margin_bps: int = 0,
    allowed_categories: list[str] | None = None,
    duration_days: int = 30,
) -> str:
    draft = await capital_mandate_service.create_capital_mandate_draft(
        db,
        user_id=user_id,
        input_obj=capital_mandate_service.CapitalMandateDraftInput(
            agent_id=agent_id,
            budget_total_cents=budget_total_cents,
            duration_days=duration_days,
            max_single_purchase_cents=max_single_purchase_cents,
            max_open_positions=max_open_positions,
            min_expected_margin_bps=min_expected_margin_bps,
            allowed_categories=allowed_categories or ["electronics"],
            geo_scope=["IT"],
            auto_buy=True,
            auto_sell=True,
            auto_relist=True,
        ),
    )
    result = await capital_mandate_service.submit_signed_capital_mandate(
        db,
        user_id=user_id,
        draft_id=draft.draft_id,
        assertion=WebAuthnAssertionPayload(**fake_assertion_payload()),
    )
    return result.capital_mandate_id


async def _seed_intent(
    db,
    *,
    user_id: str,
    side: str,
    category: str = "electronics_laptops",
    reservation_eur: float = 100,
    ideal_eur: float = 90,
) -> str:
    now = datetime.utcnow()
    intent = Intent(
        id=str(uuid.uuid4()),
        user_id=user_id,
        agent_id=None,
        side=side,
        title=f"{side}-{uuid.uuid4().hex[:6]}",
        description="console boxed",
        category=category,
        description_embedding=embedding_service._fake_embedding("console boxed"),
        reservation_price_cents=int(reservation_eur * 100),
        ideal_price_cents=int(ideal_eur * 100),
        currency="EUR",
        hard_constraints={},
        soft_preferences={},
        status="active",
        expires_at=now + timedelta(days=14),
        created_at=now,
    )
    db.add(intent)
    await db.commit()
    return intent.id


async def _seed_negotiation(db, *, price_cents: int = 9_000) -> NegotiationSetup:
    seller_id, seller_agent, _ = await setup_active_mandate_async(
        db, email=f"seller-{uuid.uuid4().hex[:8]}@x.com"
    )
    buyer_id, buyer_agent, _ = await setup_active_mandate_async(
        db, email=f"buyer-{uuid.uuid4().hex[:8]}@x.com"
    )
    sell_id = await _seed_intent(
        db,
        user_id=seller_id,
        side="sell",
        category="electronics_laptops",
        reservation_eur=80,
        ideal_eur=100,
    )
    buy_id = await _seed_intent(
        db,
        user_id=buyer_id,
        side="buy",
        category="electronics_laptops",
        reservation_eur=120,
        ideal_eur=90,
    )
    match = Match(
        id=str(uuid.uuid4()),
        buy_intent_id=buy_id,
        sell_intent_id=sell_id,
        similarity_score=0.95,
        price_overlap=True,
        price_proximity_score=0.9,
        combined_score=0.93,
        status="discovered",
    )
    db.add(match)
    await db.commit()
    offer = await negotiation_service.start_or_continue(
        db,
        user_id=seller_id,
        agent_id=seller_agent,
        match_id=match.id,
        price_cents=price_cents,
    )
    return NegotiationSetup(
        seller_user_id=seller_id,
        seller_agent_id=seller_agent,
        buyer_user_id=buyer_id,
        buyer_agent_id=buyer_agent,
        sell_intent_id=sell_id,
        buy_intent_id=buy_id,
        match_id=match.id,
        negotiation_id=offer.negotiation_id,
        proposal_hash=offer.last_turn.get("proposal_hash"),
    )


@pytest.mark.db
async def test_create_capital_mandate_draft(async_db_session) -> None:
    user_id, agent_id, _ = await setup_active_mandate_async(
        async_db_session, email=f"u-{uuid.uuid4().hex[:8]}@x.com"
    )
    draft = await capital_mandate_service.create_capital_mandate_draft(
        async_db_session,
        user_id=user_id,
        input_obj=capital_mandate_service.CapitalMandateDraftInput(
            agent_id=agent_id,
            budget_total_cents=50_000,
            duration_days=30,
            max_single_purchase_cents=10_000,
            max_open_positions=5,
            allowed_categories=["electronics"],
        ),
    )
    assert draft.draft_id
    assert draft.payload["requires_manual_approval"] is False
    assert "I profitti non sono garantiti" in draft.payload_summary["human_readable"]


@pytest.mark.db
async def test_submit_signed_capital_mandate(async_db_session, capital_webauthn_ok) -> None:
    user_id, agent_id, _ = await setup_active_mandate_async(
        async_db_session, email=f"u-{uuid.uuid4().hex[:8]}@x.com"
    )
    capital_id = await _create_capital_mandate(
        async_db_session, user_id=user_id, agent_id=agent_id
    )
    row = await async_db_session.get(CapitalMandate, capital_id)
    assert row is not None
    assert row.status == "active"
    assert row.signature["algorithm"] == "webauthn"


@pytest.mark.db
async def test_invalid_budget_over_cap_blocked(async_db_session) -> None:
    user_id, agent_id, _ = await setup_active_mandate_async(
        async_db_session, email=f"u-{uuid.uuid4().hex[:8]}@x.com"
    )
    with pytest.raises(capital_mandate_service.CapitalMandateInvalidLimits):
        await capital_mandate_service.create_capital_mandate_draft(
            async_db_session,
            user_id=user_id,
            input_obj=capital_mandate_service.CapitalMandateDraftInput(
                agent_id=agent_id,
                budget_total_cents=50_001,
            ),
        )


@pytest.mark.db
async def test_duration_default_30_days(async_db_session) -> None:
    user_id, agent_id, _ = await setup_active_mandate_async(
        async_db_session, email=f"u-{uuid.uuid4().hex[:8]}@x.com"
    )
    draft = await capital_mandate_service.create_capital_mandate_draft(
        async_db_session,
        user_id=user_id,
        input_obj=capital_mandate_service.CapitalMandateDraftInput(agent_id=agent_id),
    )
    assert draft.payload["duration_days"] == 30


@pytest.mark.db
async def test_duration_over_cap_blocked(async_db_session) -> None:
    user_id, agent_id, _ = await setup_active_mandate_async(
        async_db_session, email=f"u-{uuid.uuid4().hex[:8]}@x.com"
    )
    with pytest.raises(capital_mandate_service.CapitalMandateInvalidLimits):
        await capital_mandate_service.create_capital_mandate_draft(
            async_db_session,
            user_id=user_id,
            input_obj=capital_mandate_service.CapitalMandateDraftInput(
                agent_id=agent_id,
                duration_days=31,
            ),
        )


@pytest.mark.db
async def test_active_capital_mandate_returned(
    async_db_session, capital_webauthn_ok
) -> None:
    user_id, agent_id, _ = await setup_active_mandate_async(
        async_db_session, email=f"u-{uuid.uuid4().hex[:8]}@x.com"
    )
    capital_id = await _create_capital_mandate(
        async_db_session, user_id=user_id, agent_id=agent_id
    )
    active = await capital_mandate_service.get_active_capital_mandate(
        async_db_session, user_id=user_id, agent_id=agent_id
    )
    assert active.mandate is not None
    assert active.mandate.id == capital_id
    assert active.budget_state["available_cents"] == 50_000


@pytest.mark.db
async def test_pause_capital_mandate_blocks_authorization(
    async_db_session, capital_webauthn_ok
) -> None:
    user_id, agent_id, _ = await setup_active_mandate_async(
        async_db_session, email=f"u-{uuid.uuid4().hex[:8]}@x.com"
    )
    capital_id = await _create_capital_mandate(
        async_db_session, user_id=user_id, agent_id=agent_id
    )
    await capital_mandate_service.pause_capital_mandate(
        async_db_session, user_id=user_id, capital_mandate_id=capital_id
    )
    with pytest.raises(capital_mandate_verifier.CapitalMandateNotActive):
        await capital_mandate_verifier.authorize_auto_buy(
            async_db_session,
            agent_id=agent_id,
            amount_cents=5_000,
            expected_resale_price_cents=7_000,
            category="electronics_laptops",
        )


@pytest.mark.db
async def test_expired_capital_mandate_blocks_authorization(
    async_db_session, capital_webauthn_ok
) -> None:
    user_id, agent_id, _ = await setup_active_mandate_async(
        async_db_session, email=f"u-{uuid.uuid4().hex[:8]}@x.com"
    )
    capital_id = await _create_capital_mandate(
        async_db_session, user_id=user_id, agent_id=agent_id
    )
    mandate = await async_db_session.get(CapitalMandate, capital_id)
    mandate.expires_at = datetime.utcnow() - timedelta(seconds=1)
    await async_db_session.commit()
    with pytest.raises(capital_mandate_verifier.CapitalMandateNotActive):
        await capital_mandate_verifier.authorize_auto_buy(
            async_db_session,
            agent_id=agent_id,
            amount_cents=5_000,
            expected_resale_price_cents=7_000,
            category="electronics_laptops",
        )


@pytest.mark.db
async def test_revoked_capital_mandate_blocks_authorization(
    async_db_session, capital_webauthn_ok
) -> None:
    user_id, agent_id, _ = await setup_active_mandate_async(
        async_db_session, email=f"u-{uuid.uuid4().hex[:8]}@x.com"
    )
    capital_id = await _create_capital_mandate(
        async_db_session, user_id=user_id, agent_id=agent_id
    )
    await capital_mandate_service.revoke_capital_mandate(
        async_db_session,
        user_id=user_id,
        capital_mandate_id=capital_id,
        reason="test",
    )
    with pytest.raises(capital_mandate_verifier.CapitalMandateNotActive):
        await capital_mandate_verifier.authorize_auto_buy(
            async_db_session,
            agent_id=agent_id,
            amount_cents=5_000,
            expected_resale_price_cents=7_000,
            category="electronics_laptops",
        )


@pytest.mark.db
async def test_authorize_auto_buy_within_limits_succeeds(
    async_db_session, capital_webauthn_ok
) -> None:
    user_id, agent_id, _ = await setup_active_mandate_async(
        async_db_session, email=f"u-{uuid.uuid4().hex[:8]}@x.com"
    )
    await _create_capital_mandate(
        async_db_session,
        user_id=user_id,
        agent_id=agent_id,
        min_expected_margin_bps=1_000,
    )
    result = await capital_mandate_verifier.authorize_auto_buy(
        async_db_session,
        agent_id=agent_id,
        amount_cents=8_000,
        expected_resale_price_cents=10_000,
        category="electronics_laptops",
    )
    assert result.allowed is True
    assert result.expected_margin_bps == 2_500


@pytest.mark.db
async def test_auto_buy_over_max_single_purchase_fails(
    async_db_session, capital_webauthn_ok
) -> None:
    user_id, agent_id, _ = await setup_active_mandate_async(
        async_db_session, email=f"u-{uuid.uuid4().hex[:8]}@x.com"
    )
    await _create_capital_mandate(
        async_db_session,
        user_id=user_id,
        agent_id=agent_id,
        max_single_purchase_cents=5_000,
    )
    with pytest.raises(capital_mandate_verifier.MaxSinglePurchaseExceeded):
        await capital_mandate_verifier.authorize_auto_buy(
            async_db_session,
            agent_id=agent_id,
            amount_cents=5_001,
            expected_resale_price_cents=7_000,
            category="electronics_laptops",
        )


@pytest.mark.db
async def test_auto_buy_category_forbidden_fails(
    async_db_session, capital_webauthn_ok
) -> None:
    user_id, agent_id, _ = await setup_active_mandate_async(
        async_db_session, email=f"u-{uuid.uuid4().hex[:8]}@x.com"
    )
    await _create_capital_mandate(
        async_db_session,
        user_id=user_id,
        agent_id=agent_id,
        allowed_categories=["pokemon_cards"],
    )
    with pytest.raises(capital_mandate_verifier.CapitalCategoryNotAllowed):
        await capital_mandate_verifier.authorize_auto_buy(
            async_db_session,
            agent_id=agent_id,
            amount_cents=5_000,
            expected_resale_price_cents=7_000,
            category="electronics_laptops",
        )


@pytest.mark.db
async def test_auto_buy_insufficient_budget_fails(
    async_db_session, capital_webauthn_ok
) -> None:
    user_id, agent_id, _ = await setup_active_mandate_async(
        async_db_session, email=f"u-{uuid.uuid4().hex[:8]}@x.com"
    )
    capital_id = await _create_capital_mandate(
        async_db_session,
        user_id=user_id,
        agent_id=agent_id,
        max_single_purchase_cents=50_000,
    )
    await capital_ledger_service.reserve_budget(
        async_db_session,
        capital_mandate_id=capital_id,
        amount_cents=49_000,
        deal_id=None,
        idempotency_key=f"reserve-{uuid.uuid4()}",
    )
    await async_db_session.commit()
    with pytest.raises(capital_mandate_verifier.CapitalBudgetExceeded):
        await capital_mandate_verifier.authorize_auto_buy(
            async_db_session,
            agent_id=agent_id,
            amount_cents=2_000,
            expected_resale_price_cents=4_000,
            category="electronics_laptops",
        )


@pytest.mark.db
async def test_reserve_budget_creates_ledger_entry(
    async_db_session, capital_webauthn_ok
) -> None:
    user_id, agent_id, _ = await setup_active_mandate_async(
        async_db_session, email=f"u-{uuid.uuid4().hex[:8]}@x.com"
    )
    capital_id = await _create_capital_mandate(
        async_db_session, user_id=user_id, agent_id=agent_id
    )
    entry = await capital_ledger_service.reserve_budget(
        async_db_session,
        capital_mandate_id=capital_id,
        amount_cents=3_000,
        deal_id=None,
        idempotency_key="reserve-one",
    )
    await async_db_session.commit()
    assert entry.type == "budget_reserved"
    state = await capital_ledger_service.compute_budget_state(
        async_db_session, capital_mandate_id=capital_id
    )
    assert state.reserved_cents == 3_000
    assert state.available_cents == 47_000


@pytest.mark.db
async def test_repeated_reserve_idempotency_key_does_not_double_reserve(
    async_db_session, capital_webauthn_ok
) -> None:
    user_id, agent_id, _ = await setup_active_mandate_async(
        async_db_session, email=f"u-{uuid.uuid4().hex[:8]}@x.com"
    )
    capital_id = await _create_capital_mandate(
        async_db_session, user_id=user_id, agent_id=agent_id
    )
    await capital_ledger_service.reserve_budget(
        async_db_session,
        capital_mandate_id=capital_id,
        amount_cents=3_000,
        deal_id=None,
        idempotency_key="reserve-idem",
    )
    await capital_ledger_service.reserve_budget(
        async_db_session,
        capital_mandate_id=capital_id,
        amount_cents=3_000,
        deal_id=None,
        idempotency_key="reserve-idem",
    )
    await async_db_session.commit()
    rows = (
        await async_db_session.scalars(
            select(CapitalLedgerEntry).where(
                CapitalLedgerEntry.idempotency_key == "reserve-idem"
            )
        )
    ).all()
    state = await capital_ledger_service.compute_budget_state(
        async_db_session, capital_mandate_id=capital_id
    )
    assert len(rows) == 1
    assert state.reserved_cents == 3_000


@pytest.mark.db
async def test_accept_offer_under_capital_mandate_preauthorizes_buyer_side(
    async_db_session, capital_webauthn_ok
) -> None:
    setup = await _seed_negotiation(async_db_session, price_cents=9_000)
    await _create_capital_mandate(
        async_db_session,
        user_id=setup.buyer_user_id,
        agent_id=setup.buyer_agent_id,
    )
    verifier = FakeBaseVerifier()
    handler = AsyncToolHandler(
        async_db_session, setup.buyer_agent_id, verifier=verifier
    )
    result = await handler.handle(
        "accept_offer_under_capital_mandate",
        {
            "negotiation_id": setup.negotiation_id,
            "proposal_price_cents": 9_000,
            "expected_resale_price_cents": 12_000,
            "category": "electronics_laptops",
            "proposal_hash": setup.proposal_hash,
        },
    )
    assert result.status == "ok"
    deal = await async_db_session.get(Deal, result.data["deal_id"])
    assert deal.buyer_authorization_method == "capital_mandate"
    assert deal.buyer_capital_mandate_id is not None
    assert deal.buyer_signed_at is None
    assert verifier.authorized_actions == ["accept_offer_under_capital_mandate"]


@pytest.mark.db
async def test_deal_remains_pending_if_seller_side_not_authorized(
    async_db_session, capital_webauthn_ok
) -> None:
    setup = await _seed_negotiation(async_db_session, price_cents=9_000)
    await _create_capital_mandate(
        async_db_session,
        user_id=setup.buyer_user_id,
        agent_id=setup.buyer_agent_id,
    )
    handler = AsyncToolHandler(
        async_db_session, setup.buyer_agent_id, verifier=FakeBaseVerifier()
    )
    result = await handler.handle(
        "accept_offer_under_capital_mandate",
        {
            "negotiation_id": setup.negotiation_id,
            "proposal_price_cents": 9_000,
            "expected_resale_price_cents": 12_000,
            "category": "electronics_laptops",
            "proposal_hash": setup.proposal_hash,
        },
    )
    deal = await async_db_session.get(Deal, result.data["deal_id"])
    assert deal.status == "pending_signatures"
    assert result.data["next_step"] == "counterparty_signature_required"


@pytest.mark.db
async def test_deal_confirms_when_other_side_signs(
    async_db_session, capital_webauthn_ok, deal_webauthn_ok
) -> None:
    setup = await _seed_negotiation(async_db_session, price_cents=9_000)
    await _create_capital_mandate(
        async_db_session,
        user_id=setup.buyer_user_id,
        agent_id=setup.buyer_agent_id,
    )
    handler = AsyncToolHandler(
        async_db_session, setup.buyer_agent_id, verifier=FakeBaseVerifier()
    )
    accepted = await handler.handle(
        "accept_offer_under_capital_mandate",
        {
            "negotiation_id": setup.negotiation_id,
            "proposal_price_cents": 9_000,
            "expected_resale_price_cents": 12_000,
            "category": "electronics_laptops",
            "proposal_hash": setup.proposal_hash,
        },
    )
    draft = await deal_service.request_sign_draft(
        async_db_session,
        user_id=setup.seller_user_id,
        deal_id=accepted.data["deal_id"],
    )
    signed = await deal_service.submit_signature(
        async_db_session,
        user_id=setup.seller_user_id,
        draft_id=draft.draft_id,
        assertion=WebAuthnAssertionPayload(**fake_assertion_payload()),
    )
    deal = await async_db_session.get(Deal, accepted.data["deal_id"])
    assert signed.deal_confirmed is True
    assert deal.status == "confirmed"


@pytest.mark.db
async def test_standard_accept_offer_behavior_unchanged(async_db_session) -> None:
    setup = await _seed_negotiation(async_db_session, price_cents=9_000)
    accepted = await negotiation_service.accept_offer(
        async_db_session,
        user_id=setup.buyer_user_id,
        agent_id=setup.buyer_agent_id,
        negotiation_id=setup.negotiation_id,
        proposal_hash=setup.proposal_hash,
    )
    deal = await async_db_session.get(Deal, accepted.deal_id)
    assert accepted.next_step == "sign_deal_with_passkey"
    assert deal.status == "pending_signatures"
    assert deal.buyer_authorization_method is None
    assert deal.seller_authorization_method is None


@pytest.mark.db
async def test_ledger_appears_in_api(
    async_db_session, http_client, capital_webauthn_ok
) -> None:
    user_id, agent_id, _ = await setup_active_mandate_async(
        async_db_session, email=f"u-{uuid.uuid4().hex[:8]}@x.com"
    )
    capital_id = await _create_capital_mandate(
        async_db_session, user_id=user_id, agent_id=agent_id
    )
    await capital_ledger_service.reserve_budget(
        async_db_session,
        capital_mandate_id=capital_id,
        amount_cents=3_000,
        deal_id=None,
        idempotency_key="api-ledger",
    )
    await async_db_session.commit()
    http_client.headers["Authorization"] = (
        f"Bearer {create_access_token(user_id=user_id, tier=2)}"
    )
    resp = await http_client.get(f"/api/capital-mandates/{capital_id}/ledger")
    assert resp.status_code == 200
    body = resp.json()
    assert body["entries"][0]["idempotency_key"] == "api-ledger"
    assert body["budget_state"]["reserved_cents"] == 3_000


@pytest.mark.db
async def test_positions_appear_in_api(
    async_db_session, http_client, capital_webauthn_ok
) -> None:
    setup = await _seed_negotiation(async_db_session, price_cents=9_000)
    capital_id = await _create_capital_mandate(
        async_db_session,
        user_id=setup.buyer_user_id,
        agent_id=setup.buyer_agent_id,
    )
    accepted = await negotiation_service.accept_offer(
        async_db_session,
        user_id=setup.buyer_user_id,
        agent_id=setup.buyer_agent_id,
        negotiation_id=setup.negotiation_id,
        proposal_hash=setup.proposal_hash,
    )
    await deal_service.authorize_deal_side_with_capital_mandate(
        async_db_session,
        user_id=setup.buyer_user_id,
        agent_id=setup.buyer_agent_id,
        deal_id=accepted.deal_id,
        capital_mandate_id=capital_id,
        expected_resale_price_cents=12_000,
        category="electronics_laptops",
    )
    http_client.headers["Authorization"] = (
        f"Bearer {create_access_token(user_id=setup.buyer_user_id, tier=2)}"
    )
    resp = await http_client.get(f"/api/capital-mandates/{capital_id}/positions")
    assert resp.status_code == 200
    body = resp.json()
    assert body["positions"][0]["status"] == "purchase_authorized"
    assert body["positions"][0]["source_buy_deal_id"] == accepted.deal_id


@pytest.mark.db
async def test_active_endpoint_renders_budget_summary(
    async_db_session, http_client, capital_webauthn_ok
) -> None:
    user_id, agent_id, _ = await setup_active_mandate_async(
        async_db_session, email=f"u-{uuid.uuid4().hex[:8]}@x.com"
    )
    await _create_capital_mandate(
        async_db_session, user_id=user_id, agent_id=agent_id
    )
    http_client.headers["Authorization"] = (
        f"Bearer {create_access_token(user_id=user_id, tier=2)}"
    )
    resp = await http_client.get("/api/capital-mandates/active")
    assert resp.status_code == 200
    body = resp.json()
    assert body["active"] is True
    assert body["budget_state"]["available_cents"] == 50_000
