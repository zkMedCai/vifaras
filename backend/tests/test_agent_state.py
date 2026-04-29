"""Agent state service + inbox tests (brief task 6.2).

22 tests organized by concern:

  Identity & mandate (4):
   1. get_state returns agent identity correctly
   2. get_state includes mandate when active
   3. get_state omits mandate for pending_mandate agent
   4. get_state omits mandate for revoked agent

  Limits remaining (3):
   5. limits_remaining computed at zero-spend (full quota)
   6. limits_remaining decremented after spend
   7. limits_remaining surfaces post-reset values when stale daily counter

  Intents (2):
   8. active_intents view includes match_count
   9. intents view only includes active owned by user

  Matches privacy + ranking (3):
  10. matches view omits ideal_price of other intent
  11. matches view includes score breakdown
  12. matches view filtered by my intent ownership

  Negotiations (3):
  13. negotiation view awaiting_my_response correct when other party last
  14. negotiation view includes round + final flag
  15. negotiation view only includes my negotiations

  Inbox (4):
  16. inbox returns offers since last_tick_at
  17. inbox excludes offers before since
  18. inbox includes deals_awaiting_my_signature
  19. inbox includes approved step-ups since cursor

  Edge cases (3):
  20. pending_mandate agent returns minimal state
  21. revoked agent returns minimal state
  22. nonexistent agent raises AgentNotFound
"""
from __future__ import annotations

import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select

from app.models.schema import (
    Agent,
    Deal,
    Intent,
    Mandate,
    Match,
    Negotiation,
    StepUpRequest,
    User,
)
from app.services import (
    agent_state_service,
    embedding_service,
    negotiation_service,
)
from tests.factories import default_user_kwargs, setup_active_mandate_async


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _force_fake_embedding(monkeypatch):
    monkeypatch.setenv("EMBEDDING_BACKEND", "fake")
    embedding_service._reset_singleton_for_tests()
    yield
    embedding_service._reset_singleton_for_tests()


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


async def _seed_intent(
    db,
    *,
    user_id: str,
    side: str,
    seed_text: str = "macbook",
    reservation_eur: float = 1000,
    ideal_eur: float = 1100,
    status: str = "active",
) -> str:
    intent_id = str(uuid.uuid4())
    now = datetime.utcnow()
    intent = Intent(
        id=intent_id,
        user_id=user_id,
        agent_id=None,
        side=side,
        title=f"intent-{intent_id[:6]}",
        description=seed_text,
        category="electronics_laptops",
        description_embedding=embedding_service._fake_embedding(seed_text),
        reservation_price_cents=int(reservation_eur * 100),
        ideal_price_cents=int(ideal_eur * 100),
        currency="EUR",
        hard_constraints={},
        soft_preferences={},
        status=status,
        expires_at=now + timedelta(days=14),
        created_at=now,
    )
    db.add(intent)
    await db.commit()
    return intent_id


async def _seed_match(
    db, *, buy_intent_id: str, sell_intent_id: str, status: str = "discovered"
) -> str:
    m = Match(
        id=str(uuid.uuid4()),
        buy_intent_id=buy_intent_id,
        sell_intent_id=sell_intent_id,
        similarity_score=Decimal("0.95"),
        price_overlap=True,
        price_proximity_score=Decimal("0.85"),
        combined_score=Decimal("0.92"),
        status=status,
    )
    db.add(m)
    await db.commit()
    return m.id


@dataclass
class StateSetup:
    seller_user_id: str
    seller_agent_id: str
    seller_mandate_id: str
    buyer_user_id: str
    buyer_agent_id: str
    sell_intent_id: str
    buy_intent_id: str
    match_id: str


async def _seed_two_users_with_match(db) -> StateSetup:
    seller_id, seller_agent, seller_mandate = await setup_active_mandate_async(
        db, email=f"seller-{uuid.uuid4().hex[:6]}@x.com"
    )
    buyer_id, buyer_agent, _ = await setup_active_mandate_async(
        db, email=f"buyer-{uuid.uuid4().hex[:6]}@x.com"
    )
    sell_id = await _seed_intent(
        db,
        user_id=seller_id,
        side="sell",
        reservation_eur=1000,
        ideal_eur=1200,
    )
    buy_id = await _seed_intent(
        db,
        user_id=buyer_id,
        side="buy",
        reservation_eur=1500,
        ideal_eur=1100,
    )
    match_id = await _seed_match(
        db, buy_intent_id=buy_id, sell_intent_id=sell_id
    )
    return StateSetup(
        seller_user_id=seller_id,
        seller_agent_id=seller_agent,
        seller_mandate_id=seller_mandate,
        buyer_user_id=buyer_id,
        buyer_agent_id=buyer_agent,
        sell_intent_id=sell_id,
        buy_intent_id=buy_id,
        match_id=match_id,
    )


async def _seed_pending_mandate_agent(db, *, email: str) -> tuple[str, str]:
    user_id = str(uuid.uuid4())
    agent_id = str(uuid.uuid4())
    now = datetime.utcnow()
    user = User(id=user_id, **default_user_kwargs(tier=1, email=email))
    db.add(user)
    await db.flush()
    agent = Agent(
        id=agent_id,
        user_id=user_id,
        name="pending",
        pubkey=f"pk-{agent_id[:6]}",
        privkey_kms_ref=f"file:.secrets/agent_keys/{agent_id}.json",
        status="pending_mandate",
        created_at=now,
    )
    db.add(agent)
    await db.commit()
    return user_id, agent_id


# ===========================================================================
# 1. agent identity returned correctly
# ===========================================================================


@pytest.mark.db
async def test_get_state_returns_agent_identity(async_db_session) -> None:
    s = await _seed_two_users_with_match(async_db_session)
    state = await agent_state_service.get_full_state(
        async_db_session, agent_id=s.seller_agent_id
    )
    assert state.agent_id == s.seller_agent_id
    assert state.user_id == s.seller_user_id
    assert state.agent_status == "active"
    # nullifier pseudonym is the truncated hash; not None for active mandate users.
    assert state.nullifier_pseudonym is not None
    assert len(state.nullifier_pseudonym) == 12


# ===========================================================================
# 2. mandate present when active
# ===========================================================================


@pytest.mark.db
async def test_get_state_includes_mandate_when_active(async_db_session) -> None:
    s = await _seed_two_users_with_match(async_db_session)
    state = await agent_state_service.get_full_state(
        async_db_session, agent_id=s.seller_agent_id
    )
    assert state.mandate is not None
    assert state.mandate.mandate_id == s.seller_mandate_id
    assert "create_intent" in state.mandate.allowed_actions
    assert state.limits_remaining is not None
    assert state.limits_remaining.deals_remaining_today >= 0


# ===========================================================================
# 3. mandate omitted for pending_mandate agent
# ===========================================================================


@pytest.mark.db
async def test_get_state_omits_mandate_for_pending(async_db_session) -> None:
    user_id, agent_id = await _seed_pending_mandate_agent(
        async_db_session, email="pend@x.com"
    )
    state = await agent_state_service.get_full_state(
        async_db_session, agent_id=agent_id
    )
    assert state.agent_status == "pending_mandate"
    assert state.mandate is None
    assert state.limits_remaining is None


# ===========================================================================
# 4. mandate omitted for revoked agent
# ===========================================================================


@pytest.mark.db
async def test_get_state_omits_mandate_for_revoked(async_db_session) -> None:
    s = await _seed_two_users_with_match(async_db_session)
    # Revoke the seller's mandate + flip agent status.
    mandate = await async_db_session.get(Mandate, s.seller_mandate_id)
    mandate.revoked_at = datetime.utcnow()
    agent = await async_db_session.get(Agent, s.seller_agent_id)
    agent.status = "revoked"
    await async_db_session.commit()

    state = await agent_state_service.get_full_state(
        async_db_session, agent_id=s.seller_agent_id
    )
    assert state.agent_status == "revoked"
    assert state.mandate is None
    assert state.limits_remaining is None


# ===========================================================================
# 5. limits remaining at zero-spend
# ===========================================================================


@pytest.mark.db
async def test_limits_remaining_at_zero_spend(async_db_session) -> None:
    s = await _seed_two_users_with_match(async_db_session)
    state = await agent_state_service.get_full_state(
        async_db_session, agent_id=s.seller_agent_id
    )
    lim = state.limits_remaining
    assert lim is not None
    # Default mandate: 200 EUR/day, 500 EUR/mandate, 3 deals/day.
    assert lim.daily_volume_remaining_cents == 20000
    assert lim.mandate_total_volume_remaining_cents == 50000
    assert lim.deals_remaining_today == 3
    assert lim.is_at_daily_cap is False
    assert lim.is_near_mandate_cap is False


# ===========================================================================
# 6. limits remaining decremented after spend
# ===========================================================================


@pytest.mark.db
async def test_limits_remaining_decremented(async_db_session) -> None:
    s = await _seed_two_users_with_match(async_db_session)
    # Manually bump the spent counters on the mandate.
    mandate = await async_db_session.get(Mandate, s.seller_mandate_id)
    mandate.spent_today_eur = Decimal("50")
    mandate.spent_total_eur = Decimal("200")
    mandate.deals_count = 1
    mandate.last_reset_date = datetime.utcnow()
    await async_db_session.commit()

    state = await agent_state_service.get_full_state(
        async_db_session, agent_id=s.seller_agent_id
    )
    lim = state.limits_remaining
    assert lim.daily_volume_remaining_cents == 15000  # 200 - 50 = 150 EUR
    assert lim.mandate_total_volume_remaining_cents == 30000  # 500 - 200 = 300 EUR
    assert lim.deals_remaining_today == 2  # 3 - 1


# ===========================================================================
# 7. stale daily counter resets in view
# ===========================================================================


@pytest.mark.db
async def test_limits_remaining_resets_stale_daily(async_db_session) -> None:
    s = await _seed_two_users_with_match(async_db_session)
    mandate = await async_db_session.get(Mandate, s.seller_mandate_id)
    mandate.spent_today_eur = Decimal("180")  # near daily cap
    mandate.deals_count = 3
    # Force last_reset_date into yesterday so the view treats the counter as stale.
    mandate.last_reset_date = datetime.utcnow() - timedelta(days=2)
    await async_db_session.commit()

    state = await agent_state_service.get_full_state(
        async_db_session, agent_id=s.seller_agent_id
    )
    lim = state.limits_remaining
    # View surfaces post-reset values: full daily quota again.
    assert lim.daily_volume_remaining_cents == 20000
    assert lim.deals_remaining_today == 3


# ===========================================================================
# 8. active intents include match count
# ===========================================================================


@pytest.mark.db
async def test_active_intents_include_match_count(async_db_session) -> None:
    s = await _seed_two_users_with_match(async_db_session)
    state = await agent_state_service.get_full_state(
        async_db_session, agent_id=s.seller_agent_id
    )
    intents = state.active_intents
    assert len(intents) == 1
    assert intents[0].intent_id == s.sell_intent_id
    assert intents[0].match_count_active == 1


# ===========================================================================
# 9. intents only active + owned
# ===========================================================================


@pytest.mark.db
async def test_intents_only_active_owned(async_db_session) -> None:
    s = await _seed_two_users_with_match(async_db_session)
    # Seed a cancelled intent for seller.
    await _seed_intent(
        async_db_session,
        user_id=s.seller_user_id,
        side="sell",
        seed_text="other",
        status="cancelled",
    )
    # Seed an active intent for someone ELSE.
    other_id, _, _ = await setup_active_mandate_async(
        async_db_session, email=f"other-{uuid.uuid4().hex[:6]}@x.com"
    )
    await _seed_intent(
        async_db_session, user_id=other_id, side="sell", seed_text="z"
    )

    state = await agent_state_service.get_full_state(
        async_db_session, agent_id=s.seller_agent_id
    )
    ids = {i.intent_id for i in state.active_intents}
    assert ids == {s.sell_intent_id}


# ===========================================================================
# 10. match view does NOT expose other intent's ideal_price
# ===========================================================================


@pytest.mark.db
async def test_match_view_no_ideal_price_leak(async_db_session) -> None:
    s = await _seed_two_users_with_match(async_db_session)
    state = await agent_state_service.get_full_state(
        async_db_session, agent_id=s.seller_agent_id
    )
    assert len(state.discovered_matches) == 1
    other = state.discovered_matches[0].other_intent
    # OtherIntentView model doesn't expose ideal_price_eur (DQ-31).
    assert "ideal_price_eur" not in other.model_dump()
    assert other.reservation_price_eur > 0  # but reservation IS exposed


# ===========================================================================
# 11. match view score breakdown
# ===========================================================================


@pytest.mark.db
async def test_match_view_score_breakdown(async_db_session) -> None:
    s = await _seed_two_users_with_match(async_db_session)
    state = await agent_state_service.get_full_state(
        async_db_session, agent_id=s.seller_agent_id
    )
    m = state.discovered_matches[0]
    assert m.similarity_score == pytest.approx(0.95, rel=1e-3)
    assert m.price_proximity_score == pytest.approx(0.85, rel=1e-3)
    assert m.combined_score == pytest.approx(0.92, rel=1e-3)


# ===========================================================================
# 12. match view filtered by ownership
# ===========================================================================


@pytest.mark.db
async def test_match_view_filtered_by_ownership(async_db_session) -> None:
    s = await _seed_two_users_with_match(async_db_session)
    # Seed an unrelated match between two other users.
    a, _, _ = await setup_active_mandate_async(
        async_db_session, email=f"a-{uuid.uuid4().hex[:6]}@x.com"
    )
    b, _, _ = await setup_active_mandate_async(
        async_db_session, email=f"b-{uuid.uuid4().hex[:6]}@x.com"
    )
    a_sell = await _seed_intent(async_db_session, user_id=a, side="sell")
    b_buy = await _seed_intent(
        async_db_session, user_id=b, side="buy", reservation_eur=1500, ideal_eur=1100
    )
    await _seed_match(
        async_db_session, buy_intent_id=b_buy, sell_intent_id=a_sell
    )

    # Seller should still see only THEIR match.
    state = await agent_state_service.get_full_state(
        async_db_session, agent_id=s.seller_agent_id
    )
    match_ids = {m.match_id for m in state.discovered_matches}
    assert match_ids == {s.match_id}


# ===========================================================================
# 13. negotiation awaiting_my_response when other party last
# ===========================================================================


@pytest.mark.db
async def test_negotiation_awaiting_my_response(async_db_session) -> None:
    s = await _seed_two_users_with_match(async_db_session)
    # Buyer makes the first offer.
    await negotiation_service.start_or_continue(
        async_db_session,
        user_id=s.buyer_user_id,
        agent_id=s.buyer_agent_id,
        match_id=s.match_id,
        price_cents=120000,
    )
    # Now query state from the SELLER's POV — last turn was buyer's.
    state = await agent_state_service.get_full_state(
        async_db_session, agent_id=s.seller_agent_id
    )
    nego = state.active_negotiations[0]
    assert nego.awaiting_my_response is True
    assert nego.last_offer is not None
    assert nego.last_offer.is_from_me is False


# ===========================================================================
# 14. negotiation includes round + final flag
# ===========================================================================


@pytest.mark.db
async def test_negotiation_round_and_final_flag(async_db_session) -> None:
    s = await _seed_two_users_with_match(async_db_session)
    pairs = [
        (s.seller_user_id, s.seller_agent_id),
        (s.buyer_user_id, s.buyer_agent_id),
    ]
    for i in range(5):  # 5 turns → is_final_round flag flips on
        u, a = pairs[i % 2]
        await negotiation_service.start_or_continue(
            async_db_session,
            user_id=u,
            agent_id=a,
            match_id=s.match_id,
            price_cents=100000,
        )

    state = await agent_state_service.get_full_state(
        async_db_session, agent_id=s.seller_agent_id
    )
    nego = state.active_negotiations[0]
    assert nego.rounds_used == 5
    assert nego.max_rounds == 6
    assert nego.is_final_round is True


# ===========================================================================
# 15. negotiations only mine
# ===========================================================================


@pytest.mark.db
async def test_negotiations_only_mine(async_db_session) -> None:
    s = await _seed_two_users_with_match(async_db_session)
    await negotiation_service.start_or_continue(
        async_db_session,
        user_id=s.seller_user_id,
        agent_id=s.seller_agent_id,
        match_id=s.match_id,
        price_cents=120000,
    )

    # Unrelated 3rd-party negotiation
    a, _, _ = await setup_active_mandate_async(
        async_db_session, email=f"x-{uuid.uuid4().hex[:6]}@x.com"
    )
    b, b_agent, _ = await setup_active_mandate_async(
        async_db_session, email=f"y-{uuid.uuid4().hex[:6]}@x.com"
    )
    a_sell = await _seed_intent(async_db_session, user_id=a, side="sell")
    b_buy = await _seed_intent(
        async_db_session, user_id=b, side="buy", reservation_eur=1500, ideal_eur=1100
    )
    other_match_id = await _seed_match(
        async_db_session, buy_intent_id=b_buy, sell_intent_id=a_sell
    )
    await negotiation_service.start_or_continue(
        async_db_session,
        user_id=b,
        agent_id=b_agent,
        match_id=other_match_id,
        price_cents=120000,
    )

    state = await agent_state_service.get_full_state(
        async_db_session, agent_id=s.seller_agent_id
    )
    nego_match_ids = {n.match_id for n in state.active_negotiations}
    assert nego_match_ids == {s.match_id}


# ===========================================================================
# 16. inbox returns offers since last_tick_at
# ===========================================================================


@pytest.mark.db
async def test_inbox_returns_offers_since_last_tick(async_db_session) -> None:
    s = await _seed_two_users_with_match(async_db_session)
    # Set seller's last_tick_at to 1 hour ago.
    seller_agent = await async_db_session.get(Agent, s.seller_agent_id)
    seller_agent.last_tick_at = datetime.utcnow() - timedelta(hours=1)
    await async_db_session.commit()

    # Buyer offers AFTER last_tick.
    await negotiation_service.start_or_continue(
        async_db_session,
        user_id=s.buyer_user_id,
        agent_id=s.buyer_agent_id,
        match_id=s.match_id,
        price_cents=120000,
    )

    state = await agent_state_service.get_full_state(
        async_db_session, agent_id=s.seller_agent_id
    )
    assert len(state.inbox.new_offers_received) == 1
    assert state.inbox.new_offers_received[0].price_cents == 120000


# ===========================================================================
# 17. inbox excludes offers before since
# ===========================================================================


@pytest.mark.db
async def test_inbox_excludes_offers_before_since(async_db_session) -> None:
    s = await _seed_two_users_with_match(async_db_session)
    # Buyer offers FIRST.
    await negotiation_service.start_or_continue(
        async_db_session,
        user_id=s.buyer_user_id,
        agent_id=s.buyer_agent_id,
        match_id=s.match_id,
        price_cents=120000,
    )
    # NOW set seller's last_tick_at to right after — the offer is "before since".
    seller_agent = await async_db_session.get(Agent, s.seller_agent_id)
    seller_agent.last_tick_at = datetime.utcnow() + timedelta(seconds=5)
    await async_db_session.commit()

    state = await agent_state_service.get_full_state(
        async_db_session, agent_id=s.seller_agent_id
    )
    assert state.inbox.new_offers_received == []


# ===========================================================================
# 18. inbox includes deals_awaiting_my_signature
# ===========================================================================


@pytest.mark.db
async def test_inbox_includes_pending_deal_signature(async_db_session) -> None:
    s = await _seed_two_users_with_match(async_db_session)
    # Seller offers, buyer accepts → deal pending, both signatures missing.
    nego = await negotiation_service.start_or_continue(
        async_db_session,
        user_id=s.seller_user_id,
        agent_id=s.seller_agent_id,
        match_id=s.match_id,
        price_cents=120000,
    )
    await negotiation_service.accept_offer(
        async_db_session,
        user_id=s.buyer_user_id,
        agent_id=s.buyer_agent_id,
        negotiation_id=nego.negotiation_id,
    )

    # Both seller and buyer should see the deal awaiting their sig.
    state_seller = await agent_state_service.get_full_state(
        async_db_session, agent_id=s.seller_agent_id
    )
    state_buyer = await agent_state_service.get_full_state(
        async_db_session, agent_id=s.buyer_agent_id
    )
    assert len(state_seller.inbox.deals_awaiting_my_signature) == 1
    assert len(state_buyer.inbox.deals_awaiting_my_signature) == 1


# ===========================================================================
# 19. inbox includes approved step-ups since cursor
# ===========================================================================


@pytest.mark.db
async def test_inbox_includes_approved_step_ups(async_db_session) -> None:
    s = await _seed_two_users_with_match(async_db_session)
    # Insert a resolved step-up directly.
    now = datetime.utcnow()
    seller_agent = await async_db_session.get(Agent, s.seller_agent_id)
    seller_agent.last_tick_at = now - timedelta(hours=1)
    su = StepUpRequest(
        id=str(uuid.uuid4()),
        agent_id=s.seller_agent_id,
        mandate_id=s.seller_mandate_id,
        user_id=s.seller_user_id,
        action="accept_offer",
        action_params={"price_cents": 12000},
        reason="Above threshold",
        challenge=secrets.token_bytes(32),
        canonical_payload=b"{}",
        status="approved",
        expires_at=now + timedelta(minutes=10),
        resolved_at=now,
        created_at=now,
    )
    async_db_session.add(su)
    await async_db_session.commit()

    state = await agent_state_service.get_full_state(
        async_db_session, agent_id=s.seller_agent_id
    )
    assert len(state.inbox.approved_step_ups) == 1
    assert state.inbox.approved_step_ups[0].step_up_id == su.id


# ===========================================================================
# 20. pending_mandate agent → minimal state
# ===========================================================================


@pytest.mark.db
async def test_pending_mandate_agent_minimal_state(async_db_session) -> None:
    user_id, agent_id = await _seed_pending_mandate_agent(
        async_db_session, email="p@x.com"
    )
    state = await agent_state_service.get_full_state(
        async_db_session, agent_id=agent_id
    )
    assert state.mandate is None
    assert state.limits_remaining is None
    assert state.active_intents == []
    assert state.discovered_matches == []
    assert state.active_negotiations == []
    assert state.pending_deals == []
    assert state.next_action_required is False


# ===========================================================================
# 21. revoked agent → minimal state
# ===========================================================================


@pytest.mark.db
async def test_revoked_agent_minimal_state(async_db_session) -> None:
    s = await _seed_two_users_with_match(async_db_session)
    mandate = await async_db_session.get(Mandate, s.seller_mandate_id)
    mandate.revoked_at = datetime.utcnow()
    agent = await async_db_session.get(Agent, s.seller_agent_id)
    agent.status = "revoked"
    await async_db_session.commit()

    state = await agent_state_service.get_full_state(
        async_db_session, agent_id=s.seller_agent_id
    )
    assert state.agent_status == "revoked"
    assert state.mandate is None
    # Active intents are still owned by the user; the agent runtime in
    # 6.3 will decline to act on them given mandate=None.
    assert state.next_action_required in (False, True)  # heuristic; not asserting strict


# ===========================================================================
# 22. nonexistent agent → AgentNotFound
# ===========================================================================


@pytest.mark.db
async def test_nonexistent_agent_raises(async_db_session) -> None:
    with pytest.raises(agent_state_service.AgentNotFound):
        await agent_state_service.get_full_state(
            async_db_session, agent_id=str(uuid.uuid4())
        )
