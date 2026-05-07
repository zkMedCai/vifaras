"""Negotiation service + API tests (brief task 5.1).

34 tests organized by concern:

  Start / continue (8):
   1. start creates new negotiation with first offer; match → negotiating
   2. continue appends a turn
   3. start rejects inactive match (cancelled)
   4. start rejects agent not party to match
   5. continue rejects when already agreed
   6. max_rounds increments across multiple turns
   7. is_final_round flag set at max-1
   8. max_rounds exceeded raises

  Accept / structured proposals (11):
   9. accept marks negotiation + match agreed
  10. accept requires tier 2 (tier 1 → 402)
  11. accept rejects own offer (must come from counterparty)
  12. accept with no offers yet raises
  13. accept response carries agreed_price + next_step
  13b. structured terms create canonical proposal snapshot + hash
  13c. counter offer carries forward canonical shipping terms
  13d. accept can pin latest proposal hash
  13e. accept rejects mismatched proposal hash
  13f. invalid terms delta is rejected

  Reject (3):
  14. reject marks negotiation + match rejected
  15. reject requires tier 1 (tier 0 → 402)
  16. reject rejects own offer

  State / list (3):
  17. get_state returns full turn history for party
  18. get_state returns 403 for non-party
  19. list filters by status + only own negotiations

  Concurrency / cascade (3):
  20. concurrent start for same match: one creates, the other appends
  21. intent cancel cascades active negotiations to cancelled
  22. agent_not_owned (passing someone else's agent_id) → 403

  Validation (2):
  23. negative price_cents → 422
  24. message > 500 chars truncated silently

  API surface (4):
  25. POST /api/negotiations happy path
  26. POST .../accept tier-gated 402 at tier 1
  27. GET /api/negotiations lists caller's negotiations only
  28. GET /api/negotiations/{id} returns full state for party
"""
from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select

from app.core.security import create_access_token
from app.models.schema import Agent, Intent, Match, Negotiation, User
from app.services import embedding_service, negotiation_service
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
# Seed helpers
# ---------------------------------------------------------------------------


@dataclass
class NegoSetup:
    """Seller (tier 2) + buyer (tier 1 by default) + sell + buy + match."""

    seller_user_id: str
    seller_agent_id: str
    buyer_user_id: str
    buyer_agent_id: str
    sell_intent_id: str
    buy_intent_id: str
    match_id: str


async def _seed_tier_1_user_with_agent(
    db, *, email: str
) -> tuple[str, str]:
    user_id = str(uuid.uuid4())
    agent_id = str(uuid.uuid4())
    now = datetime.utcnow()
    user = User(id=user_id, **default_user_kwargs(tier=1, email=email))
    db.add(user)
    await db.flush()
    agent = Agent(
        id=agent_id,
        user_id=user_id,
        name="t1",
        pubkey=f"t1-pk-{agent_id[:6]}",
        privkey_kms_ref=f"file:.secrets/agent_keys/{agent_id}.json",
        status="pending_mandate",
        created_at=now,
    )
    db.add(agent)
    await db.commit()
    return user_id, agent_id


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


async def _seed_setup(db, *, buyer_tier: int = 1) -> NegoSetup:
    """Seller tier 2 (active mandate) + buyer at requested tier."""
    seller_id, seller_agent_id, _ = await setup_active_mandate_async(
        db, email=f"sell-{uuid.uuid4().hex[:6]}@x.com"
    )
    if buyer_tier == 1:
        buyer_id, buyer_agent_id = await _seed_tier_1_user_with_agent(
            db, email=f"buy-{uuid.uuid4().hex[:6]}@x.com"
        )
    elif buyer_tier == 2:
        buyer_id, buyer_agent_id, _ = await setup_active_mandate_async(
            db, email=f"buy2-{uuid.uuid4().hex[:6]}@x.com"
        )
    else:
        raise ValueError(f"unsupported buyer_tier {buyer_tier}")

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
    return NegoSetup(
        seller_user_id=seller_id,
        seller_agent_id=seller_agent_id,
        buyer_user_id=buyer_id,
        buyer_agent_id=buyer_agent_id,
        sell_intent_id=sell_id,
        buy_intent_id=buy_id,
        match_id=match_id,
    )


def _bearer(client, user_id: str, tier: int) -> None:
    client.headers["Authorization"] = (
        f"Bearer {create_access_token(user_id=user_id, tier=tier)}"
    )


# ===========================================================================
# 1. start creates new negotiation
# ===========================================================================


@pytest.mark.db
async def test_start_creates_new_negotiation_with_first_offer(
    async_db_session,
) -> None:
    s = await _seed_setup(async_db_session)
    result = await negotiation_service.start_or_continue(
        async_db_session,
        user_id=s.seller_user_id,
        agent_id=s.seller_agent_id,
        match_id=s.match_id,
        price_cents=120000,
        message="As listed",
    )
    assert result.created_new is True
    assert result.rounds_used == 1
    assert result.last_turn["type"] == "offer"
    assert result.last_turn["price_cents"] == 120000

    nego = await async_db_session.scalar(
        select(Negotiation).where(Negotiation.id == result.negotiation_id)
    )
    assert nego.status == "active"
    match = await async_db_session.get(Match, s.match_id)
    await async_db_session.refresh(match)
    assert match.status == "negotiating"


# ===========================================================================
# 2. continue appends a turn
# ===========================================================================


@pytest.mark.db
async def test_continue_appends_turn(async_db_session) -> None:
    s = await _seed_setup(async_db_session)
    first = await negotiation_service.start_or_continue(
        async_db_session,
        user_id=s.seller_user_id,
        agent_id=s.seller_agent_id,
        match_id=s.match_id,
        price_cents=120000,
    )
    second = await negotiation_service.start_or_continue(
        async_db_session,
        user_id=s.buyer_user_id,
        agent_id=s.buyer_agent_id,
        match_id=s.match_id,
        price_cents=110000,
        message="Posso ritirare",
    )
    assert second.created_new is False
    assert second.rounds_used == 2
    assert second.last_turn["type"] == "counter_offer"
    assert second.last_turn["agent_id"] == s.buyer_agent_id


# ===========================================================================
# 3. start rejects inactive match
# ===========================================================================


@pytest.mark.db
async def test_start_rejects_inactive_match(async_db_session) -> None:
    s = await _seed_setup(async_db_session)
    # Force-cancel the match.
    match = await async_db_session.get(Match, s.match_id)
    match.status = "cancelled"
    await async_db_session.commit()

    with pytest.raises(negotiation_service.InvalidMatchState):
        await negotiation_service.start_or_continue(
            async_db_session,
            user_id=s.seller_user_id,
            agent_id=s.seller_agent_id,
            match_id=s.match_id,
            price_cents=120000,
        )


# ===========================================================================
# 4. start rejects agent not party to match
# ===========================================================================


@pytest.mark.db
async def test_start_rejects_agent_not_party_to_match(async_db_session) -> None:
    s = await _seed_setup(async_db_session)
    # An unrelated tier-1 user with their own agent — owns no intent in this match.
    outsider_user, outsider_agent = await _seed_tier_1_user_with_agent(
        async_db_session, email="outsider@x.com"
    )

    with pytest.raises(negotiation_service.AgentNotPartyToMatch):
        await negotiation_service.start_or_continue(
            async_db_session,
            user_id=outsider_user,
            agent_id=outsider_agent,
            match_id=s.match_id,
            price_cents=120000,
        )


# ===========================================================================
# 5. continue rejects when already agreed
# ===========================================================================


@pytest.mark.db
async def test_continue_rejects_when_already_agreed(async_db_session) -> None:
    s = await _seed_setup(async_db_session, buyer_tier=2)
    # Seller offers, buyer accepts.
    await negotiation_service.start_or_continue(
        async_db_session,
        user_id=s.seller_user_id,
        agent_id=s.seller_agent_id,
        match_id=s.match_id,
        price_cents=120000,
    )
    accept_result = await negotiation_service.accept_offer(
        async_db_session,
        user_id=s.buyer_user_id,
        agent_id=s.buyer_agent_id,
        negotiation_id=(
            await async_db_session.scalar(
                select(Negotiation.id).where(
                    Negotiation.match_id == s.match_id
                )
            )
        ),
    )
    assert accept_result.agreed_price_cents == 120000

    # Trying to continue must fail because match is now 'agreed'.
    with pytest.raises(negotiation_service.InvalidMatchState):
        await negotiation_service.start_or_continue(
            async_db_session,
            user_id=s.seller_user_id,
            agent_id=s.seller_agent_id,
            match_id=s.match_id,
            price_cents=125000,
        )


# ===========================================================================
# 6. max_rounds increments correctly across turns
# ===========================================================================


@pytest.mark.db
async def test_max_rounds_increments_correctly(async_db_session) -> None:
    s = await _seed_setup(async_db_session)
    # Alternate seller / buyer turns.
    pairs = [
        (s.seller_user_id, s.seller_agent_id),
        (s.buyer_user_id, s.buyer_agent_id),
    ]
    for i in range(4):
        user_id, agent_id = pairs[i % 2]
        result = await negotiation_service.start_or_continue(
            async_db_session,
            user_id=user_id,
            agent_id=agent_id,
            match_id=s.match_id,
            price_cents=100000 + i * 1000,
        )
        assert result.rounds_used == i + 1


# ===========================================================================
# 7. is_final_round flag at max-1
# ===========================================================================


@pytest.mark.db
async def test_is_final_round_flag_at_max_minus_one(async_db_session) -> None:
    s = await _seed_setup(async_db_session)
    pairs = [
        (s.seller_user_id, s.seller_agent_id),
        (s.buyer_user_id, s.buyer_agent_id),
    ]
    last_result = None
    for i in range(negotiation_service.MAX_ROUNDS - 1):
        user_id, agent_id = pairs[i % 2]
        last_result = await negotiation_service.start_or_continue(
            async_db_session,
            user_id=user_id,
            agent_id=agent_id,
            match_id=s.match_id,
            price_cents=100000,
        )
    # After MAX_ROUNDS - 1 turns: rounds_used = 5, is_final_round = True.
    assert last_result.rounds_used == negotiation_service.MAX_ROUNDS - 1
    assert last_result.is_final_round is True


# ===========================================================================
# 8. max_rounds exceeded raises
# ===========================================================================


@pytest.mark.db
async def test_max_rounds_exceeded_raises(async_db_session) -> None:
    s = await _seed_setup(async_db_session)
    pairs = [
        (s.seller_user_id, s.seller_agent_id),
        (s.buyer_user_id, s.buyer_agent_id),
    ]
    for i in range(negotiation_service.MAX_ROUNDS):
        user_id, agent_id = pairs[i % 2]
        await negotiation_service.start_or_continue(
            async_db_session,
            user_id=user_id,
            agent_id=agent_id,
            match_id=s.match_id,
            price_cents=100000,
        )
    # Round 7 → MaxRoundsReached.
    with pytest.raises(negotiation_service.MaxRoundsReached):
        await negotiation_service.start_or_continue(
            async_db_session,
            user_id=s.seller_user_id,
            agent_id=s.seller_agent_id,
            match_id=s.match_id,
            price_cents=100000,
        )


# ===========================================================================
# 9. accept marks negotiation + match agreed
# ===========================================================================


@pytest.mark.db
async def test_accept_marks_negotiation_and_match_agreed(
    async_db_session,
) -> None:
    s = await _seed_setup(async_db_session, buyer_tier=2)
    result = await negotiation_service.start_or_continue(
        async_db_session,
        user_id=s.seller_user_id,
        agent_id=s.seller_agent_id,
        match_id=s.match_id,
        price_cents=120000,
    )
    accept = await negotiation_service.accept_offer(
        async_db_session,
        user_id=s.buyer_user_id,
        agent_id=s.buyer_agent_id,
        negotiation_id=result.negotiation_id,
    )
    assert accept.agreed_price_cents == 120000

    nego = await async_db_session.get(Negotiation, result.negotiation_id)
    await async_db_session.refresh(nego)
    assert nego.status == "agreed"
    assert nego.state["final_status"] == "agreed"

    match = await async_db_session.get(Match, s.match_id)
    await async_db_session.refresh(match)
    assert match.status == "agreed"


# ===========================================================================
# 10. accept requires tier 2 (API check)
# ===========================================================================


@pytest.mark.db
async def test_accept_requires_tier_2_via_api(http_client, async_db_session) -> None:
    s = await _seed_setup(async_db_session, buyer_tier=2)
    # Seller offers (tier 2 by setup).
    _bearer(http_client, s.seller_user_id, tier=2)
    r1 = await http_client.post(
        "/api/negotiations",
        json={
            "match_id": s.match_id,
            "agent_id": s.seller_agent_id,
            "price_cents": 120000,
        },
    )
    assert r1.status_code == 200
    nego_id = r1.json()["negotiation_id"]

    # Buyer at tier 1 tries to accept → 402.
    _bearer(http_client, s.buyer_user_id, tier=1)
    r2 = await http_client.post(
        f"/api/negotiations/{nego_id}/accept",
        json={"agent_id": s.buyer_agent_id},
    )
    assert r2.status_code == 402

    # Same buyer at tier 2 → 200.
    _bearer(http_client, s.buyer_user_id, tier=2)
    r3 = await http_client.post(
        f"/api/negotiations/{nego_id}/accept",
        json={"agent_id": s.buyer_agent_id},
    )
    assert r3.status_code == 200


# ===========================================================================
# 11. accept rejects own offer
# ===========================================================================


@pytest.mark.db
async def test_accept_rejects_own_offer(async_db_session) -> None:
    s = await _seed_setup(async_db_session, buyer_tier=2)
    result = await negotiation_service.start_or_continue(
        async_db_session,
        user_id=s.seller_user_id,
        agent_id=s.seller_agent_id,
        match_id=s.match_id,
        price_cents=120000,
    )
    # Seller (whose agent made the offer) tries to accept their own offer.
    with pytest.raises(negotiation_service.CannotActOnOwnOffer):
        await negotiation_service.accept_offer(
            async_db_session,
            user_id=s.seller_user_id,
            agent_id=s.seller_agent_id,
            negotiation_id=result.negotiation_id,
        )


# ===========================================================================
# 12. accept with no offers yet raises
# ===========================================================================


@pytest.mark.db
async def test_accept_when_no_offers_yet_raises(async_db_session) -> None:
    s = await _seed_setup(async_db_session, buyer_tier=2)
    # Manually create an empty negotiation (no turns).
    nego = Negotiation(
        match_id=s.match_id,
        state={
            "turns": [],
            "is_final_round": False,
            "final_status": None,
            "agreed_price_cents": None,
        },
        rounds_used=0,
        max_rounds=negotiation_service.MAX_ROUNDS,
        status="active",
        started_at=datetime.utcnow(),
    )
    async_db_session.add(nego)
    await async_db_session.commit()

    with pytest.raises(negotiation_service.NoOfferToAccept):
        await negotiation_service.accept_offer(
            async_db_session,
            user_id=s.buyer_user_id,
            agent_id=s.buyer_agent_id,
            negotiation_id=nego.id,
        )


# ===========================================================================
# 13. accept response carries agreed_price + next_step
# ===========================================================================


@pytest.mark.db
async def test_accept_response_carries_next_step(async_db_session) -> None:
    s = await _seed_setup(async_db_session, buyer_tier=2)
    result = await negotiation_service.start_or_continue(
        async_db_session,
        user_id=s.seller_user_id,
        agent_id=s.seller_agent_id,
        match_id=s.match_id,
        price_cents=120000,
    )
    accept = await negotiation_service.accept_offer(
        async_db_session,
        user_id=s.buyer_user_id,
        agent_id=s.buyer_agent_id,
        negotiation_id=result.negotiation_id,
    )
    assert accept.next_step == "sign_deal_with_passkey"
    assert accept.match_id == s.match_id


# ===========================================================================
# 13b. structured terms create canonical proposal snapshot + hash
# ===========================================================================


@pytest.mark.db
async def test_structured_terms_create_canonical_proposal_hash(
    async_db_session,
) -> None:
    s = await _seed_setup(async_db_session)

    result = await negotiation_service.start_or_continue(
        async_db_session,
        user_id=s.seller_user_id,
        agent_id=s.seller_agent_id,
        match_id=s.match_id,
        price_cents=120000,
        message="Posso includere spedizione tracciata pagata dal buyer.",
        terms_delta={
            "shipping_required": True,
            "shipping_paid_by": "buyer",
            "shipping_method_preference": "tracked_parcel",
        },
    )

    turn = result.last_turn
    assert turn["schema_version"] == 2
    assert turn["public_message"] == "Posso includere spedizione tracciata pagata dal buyer."
    assert turn["message"] == turn["public_message"]
    assert turn["terms_delta"] == {
        "shipping_required": True,
        "shipping_paid_by": "buyer",
        "shipping_method_preference": "tracked_parcel",
    }
    assert turn["canonical_terms_snapshot"] == {
        "schema_version": 1,
        "item_price_cents": 120000,
        "currency": "EUR",
        "shipping_required": True,
        "shipping_paid_by": "buyer",
        "shipping_method_preference": "tracked_parcel",
        "tracking_required": True,
        "insurance_required": False,
    }
    assert turn["proposal_hash"].startswith("sha256:")
    assert len(turn["proposal_hash"]) == len("sha256:") + 64
    assert turn["policy_check"]["allowed"] is True


# ===========================================================================
# 13c. counter offer carries forward previous canonical shipping terms
# ===========================================================================


@pytest.mark.db
async def test_counter_offer_carries_forward_canonical_terms(
    async_db_session,
) -> None:
    s = await _seed_setup(async_db_session)

    await negotiation_service.start_or_continue(
        async_db_session,
        user_id=s.seller_user_id,
        agent_id=s.seller_agent_id,
        match_id=s.match_id,
        price_cents=120000,
        terms_delta={
            "shipping_required": True,
            "shipping_paid_by": "buyer",
            "shipping_method_preference": "tracked_parcel",
        },
    )

    counter = await negotiation_service.start_or_continue(
        async_db_session,
        user_id=s.buyer_user_id,
        agent_id=s.buyer_agent_id,
        match_id=s.match_id,
        price_cents=110000,
        message="Accetto tracciata, ma a 1100.",
    )

    terms = counter.last_turn["canonical_terms_snapshot"]
    assert terms["item_price_cents"] == 110000
    assert terms["shipping_required"] is True
    assert terms["shipping_paid_by"] == "buyer"
    assert terms["shipping_method_preference"] == "tracked_parcel"
    assert terms["tracking_required"] is True


# ===========================================================================
# 13d. accept can pin latest proposal hash
# ===========================================================================


@pytest.mark.db
async def test_accept_with_matching_proposal_hash_pins_terms(
    async_db_session,
) -> None:
    s = await _seed_setup(async_db_session, buyer_tier=2)
    result = await negotiation_service.start_or_continue(
        async_db_session,
        user_id=s.seller_user_id,
        agent_id=s.seller_agent_id,
        match_id=s.match_id,
        price_cents=120000,
        terms_delta={
            "shipping_required": True,
            "shipping_paid_by": "buyer",
            "shipping_method_preference": "tracked_parcel",
        },
    )
    proposal_hash = result.last_turn["proposal_hash"]

    accept = await negotiation_service.accept_offer(
        async_db_session,
        user_id=s.buyer_user_id,
        agent_id=s.buyer_agent_id,
        negotiation_id=result.negotiation_id,
        proposal_hash=proposal_hash,
    )

    assert accept.proposal_hash == proposal_hash
    assert accept.canonical_terms_snapshot["item_price_cents"] == 120000
    assert accept.canonical_terms_snapshot["shipping_method_preference"] == "tracked_parcel"

    nego = await async_db_session.get(Negotiation, result.negotiation_id)
    await async_db_session.refresh(nego)
    assert nego.state["accepted_proposal_hash"] == proposal_hash
    assert (
        nego.state["accepted_canonical_terms_snapshot"]["shipping_paid_by"]
        == "buyer"
    )
    assert nego.state["turns"][-1]["accepted_proposal_hash"] == proposal_hash


# ===========================================================================
# 13e. accept rejects mismatched proposal hash
# ===========================================================================


@pytest.mark.db
async def test_accept_rejects_mismatched_proposal_hash(
    async_db_session,
) -> None:
    s = await _seed_setup(async_db_session, buyer_tier=2)
    result = await negotiation_service.start_or_continue(
        async_db_session,
        user_id=s.seller_user_id,
        agent_id=s.seller_agent_id,
        match_id=s.match_id,
        price_cents=120000,
    )

    with pytest.raises(negotiation_service.ProposalHashMismatch):
        await negotiation_service.accept_offer(
            async_db_session,
            user_id=s.buyer_user_id,
            agent_id=s.buyer_agent_id,
            negotiation_id=result.negotiation_id,
            proposal_hash="sha256:" + "0" * 64,
        )


# ===========================================================================
# 13f. invalid terms delta is rejected
# ===========================================================================


@pytest.mark.db
async def test_invalid_terms_delta_is_rejected(async_db_session) -> None:
    s = await _seed_setup(async_db_session)

    with pytest.raises(negotiation_service.InvalidTermsDelta):
        await negotiation_service.start_or_continue(
            async_db_session,
            user_id=s.seller_user_id,
            agent_id=s.seller_agent_id,
            match_id=s.match_id,
            price_cents=120000,
            terms_delta={"shipping_method_preference": "drone"},
        )


# ===========================================================================
# 14. reject marks negotiation + match rejected
# ===========================================================================


@pytest.mark.db
async def test_reject_marks_negotiation_and_match_rejected(
    async_db_session,
) -> None:
    s = await _seed_setup(async_db_session)
    result = await negotiation_service.start_or_continue(
        async_db_session,
        user_id=s.seller_user_id,
        agent_id=s.seller_agent_id,
        match_id=s.match_id,
        price_cents=120000,
    )
    rj = await negotiation_service.reject_offer(
        async_db_session,
        user_id=s.buyer_user_id,
        agent_id=s.buyer_agent_id,
        negotiation_id=result.negotiation_id,
        reason="too high",
    )
    assert rj.reason == "too high"
    nego = await async_db_session.get(Negotiation, result.negotiation_id)
    await async_db_session.refresh(nego)
    assert nego.status == "rejected"
    match = await async_db_session.get(Match, s.match_id)
    await async_db_session.refresh(match)
    assert match.status == "rejected"


# ===========================================================================
# 15. reject requires tier 1 (API)
# ===========================================================================


@pytest.mark.db
async def test_reject_requires_tier_1(http_client, async_db_session) -> None:
    s = await _seed_setup(async_db_session)
    _bearer(http_client, s.seller_user_id, tier=2)
    r1 = await http_client.post(
        "/api/negotiations",
        json={
            "match_id": s.match_id,
            "agent_id": s.seller_agent_id,
            "price_cents": 120000,
        },
    )
    assert r1.status_code == 200
    nego_id = r1.json()["negotiation_id"]

    # Buyer at tier 0 → 402.
    _bearer(http_client, s.buyer_user_id, tier=0)
    r2 = await http_client.post(
        f"/api/negotiations/{nego_id}/reject",
        json={"agent_id": s.buyer_agent_id},
    )
    assert r2.status_code == 402


# ===========================================================================
# 16. reject rejects own offer
# ===========================================================================


@pytest.mark.db
async def test_reject_rejects_own_offer(async_db_session) -> None:
    s = await _seed_setup(async_db_session)
    result = await negotiation_service.start_or_continue(
        async_db_session,
        user_id=s.seller_user_id,
        agent_id=s.seller_agent_id,
        match_id=s.match_id,
        price_cents=120000,
    )
    with pytest.raises(negotiation_service.CannotActOnOwnOffer):
        await negotiation_service.reject_offer(
            async_db_session,
            user_id=s.seller_user_id,
            agent_id=s.seller_agent_id,
            negotiation_id=result.negotiation_id,
        )


# ===========================================================================
# 17. get_state returns full history for party
# ===========================================================================


@pytest.mark.db
async def test_get_state_returns_full_history_for_party(async_db_session) -> None:
    s = await _seed_setup(async_db_session)
    result = await negotiation_service.start_or_continue(
        async_db_session,
        user_id=s.seller_user_id,
        agent_id=s.seller_agent_id,
        match_id=s.match_id,
        price_cents=120000,
        message="As listed",
    )
    nego = await negotiation_service.get_negotiation_state(
        async_db_session,
        user_id=s.buyer_user_id,
        negotiation_id=result.negotiation_id,
    )
    assert len(nego.state["turns"]) == 1
    assert nego.state["turns"][0]["price_cents"] == 120000


# ===========================================================================
# 18. get_state returns 403 for non-party
# ===========================================================================


@pytest.mark.db
async def test_get_state_returns_403_for_non_party(async_db_session) -> None:
    s = await _seed_setup(async_db_session)
    result = await negotiation_service.start_or_continue(
        async_db_session,
        user_id=s.seller_user_id,
        agent_id=s.seller_agent_id,
        match_id=s.match_id,
        price_cents=120000,
    )
    outsider, _ = await _seed_tier_1_user_with_agent(
        async_db_session, email="snoop@x.com"
    )
    with pytest.raises(negotiation_service.NegotiationNotForUser):
        await negotiation_service.get_negotiation_state(
            async_db_session,
            user_id=outsider,
            negotiation_id=result.negotiation_id,
        )


# ===========================================================================
# 19. list filters by status + only own negotiations
# ===========================================================================


@pytest.mark.db
async def test_list_negotiations_only_own(async_db_session) -> None:
    s = await _seed_setup(async_db_session)
    await negotiation_service.start_or_continue(
        async_db_session,
        user_id=s.seller_user_id,
        agent_id=s.seller_agent_id,
        match_id=s.match_id,
        price_cents=120000,
    )

    # Outsider has no negotiations.
    outsider, _ = await _seed_tier_1_user_with_agent(
        async_db_session, email="other@x.com"
    )

    seller_page = await negotiation_service.list_negotiations_for_user(
        async_db_session, user_id=s.seller_user_id
    )
    outsider_page = await negotiation_service.list_negotiations_for_user(
        async_db_session, user_id=outsider
    )
    assert seller_page.total == 1
    assert outsider_page.total == 0


# ===========================================================================
# 20. concurrent start: only one creates, the other appends
# ===========================================================================


@pytest.mark.db
async def test_concurrent_start_only_one_creates(async_db_session) -> None:
    # Strict concurrency requires separate sessions on the same DB. Within
    # a single session, two awaits serialize. We assert the *invariant*
    # via the Match-row lock: a second call on the same match always sees
    # the negotiation already exists and appends a turn.
    s = await _seed_setup(async_db_session)
    first = await negotiation_service.start_or_continue(
        async_db_session,
        user_id=s.seller_user_id,
        agent_id=s.seller_agent_id,
        match_id=s.match_id,
        price_cents=120000,
    )
    second = await negotiation_service.start_or_continue(
        async_db_session,
        user_id=s.buyer_user_id,
        agent_id=s.buyer_agent_id,
        match_id=s.match_id,
        price_cents=110000,
    )
    assert first.created_new is True
    assert second.created_new is False
    # Exactly one Negotiation row exists for this match.
    rows = list(
        await async_db_session.scalars(
            select(Negotiation).where(Negotiation.match_id == s.match_id)
        )
    )
    assert len(rows) == 1


# ===========================================================================
# 21. intent cancel cascades active negotiations to cancelled
# ===========================================================================


@pytest.mark.db
async def test_intent_cancel_cascades_negotiation(async_db_session) -> None:
    from app.services import intent_service

    s = await _seed_setup(async_db_session)
    nego_result = await negotiation_service.start_or_continue(
        async_db_session,
        user_id=s.seller_user_id,
        agent_id=s.seller_agent_id,
        match_id=s.match_id,
        price_cents=120000,
    )

    await intent_service.cancel_intent(
        async_db_session,
        user_id=s.seller_user_id,
        intent_id=s.sell_intent_id,
    )
    nego = await async_db_session.get(Negotiation, nego_result.negotiation_id)
    await async_db_session.refresh(nego)
    assert nego.status == "cancelled"
    assert nego.closed_at is not None


# ===========================================================================
# 22. agent_not_owned (passing someone else's agent_id) → 403
# ===========================================================================


@pytest.mark.db
async def test_agent_not_owned_raises(async_db_session) -> None:
    s = await _seed_setup(async_db_session)
    # Seller's user passes BUYER's agent_id.
    with pytest.raises(negotiation_service.AgentNotOwned):
        await negotiation_service.start_or_continue(
            async_db_session,
            user_id=s.seller_user_id,
            agent_id=s.buyer_agent_id,
            match_id=s.match_id,
            price_cents=120000,
        )


# ===========================================================================
# 23. negative price_cents → 422
# ===========================================================================


@pytest.mark.db
async def test_negative_price_cents_raises(async_db_session) -> None:
    s = await _seed_setup(async_db_session)
    with pytest.raises(negotiation_service.InvalidPrice):
        await negotiation_service.start_or_continue(
            async_db_session,
            user_id=s.seller_user_id,
            agent_id=s.seller_agent_id,
            match_id=s.match_id,
            price_cents=-100,
        )


# ===========================================================================
# 24. message > 500 chars rejected by moderation (was: truncated silently)
# ===========================================================================


@pytest.mark.db
async def test_long_message_rejected_by_moderation(async_db_session) -> None:
    """Brief 7.1.4 swapped the V0 policy from silent truncation to hard
    rejection — `_truncate_message` is now defensive backstop only, the
    authoritative cap is enforced by `moderate_optional`."""
    from app.services.content_moderation import TooLong

    s = await _seed_setup(async_db_session)
    long_msg = "X" * 700
    with pytest.raises(TooLong) as exc:
        await negotiation_service.start_or_continue(
            async_db_session,
            user_id=s.seller_user_id,
            agent_id=s.seller_agent_id,
            match_id=s.match_id,
            price_cents=120000,
            message=long_msg,
        )
    assert exc.value.field == "message"
    assert exc.value.code == "too_long"


# ===========================================================================
# 25. POST /api/negotiations happy path
# ===========================================================================


@pytest.mark.db
async def test_post_negotiations_happy_path(http_client, async_db_session) -> None:
    s = await _seed_setup(async_db_session)
    _bearer(http_client, s.seller_user_id, tier=2)
    r = await http_client.post(
        "/api/negotiations",
        json={
            "match_id": s.match_id,
            "agent_id": s.seller_agent_id,
            "price_cents": 120000,
            "message": "first",
            "terms_delta": {
                "shipping_required": True,
                "shipping_paid_by": "buyer",
                "shipping_method_preference": "tracked_parcel",
            },
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["created_new"] is True
    assert body["rounds_used"] == 1
    assert body["last_turn"]["price_cents"] == 120000
    assert body["last_turn"]["schema_version"] == 2
    assert body["last_turn"]["proposal_hash"].startswith("sha256:")
    assert (
        body["last_turn"]["canonical_terms_snapshot"]["shipping_method_preference"]
        == "tracked_parcel"
    )


# ===========================================================================
# 26. start requires tier 1 minimum
# ===========================================================================


@pytest.mark.db
async def test_start_requires_tier_1(http_client, async_db_session) -> None:
    s = await _seed_setup(async_db_session)
    _bearer(http_client, s.seller_user_id, tier=0)
    r = await http_client.post(
        "/api/negotiations",
        json={
            "match_id": s.match_id,
            "agent_id": s.seller_agent_id,
            "price_cents": 120000,
        },
    )
    assert r.status_code == 402


# ===========================================================================
# 27. GET /api/negotiations lists caller's negotiations only
# ===========================================================================


@pytest.mark.db
async def test_list_negotiations_via_api(http_client, async_db_session) -> None:
    s = await _seed_setup(async_db_session)
    _bearer(http_client, s.seller_user_id, tier=2)
    await http_client.post(
        "/api/negotiations",
        json={
            "match_id": s.match_id,
            "agent_id": s.seller_agent_id,
            "price_cents": 120000,
        },
    )
    r = await http_client.get("/api/negotiations")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 1
    assert body["negotiations"][0]["match_id"] == s.match_id


# ===========================================================================
# 28. GET /api/negotiations/{id} returns full state for party
# ===========================================================================


@pytest.mark.db
async def test_get_negotiation_via_api(http_client, async_db_session) -> None:
    s = await _seed_setup(async_db_session)
    _bearer(http_client, s.seller_user_id, tier=2)
    r1 = await http_client.post(
        "/api/negotiations",
        json={
            "match_id": s.match_id,
            "agent_id": s.seller_agent_id,
            "price_cents": 120000,
            "message": "hello",
        },
    )
    nego_id = r1.json()["negotiation_id"]

    # Buyer at tier 1 reads the negotiation state.
    _bearer(http_client, s.buyer_user_id, tier=1)
    r2 = await http_client.get(f"/api/negotiations/{nego_id}")
    assert r2.status_code == 200
    body = r2.json()
    assert body["status"] == "active"
    assert len(body["turns"]) == 1
    assert body["turns"][0]["message"] == "hello"
    assert body["turns"][0]["proposal_hash"].startswith("sha256:")
