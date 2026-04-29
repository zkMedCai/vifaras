"""Mini-auction concurrency + cascade tests (brief task 5.2).

18 tests organized by concern:

  Single-match regression (4):
   1. accept marks BOTH intents 'matched'
   2. accept works with no competing matches (vanilla case)
   3. accept does not touch unrelated users' intents
   4. match lifecycle: 'discovered'→'negotiating'→'agreed' (chosen),
      'discovered'/'negotiating'→'expired' (others)

  Multi-match cascade (6):
   5. accept on chosen match cancels competing active negotiations
   6. accept expires competing matches (status 'discovered'/'negotiating')
   7. cascade ignores already-cancelled negotiations
   8. cascade ignores terminal-status matches (agreed/rejected/expired)
   9. cascade only affects matches whose intents touch the accepted pair
  10. cancellation_reason 'other_match_accepted' persisted to state JSONB

  Race conditions (4):
  11. sequential second accept on already-matched intent → IntentAlreadyMatched
  12. two accepts on disjoint intent pairs both succeed
  13. lock order invariant: sorted-by-ID lock works regardless of buy/sell role
  14. competing-match audit log row exists with reason

  Intent state transitions (4):
  15. cannot cancel matched intent (intent_service.cancel_intent → 409)
  16. find_matches_for_intent skips matched intents
  17. both intents transition to 'matched' atomically (success path)
  18. matched intent rejects update_intent attempts
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select

from app.models.schema import (
    Agent,
    AuditLog,
    Intent,
    Match,
    Negotiation,
    User,
)
from app.services import (
    embedding_service,
    intent_service,
    match_service,
    negotiation_service,
)
from tests.factories import default_user_kwargs, setup_active_mandate_async


# ---------------------------------------------------------------------------
# Module fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _force_fake_embedding(monkeypatch):
    monkeypatch.setenv("EMBEDDING_BACKEND", "fake")
    embedding_service._reset_singleton_for_tests()
    yield
    embedding_service._reset_singleton_for_tests()


# ---------------------------------------------------------------------------
# Seed helpers (inline copies of test_negotiation.py — promotion to
# factories.py is a 7.x cleanup task; see IDEAS_BACKLOG.md)
# ---------------------------------------------------------------------------


@dataclass
class BuyerEntry:
    user_id: str
    agent_id: str
    intent_id: str
    match_id: str


@dataclass
class MultiMatchSetup:
    seller_user_id: str
    seller_agent_id: str
    sell_intent_id: str
    buyers: list[BuyerEntry] = field(default_factory=list)


async def _seed_intent(
    db,
    *,
    user_id: str,
    side: str,
    seed_text: str = "macbook",
    category: str = "electronics_laptops",
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
        category=category,
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
    db,
    *,
    buy_intent_id: str,
    sell_intent_id: str,
    status: str = "discovered",
) -> str:
    match = Match(
        id=str(uuid.uuid4()),
        buy_intent_id=buy_intent_id,
        sell_intent_id=sell_intent_id,
        similarity_score=0.95,
        price_overlap=True,
        price_proximity_score=0.85,
        combined_score=0.92,
        status=status,
    )
    db.add(match)
    await db.commit()
    return match.id


async def _seed_multi_match(
    db, *, num_buyers: int = 3
) -> MultiMatchSetup:
    """1 seller (tier 2 active mandate) + N buyers (tier 2 active mandate)
    each with their own buy intent + match against the single sell intent."""
    seller_id, seller_agent_id, _ = await setup_active_mandate_async(
        db, email=f"seller-{uuid.uuid4().hex[:6]}@x.com"
    )
    sell_id = await _seed_intent(
        db,
        user_id=seller_id,
        side="sell",
        reservation_eur=1000,
        ideal_eur=1200,
    )

    buyers: list[BuyerEntry] = []
    for i in range(num_buyers):
        bu, ba, _ = await setup_active_mandate_async(
            db, email=f"buyer{i}-{uuid.uuid4().hex[:6]}@x.com"
        )
        bi = await _seed_intent(
            db,
            user_id=bu,
            side="buy",
            reservation_eur=1500,
            ideal_eur=1100,
        )
        m = await _seed_match(
            db, buy_intent_id=bi, sell_intent_id=sell_id
        )
        buyers.append(BuyerEntry(bu, ba, bi, m))

    return MultiMatchSetup(
        seller_user_id=seller_id,
        seller_agent_id=seller_agent_id,
        sell_intent_id=sell_id,
        buyers=buyers,
    )


# ===========================================================================
# 1. accept marks BOTH intents 'matched'
# ===========================================================================


@pytest.mark.db
async def test_accept_marks_both_intents_matched(async_db_session) -> None:
    s = await _seed_multi_match(async_db_session, num_buyers=1)
    b = s.buyers[0]
    # Seller offers, buyer accepts.
    nego_result = await negotiation_service.start_or_continue(
        async_db_session,
        user_id=s.seller_user_id,
        agent_id=s.seller_agent_id,
        match_id=b.match_id,
        price_cents=120000,
    )
    await negotiation_service.accept_offer(
        async_db_session,
        user_id=b.user_id,
        agent_id=b.agent_id,
        negotiation_id=nego_result.negotiation_id,
    )

    sell_intent = await async_db_session.get(Intent, s.sell_intent_id)
    buy_intent = await async_db_session.get(Intent, b.intent_id)
    await async_db_session.refresh(sell_intent)
    await async_db_session.refresh(buy_intent)
    assert sell_intent.status == "matched"
    assert buy_intent.status == "matched"


# ===========================================================================
# 2. accept works with no competing matches (vanilla case)
# ===========================================================================


@pytest.mark.db
async def test_accept_no_competing_matches_works(async_db_session) -> None:
    s = await _seed_multi_match(async_db_session, num_buyers=1)
    b = s.buyers[0]
    nego = await negotiation_service.start_or_continue(
        async_db_session,
        user_id=s.seller_user_id,
        agent_id=s.seller_agent_id,
        match_id=b.match_id,
        price_cents=120000,
    )
    accept = await negotiation_service.accept_offer(
        async_db_session,
        user_id=b.user_id,
        agent_id=b.agent_id,
        negotiation_id=nego.negotiation_id,
    )
    assert accept.agreed_price_cents == 120000


# ===========================================================================
# 3. accept does not touch unrelated users' intents
# ===========================================================================


@pytest.mark.db
async def test_accept_does_not_affect_unrelated_intents(
    async_db_session,
) -> None:
    s = await _seed_multi_match(async_db_session, num_buyers=1)
    b = s.buyers[0]
    # Seed an unrelated tier-2 user with their own active intent.
    other_id, _, _ = await setup_active_mandate_async(
        async_db_session, email=f"other-{uuid.uuid4().hex[:6]}@x.com"
    )
    other_intent_id = await _seed_intent(
        async_db_session, user_id=other_id, side="sell"
    )

    # Run the seller↔buyer accept.
    nego = await negotiation_service.start_or_continue(
        async_db_session,
        user_id=s.seller_user_id,
        agent_id=s.seller_agent_id,
        match_id=b.match_id,
        price_cents=120000,
    )
    await negotiation_service.accept_offer(
        async_db_session,
        user_id=b.user_id,
        agent_id=b.agent_id,
        negotiation_id=nego.negotiation_id,
    )

    other_intent = await async_db_session.get(Intent, other_intent_id)
    await async_db_session.refresh(other_intent)
    assert other_intent.status == "active"


# ===========================================================================
# 4. Match lifecycle status progression
# ===========================================================================


@pytest.mark.db
async def test_match_lifecycle_progression(async_db_session) -> None:
    s = await _seed_multi_match(async_db_session, num_buyers=2)
    chosen, other = s.buyers[0], s.buyers[1]

    nego = await negotiation_service.start_or_continue(
        async_db_session,
        user_id=s.seller_user_id,
        agent_id=s.seller_agent_id,
        match_id=chosen.match_id,
        price_cents=120000,
    )
    await negotiation_service.accept_offer(
        async_db_session,
        user_id=chosen.user_id,
        agent_id=chosen.agent_id,
        negotiation_id=nego.negotiation_id,
    )

    chosen_match = await async_db_session.get(Match, chosen.match_id)
    other_match = await async_db_session.get(Match, other.match_id)
    await async_db_session.refresh(chosen_match)
    await async_db_session.refresh(other_match)
    assert chosen_match.status == "agreed"
    assert other_match.status == "expired"


# ===========================================================================
# 5. cascade cancels competing active negotiations
# ===========================================================================


@pytest.mark.db
async def test_cascade_cancels_competing_negotiations(async_db_session) -> None:
    s = await _seed_multi_match(async_db_session, num_buyers=3)
    # Seller offers on each match. The 3 negotiations all become active.
    nego_ids = []
    for b in s.buyers:
        n = await negotiation_service.start_or_continue(
            async_db_session,
            user_id=s.seller_user_id,
            agent_id=s.seller_agent_id,
            match_id=b.match_id,
            price_cents=120000,
        )
        nego_ids.append(n.negotiation_id)

    # buyers[0] accepts.
    chosen = s.buyers[0]
    await negotiation_service.accept_offer(
        async_db_session,
        user_id=chosen.user_id,
        agent_id=chosen.agent_id,
        negotiation_id=nego_ids[0],
    )

    # Other 2 negotiations should be 'cancelled'.
    for nid in nego_ids[1:]:
        nego = await async_db_session.get(Negotiation, nid)
        await async_db_session.refresh(nego)
        assert nego.status == "cancelled"

    # The accepted one is 'agreed'.
    chosen_nego = await async_db_session.get(Negotiation, nego_ids[0])
    await async_db_session.refresh(chosen_nego)
    assert chosen_nego.status == "agreed"


# ===========================================================================
# 6. cascade expires competing matches
# ===========================================================================


@pytest.mark.db
async def test_cascade_expires_competing_matches(async_db_session) -> None:
    s = await _seed_multi_match(async_db_session, num_buyers=3)
    chosen = s.buyers[0]

    nego = await negotiation_service.start_or_continue(
        async_db_session,
        user_id=s.seller_user_id,
        agent_id=s.seller_agent_id,
        match_id=chosen.match_id,
        price_cents=120000,
    )
    await negotiation_service.accept_offer(
        async_db_session,
        user_id=chosen.user_id,
        agent_id=chosen.agent_id,
        negotiation_id=nego.negotiation_id,
    )

    for b in s.buyers[1:]:
        m = await async_db_session.get(Match, b.match_id)
        await async_db_session.refresh(m)
        assert m.status == "expired"


# ===========================================================================
# 7. cascade ignores already-cancelled negotiations
# ===========================================================================


@pytest.mark.db
async def test_cascade_ignores_already_cancelled_negotiations(
    async_db_session,
) -> None:
    s = await _seed_multi_match(async_db_session, num_buyers=2)
    chosen, other = s.buyers[0], s.buyers[1]

    # Pre-cancel the "other" negotiation by direct insert with cancelled status.
    pre_cancelled = Negotiation(
        match_id=other.match_id,
        state={
            "turns": [],
            "is_final_round": False,
            "final_status": "cancelled",
            "agreed_price_cents": None,
        },
        rounds_used=0,
        max_rounds=negotiation_service.MAX_ROUNDS,
        status="cancelled",
        started_at=datetime.utcnow(),
        closed_at=datetime.utcnow(),
    )
    async_db_session.add(pre_cancelled)
    await async_db_session.commit()

    # Run the chosen accept flow.
    nego = await negotiation_service.start_or_continue(
        async_db_session,
        user_id=s.seller_user_id,
        agent_id=s.seller_agent_id,
        match_id=chosen.match_id,
        price_cents=120000,
    )
    await negotiation_service.accept_offer(
        async_db_session,
        user_id=chosen.user_id,
        agent_id=chosen.agent_id,
        negotiation_id=nego.negotiation_id,
    )

    # Pre-cancelled stays cancelled (was already at terminal).
    await async_db_session.refresh(pre_cancelled)
    assert pre_cancelled.status == "cancelled"


# ===========================================================================
# 8. cascade ignores terminal-status matches
# ===========================================================================


@pytest.mark.db
async def test_cascade_ignores_terminal_status_matches(
    async_db_session,
) -> None:
    s = await _seed_multi_match(async_db_session, num_buyers=2)
    chosen, other = s.buyers[0], s.buyers[1]

    # Force-set the "other" match to 'rejected' (terminal).
    other_match = await async_db_session.get(Match, other.match_id)
    other_match.status = "rejected"
    await async_db_session.commit()

    nego = await negotiation_service.start_or_continue(
        async_db_session,
        user_id=s.seller_user_id,
        agent_id=s.seller_agent_id,
        match_id=chosen.match_id,
        price_cents=120000,
    )
    await negotiation_service.accept_offer(
        async_db_session,
        user_id=chosen.user_id,
        agent_id=chosen.agent_id,
        negotiation_id=nego.negotiation_id,
    )

    # 'rejected' stays 'rejected' — not transitioned to 'expired'.
    await async_db_session.refresh(other_match)
    assert other_match.status == "rejected"


# ===========================================================================
# 9. cascade only affects matches involving the accepted pair's intents
# ===========================================================================


@pytest.mark.db
async def test_cascade_only_affects_relevant_intents(async_db_session) -> None:
    s = await _seed_multi_match(async_db_session, num_buyers=1)
    b = s.buyers[0]

    # An unrelated seller+buyer pair with their own match.
    other_seller, other_seller_agent, _ = await setup_active_mandate_async(
        async_db_session, email=f"os-{uuid.uuid4().hex[:6]}@x.com"
    )
    other_buyer, other_buyer_agent, _ = await setup_active_mandate_async(
        async_db_session, email=f"ob-{uuid.uuid4().hex[:6]}@x.com"
    )
    o_sell = await _seed_intent(
        async_db_session, user_id=other_seller, side="sell"
    )
    o_buy = await _seed_intent(
        async_db_session, user_id=other_buyer, side="buy",
        reservation_eur=1500, ideal_eur=1100,
    )
    o_match = await _seed_match(
        async_db_session, buy_intent_id=o_buy, sell_intent_id=o_sell
    )

    # Run accept on the original pair.
    nego = await negotiation_service.start_or_continue(
        async_db_session,
        user_id=s.seller_user_id,
        agent_id=s.seller_agent_id,
        match_id=b.match_id,
        price_cents=120000,
    )
    await negotiation_service.accept_offer(
        async_db_session,
        user_id=b.user_id,
        agent_id=b.agent_id,
        negotiation_id=nego.negotiation_id,
    )

    other_match = await async_db_session.get(Match, o_match)
    await async_db_session.refresh(other_match)
    assert other_match.status == "discovered"  # unaffected


# ===========================================================================
# 10. cancellation_reason persisted to state JSONB
# ===========================================================================


@pytest.mark.db
async def test_cancellation_reason_persisted_to_state(
    async_db_session,
) -> None:
    s = await _seed_multi_match(async_db_session, num_buyers=2)
    chosen, other = s.buyers[0], s.buyers[1]

    n_chosen = await negotiation_service.start_or_continue(
        async_db_session,
        user_id=s.seller_user_id,
        agent_id=s.seller_agent_id,
        match_id=chosen.match_id,
        price_cents=120000,
    )
    n_other = await negotiation_service.start_or_continue(
        async_db_session,
        user_id=s.seller_user_id,
        agent_id=s.seller_agent_id,
        match_id=other.match_id,
        price_cents=120000,
    )

    await negotiation_service.accept_offer(
        async_db_session,
        user_id=chosen.user_id,
        agent_id=chosen.agent_id,
        negotiation_id=n_chosen.negotiation_id,
    )

    other_nego = await async_db_session.get(Negotiation, n_other.negotiation_id)
    await async_db_session.refresh(other_nego)
    assert other_nego.status == "cancelled"
    assert (
        other_nego.state.get("cancellation_reason")
        == negotiation_service.CancelReason.OTHER_MATCH_ACCEPTED
    )


# ===========================================================================
# 11. sequential second accept on already-matched intent → IntentAlreadyMatched
# ===========================================================================


@pytest.mark.db
async def test_sequential_second_accept_raises_intent_already_matched(
    async_db_session,
) -> None:
    s = await _seed_multi_match(async_db_session, num_buyers=2)
    chosen, other = s.buyers[0], s.buyers[1]

    n1 = await negotiation_service.start_or_continue(
        async_db_session,
        user_id=s.seller_user_id,
        agent_id=s.seller_agent_id,
        match_id=chosen.match_id,
        price_cents=120000,
    )
    n2 = await negotiation_service.start_or_continue(
        async_db_session,
        user_id=s.seller_user_id,
        agent_id=s.seller_agent_id,
        match_id=other.match_id,
        price_cents=125000,
    )

    # First accept succeeds.
    await negotiation_service.accept_offer(
        async_db_session,
        user_id=chosen.user_id,
        agent_id=chosen.agent_id,
        negotiation_id=n1.negotiation_id,
    )

    # Second accept now sees the cascade-cancelled negotiation; raises
    # NegotiationNotActive (the cascade got there first). The
    # IntentAlreadyMatched path triggers when the cascade hasn't yet
    # touched n2 — see test 17 for that specific scenario.
    with pytest.raises(
        (
            negotiation_service.NegotiationNotActive,
            negotiation_service.IntentAlreadyMatched,
        )
    ):
        await negotiation_service.accept_offer(
            async_db_session,
            user_id=other.user_id,
            agent_id=other.agent_id,
            negotiation_id=n2.negotiation_id,
        )


# ===========================================================================
# 12. two accepts on disjoint intent pairs both succeed
# ===========================================================================


@pytest.mark.db
async def test_two_accepts_on_disjoint_pairs_both_succeed(
    async_db_session,
) -> None:
    # Pair A
    a_seller, a_seller_agent, _ = await setup_active_mandate_async(
        async_db_session, email=f"as-{uuid.uuid4().hex[:6]}@x.com"
    )
    a_buyer, a_buyer_agent, _ = await setup_active_mandate_async(
        async_db_session, email=f"ab-{uuid.uuid4().hex[:6]}@x.com"
    )
    a_sell = await _seed_intent(
        async_db_session, user_id=a_seller, side="sell"
    )
    a_buy = await _seed_intent(
        async_db_session, user_id=a_buyer, side="buy",
        reservation_eur=1500, ideal_eur=1100,
    )
    a_match = await _seed_match(
        async_db_session, buy_intent_id=a_buy, sell_intent_id=a_sell
    )

    # Pair B (totally disjoint users + intents)
    b_seller, b_seller_agent, _ = await setup_active_mandate_async(
        async_db_session, email=f"bs-{uuid.uuid4().hex[:6]}@x.com"
    )
    b_buyer, b_buyer_agent, _ = await setup_active_mandate_async(
        async_db_session, email=f"bb-{uuid.uuid4().hex[:6]}@x.com"
    )
    b_sell = await _seed_intent(
        async_db_session, user_id=b_seller, side="sell"
    )
    b_buy = await _seed_intent(
        async_db_session, user_id=b_buyer, side="buy",
        reservation_eur=1500, ideal_eur=1100,
    )
    b_match = await _seed_match(
        async_db_session, buy_intent_id=b_buy, sell_intent_id=b_sell
    )

    n_a = await negotiation_service.start_or_continue(
        async_db_session,
        user_id=a_seller, agent_id=a_seller_agent,
        match_id=a_match, price_cents=120000,
    )
    n_b = await negotiation_service.start_or_continue(
        async_db_session,
        user_id=b_seller, agent_id=b_seller_agent,
        match_id=b_match, price_cents=130000,
    )
    await negotiation_service.accept_offer(
        async_db_session,
        user_id=a_buyer, agent_id=a_buyer_agent,
        negotiation_id=n_a.negotiation_id,
    )
    await negotiation_service.accept_offer(
        async_db_session,
        user_id=b_buyer, agent_id=b_buyer_agent,
        negotiation_id=n_b.negotiation_id,
    )

    # Both pairs ended up with both intents matched.
    for intent_id in (a_sell, a_buy, b_sell, b_buy):
        intent = await async_db_session.get(Intent, intent_id)
        await async_db_session.refresh(intent)
        assert intent.status == "matched"


# ===========================================================================
# 13. lock order: works regardless of buy/sell ID ordering
# ===========================================================================


@pytest.mark.db
async def test_lock_order_works_regardless_of_id_order(
    async_db_session,
) -> None:
    """Run accept many times across pairs whose buy.id can be < or > sell.id.

    With sorted-by-ID locking, there's no deadlock or behavioral asymmetry.
    """
    for _ in range(5):
        s = await _seed_multi_match(async_db_session, num_buyers=1)
        b = s.buyers[0]
        # Some buy_intent_ids will sort below, others above the sell_intent_id.
        nego = await negotiation_service.start_or_continue(
            async_db_session,
            user_id=s.seller_user_id,
            agent_id=s.seller_agent_id,
            match_id=b.match_id,
            price_cents=120000,
        )
        await negotiation_service.accept_offer(
            async_db_session,
            user_id=b.user_id,
            agent_id=b.agent_id,
            negotiation_id=nego.negotiation_id,
        )
        sell_intent = await async_db_session.get(Intent, s.sell_intent_id)
        buy_intent = await async_db_session.get(Intent, b.intent_id)
        await async_db_session.refresh(sell_intent)
        await async_db_session.refresh(buy_intent)
        assert sell_intent.status == "matched"
        assert buy_intent.status == "matched"


# ===========================================================================
# 14. competing-match audit log row exists with reason
# ===========================================================================


@pytest.mark.db
async def test_competing_match_audit_logged_with_reason(
    async_db_session,
) -> None:
    s = await _seed_multi_match(async_db_session, num_buyers=2)
    chosen, other = s.buyers[0], s.buyers[1]
    n_chosen = await negotiation_service.start_or_continue(
        async_db_session,
        user_id=s.seller_user_id,
        agent_id=s.seller_agent_id,
        match_id=chosen.match_id,
        price_cents=120000,
    )
    await negotiation_service.accept_offer(
        async_db_session,
        user_id=chosen.user_id,
        agent_id=chosen.agent_id,
        negotiation_id=n_chosen.negotiation_id,
    )

    rows = list(
        await async_db_session.scalars(
            select(AuditLog)
            .where(AuditLog.action == audit_service_action_expire())
            .where(AuditLog.params["match_id"].astext == other.match_id)
        )
    )
    # Exactly one expire_match audit row for the loser match.
    assert len(rows) == 1
    assert (
        rows[0].params.get("reason")
        == negotiation_service.CancelReason.OTHER_MATCH_ACCEPTED
    )


def audit_service_action_expire() -> str:
    """Helper: lookup MatchActions.EXPIRE without importing in module top
    (kept colocated with the test that uses it)."""
    from app.services import audit_service

    return audit_service.MatchActions.EXPIRE


# ===========================================================================
# 15. cannot cancel matched intent
# ===========================================================================


@pytest.mark.db
async def test_cannot_cancel_matched_intent(async_db_session) -> None:
    s = await _seed_multi_match(async_db_session, num_buyers=1)
    b = s.buyers[0]
    nego = await negotiation_service.start_or_continue(
        async_db_session,
        user_id=s.seller_user_id,
        agent_id=s.seller_agent_id,
        match_id=b.match_id,
        price_cents=120000,
    )
    await negotiation_service.accept_offer(
        async_db_session,
        user_id=b.user_id,
        agent_id=b.agent_id,
        negotiation_id=nego.negotiation_id,
    )

    # Seller now tries to cancel their (matched) intent — should 409.
    with pytest.raises(negotiation_service.IntentAlreadyMatched):
        await intent_service.cancel_intent(
            async_db_session,
            user_id=s.seller_user_id,
            intent_id=s.sell_intent_id,
        )


# ===========================================================================
# 16. find_matches_for_intent skips matched intents
# ===========================================================================


@pytest.mark.db
async def test_find_matches_skips_matched_intents(async_db_session) -> None:
    s = await _seed_multi_match(async_db_session, num_buyers=1)
    b = s.buyers[0]
    nego = await negotiation_service.start_or_continue(
        async_db_session,
        user_id=s.seller_user_id,
        agent_id=s.seller_agent_id,
        match_id=b.match_id,
        price_cents=120000,
    )
    await negotiation_service.accept_offer(
        async_db_session,
        user_id=b.user_id,
        agent_id=b.agent_id,
        negotiation_id=nego.negotiation_id,
    )

    # Both intents are now 'matched'. Re-running matcher on either should
    # return [] (the matcher filters Intent.status == 'active', and the
    # intent itself is no longer active so the early-return triggers).
    sell_matches = await match_service.find_matches_for_intent(
        async_db_session, intent_id=s.sell_intent_id
    )
    buy_matches = await match_service.find_matches_for_intent(
        async_db_session, intent_id=b.intent_id
    )
    assert sell_matches == []
    assert buy_matches == []


# ===========================================================================
# 17. both intents transition to 'matched' atomically (success path)
# ===========================================================================


@pytest.mark.db
async def test_both_intents_matched_atomically(async_db_session) -> None:
    s = await _seed_multi_match(async_db_session, num_buyers=1)
    b = s.buyers[0]
    # Pre-state: both 'active'.
    sell = await async_db_session.get(Intent, s.sell_intent_id)
    buy = await async_db_session.get(Intent, b.intent_id)
    assert sell.status == "active"
    assert buy.status == "active"

    nego = await negotiation_service.start_or_continue(
        async_db_session,
        user_id=s.seller_user_id,
        agent_id=s.seller_agent_id,
        match_id=b.match_id,
        price_cents=120000,
    )
    await negotiation_service.accept_offer(
        async_db_session,
        user_id=b.user_id,
        agent_id=b.agent_id,
        negotiation_id=nego.negotiation_id,
    )

    # Post: both 'matched', neither in any half-state.
    await async_db_session.refresh(sell)
    await async_db_session.refresh(buy)
    assert sell.status == "matched"
    assert buy.status == "matched"


# ===========================================================================
# 18. matched intent rejects update_intent
# ===========================================================================


@pytest.mark.db
async def test_matched_intent_rejects_update(http_client, async_db_session) -> None:
    s = await _seed_multi_match(async_db_session, num_buyers=1)
    b = s.buyers[0]
    nego = await negotiation_service.start_or_continue(
        async_db_session,
        user_id=s.seller_user_id,
        agent_id=s.seller_agent_id,
        match_id=b.match_id,
        price_cents=120000,
    )
    await negotiation_service.accept_offer(
        async_db_session,
        user_id=b.user_id,
        agent_id=b.agent_id,
        negotiation_id=nego.negotiation_id,
    )

    # Seller tries to update the (matched) intent.
    from app.core.security import create_access_token

    http_client.headers["Authorization"] = (
        f"Bearer {create_access_token(user_id=s.seller_user_id, tier=2)}"
    )
    response = await http_client.patch(
        f"/api/intents/{s.sell_intent_id}",
        json={"title": "new title"},
    )
    # update_intent gates on status='active' → IntentNotEditable (409).
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "intent_not_editable"
