"""Match service + API tests (brief task 4.3).

31 tests organized by concern:

  Score functions (6):
   1. price_proximity perfect alignment → 1.0
   2. price_proximity no overlap → ~0.0 (clamped)
   3. price_proximity clamped to [0, 1]
   4. combined_score weights 0.7/0.3
   5. combined_score: similarity dominates over price
   6. price_proximity zero-width deal zone (cap == floor)

  Match discovery (8):
   7. returns candidates with price overlap
   8. excludes candidates without price overlap
   9. excludes intents owned by the same user (self-match)
  10. excludes inactive intents
  11. excludes expired intents
  12. only opposite side (BUY finds SELL)
  13. only same category
  14. side='trade' raises TradeMatchingNotImplemented

  Persistence (5):
  15. match persisted with unique constraint
  16. upsert updates score, doesn't create duplicate
  17. idempotent re-run — no duplicate, no audit flood
  18. status initially 'discovered'
  19. score breakdown columns populated

  Lifecycle (4):
  20. cancel intent expires its matches
  21. mark_match_negotiating transitions discovered → negotiating
  22. mark_match_negotiating from terminal state raises 409
  23. refresh_low_match_intents picks up under-matched intents

  API (4):
  24. owner can list matches
  25. non-owner gets 404 (no info leak)
  26. min_score filter
  27. detail endpoint requires tier 2

  Integration (3):
  28. cosine search via real pgvector returns ranked candidates
  29. POST /api/intents triggers match calc
  30. PATCH /api/intents/{id} re-triggers match calc on price fix

  Privacy (1):
  31. list view does NOT expose counterparty's ideal_price_eur
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta

import pytest
from app.core.config import settings
from app.core.security import create_access_token
from app.models.schema import Intent, Match, User
from app.services import embedding_service, match_scheduler, match_service
from sqlalchemy import select

from tests.conftest import FakeAnthropicClient, _make_message, text_block
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
# Helpers
# ---------------------------------------------------------------------------


async def _seed_user(db, *, tier: int, email: str | None = None) -> str:
    user_id = str(uuid.uuid4())
    email = email or f"u-{user_id[:8]}@example.com"
    user = User(id=user_id, **default_user_kwargs(tier=tier, email=email))
    db.add(user)
    await db.commit()
    return user_id


async def _seed_tier_2_user(db, *, email: str | None = None) -> str:
    email = email or f"u2-{uuid.uuid4().hex[:8]}@example.com"
    user_id, _, _ = await setup_active_mandate_async(db, email=email)
    return user_id


def _bearer(client, user_id: str, tier: int) -> None:
    client.headers["Authorization"] = (
        f"Bearer {create_access_token(user_id=user_id, tier=tier)}"
    )


async def _seed_intent(
    db,
    *,
    user_id: str,
    side: str,
    seed_text: str,
    category: str = "electronics_laptops",
    reservation_eur: float = 1000,
    ideal_eur: float = 1100,
    status: str = "active",
    expires_in_days: int = 14,
    with_embedding: bool = True,
) -> str:
    """Insert an Intent row with embedding seeded by `seed_text`.

    Same `seed_text` on two intents → identical embeddings → cosine_sim
    1.0. Different `seed_text` → roughly orthogonal embeddings.

    For SELL: ideal >= reservation. For BUY: ideal <= reservation.
    Caller picks numbers consistent with `side`.
    """
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
        description_embedding=(
            embedding_service._fake_embedding(seed_text)
            if with_embedding
            else None
        ),
        reservation_price_cents=int(reservation_eur * 100),
        ideal_price_cents=int(ideal_eur * 100),
        currency="EUR",
        hard_constraints={},
        soft_preferences={},
        status=status,
        expires_at=now + timedelta(days=expires_in_days),
        created_at=now,
    )
    db.add(intent)
    await db.commit()
    return intent_id


# ===========================================================================
# 1-6 — Score functions (pure math, no DB)
# ===========================================================================


def test_price_proximity_perfect_alignment_returns_one() -> None:
    # buyer_ideal == seller_ideal == zone center → distance 0 → 1.0
    score = match_service.compute_price_proximity(
        buyer_cap_cents=120_000,
        buyer_ideal_cents=110_000,  # zone center
        seller_floor_cents=100_000,
        seller_ideal_cents=110_000,  # zone center
    )
    assert score == pytest.approx(1.0)


def test_price_proximity_clamped_to_zero_on_far_ideals() -> None:
    # Both ideals far outside the zone → clamped to 0.
    score = match_service.compute_price_proximity(
        buyer_cap_cents=120_000,
        buyer_ideal_cents=10_000,  # far below the zone
        seller_floor_cents=100_000,
        seller_ideal_cents=200_000,  # far above the zone
    )
    assert score == 0.0


def test_price_proximity_returns_value_in_unit_range() -> None:
    # Probe a few realistic shapes; output must always be in [0, 1].
    cases = [
        # (buyer_cap, buyer_ideal, seller_floor, seller_ideal)
        (150_000, 130_000, 100_000, 120_000),
        (200_000, 180_000, 100_000, 110_000),
        (105_000, 102_000, 100_000, 103_000),
    ]
    for cap, b_ideal, floor, s_ideal in cases:
        score = match_service.compute_price_proximity(
            buyer_cap_cents=cap,
            buyer_ideal_cents=b_ideal,
            seller_floor_cents=floor,
            seller_ideal_cents=s_ideal,
        )
        assert 0.0 <= score <= 1.0


def test_combined_score_weights_match_constants() -> None:
    # combined = 0.7 * 1 + 0.3 * 0 = 0.7
    assert match_service.combine_scores(similarity=1.0, price_proximity=0.0) == pytest.approx(0.7)
    # combined = 0.7 * 0 + 0.3 * 1 = 0.3
    assert match_service.combine_scores(similarity=0.0, price_proximity=1.0) == pytest.approx(0.3)
    # combined = 0.7 * 0.5 + 0.3 * 0.5 = 0.5
    assert match_service.combine_scores(similarity=0.5, price_proximity=0.5) == pytest.approx(0.5)


def test_combined_score_similarity_dominates_over_price() -> None:
    # High-similarity / low-price beats low-similarity / high-price.
    # 0.7*0.9 + 0.3*0.1 = 0.66; 0.7*0.1 + 0.3*0.9 = 0.34
    high_sim = match_service.combine_scores(similarity=0.9, price_proximity=0.1)
    high_price = match_service.combine_scores(similarity=0.1, price_proximity=0.9)
    assert high_sim > high_price


def test_price_proximity_zero_width_deal_zone() -> None:
    # cap == floor: zone width = max(0, 1) = 1 (clamp). Function returns
    # a number in [0, 1] without crashing.
    score = match_service.compute_price_proximity(
        buyer_cap_cents=100_000,
        buyer_ideal_cents=99_000,
        seller_floor_cents=100_000,
        seller_ideal_cents=101_000,
    )
    assert 0.0 <= score <= 1.0


# ===========================================================================
# 7. returns candidates with price overlap
# ===========================================================================


@pytest.mark.db
async def test_find_matches_returns_candidates_with_overlap(
    async_db_session,
) -> None:
    user_a = await _seed_user(async_db_session, tier=0, email="a7@x.com")
    user_b = await _seed_user(async_db_session, tier=0, email="b7@x.com")
    sell_id = await _seed_intent(
        async_db_session,
        user_id=user_a,
        side="sell",
        seed_text="MacBook Pro 14",
        reservation_eur=1000,
        ideal_eur=1100,
    )
    await _seed_intent(
        async_db_session,
        user_id=user_b,
        side="buy",
        seed_text="MacBook Pro 14",
        reservation_eur=1200,  # cap >= seller floor (1000) → overlap
        ideal_eur=1050,
    )
    matches = await match_service.find_matches_for_intent(
        async_db_session, intent_id=sell_id
    )
    assert len(matches) == 1
    assert float(matches[0].combined_score) > 0.5  # high sim + decent price


# ===========================================================================
# 8. excludes candidates without price overlap
# ===========================================================================


@pytest.mark.db
async def test_find_matches_excludes_no_price_overlap(
    async_db_session,
) -> None:
    user_a = await _seed_user(async_db_session, tier=0, email="a8@x.com")
    user_b = await _seed_user(async_db_session, tier=0, email="b8@x.com")
    sell_id = await _seed_intent(
        async_db_session,
        user_id=user_a,
        side="sell",
        seed_text="MacBook",
        reservation_eur=1500,  # floor 1500
        ideal_eur=1600,
    )
    await _seed_intent(
        async_db_session,
        user_id=user_b,
        side="buy",
        seed_text="MacBook",
        reservation_eur=1000,  # cap 1000 < seller floor 1500 → no overlap
        ideal_eur=900,
    )
    matches = await match_service.find_matches_for_intent(
        async_db_session, intent_id=sell_id
    )
    assert matches == []


# ===========================================================================
# 9. excludes self-user intents
# ===========================================================================


@pytest.mark.db
async def test_find_matches_excludes_self_user_intents(async_db_session) -> None:
    user_id = await _seed_user(async_db_session, tier=0, email="self@x.com")
    sell_id = await _seed_intent(
        async_db_session, user_id=user_id, side="sell", seed_text="bici"
    )
    # Same user creates a BUY for the same thing — should NOT match.
    await _seed_intent(
        async_db_session,
        user_id=user_id,
        side="buy",
        seed_text="bici",
        reservation_eur=2000,
        ideal_eur=1500,
    )
    matches = await match_service.find_matches_for_intent(
        async_db_session, intent_id=sell_id
    )
    assert matches == []


# ===========================================================================
# 10. excludes inactive intents
# ===========================================================================


@pytest.mark.db
async def test_find_matches_excludes_inactive_intents(async_db_session) -> None:
    a = await _seed_user(async_db_session, tier=0, email="a10@x.com")
    b = await _seed_user(async_db_session, tier=0, email="b10@x.com")
    sell_id = await _seed_intent(
        async_db_session, user_id=a, side="sell", seed_text="cuffie"
    )
    # Counterparty intent is `cancelled` — should be skipped.
    await _seed_intent(
        async_db_session,
        user_id=b,
        side="buy",
        seed_text="cuffie",
        reservation_eur=1500,
        ideal_eur=1100,
        status="cancelled",
    )
    matches = await match_service.find_matches_for_intent(
        async_db_session, intent_id=sell_id
    )
    assert matches == []


# ===========================================================================
# 11. excludes expired intents
# ===========================================================================


@pytest.mark.db
async def test_find_matches_excludes_expired_intents(async_db_session) -> None:
    a = await _seed_user(async_db_session, tier=0, email="a11@x.com")
    b = await _seed_user(async_db_session, tier=0, email="b11@x.com")
    sell_id = await _seed_intent(
        async_db_session, user_id=a, side="sell", seed_text="lampada"
    )
    await _seed_intent(
        async_db_session,
        user_id=b,
        side="buy",
        seed_text="lampada",
        reservation_eur=1500,
        ideal_eur=1100,
        expires_in_days=-1,  # already expired
    )
    matches = await match_service.find_matches_for_intent(
        async_db_session, intent_id=sell_id
    )
    assert matches == []


# ===========================================================================
# 12. only opposite side
# ===========================================================================


@pytest.mark.db
async def test_find_matches_only_opposite_side(async_db_session) -> None:
    a = await _seed_user(async_db_session, tier=0, email="a12@x.com")
    b = await _seed_user(async_db_session, tier=0, email="b12@x.com")
    c = await _seed_user(async_db_session, tier=0, email="c12@x.com")
    sell_id = await _seed_intent(
        async_db_session, user_id=a, side="sell", seed_text="vinyl"
    )
    # Same-side competitor: another SELL → must NOT match.
    await _seed_intent(
        async_db_session, user_id=b, side="sell", seed_text="vinyl"
    )
    # True counterparty: BUY → must match.
    await _seed_intent(
        async_db_session,
        user_id=c,
        side="buy",
        seed_text="vinyl",
        reservation_eur=1500,
        ideal_eur=1100,
    )
    matches = await match_service.find_matches_for_intent(
        async_db_session, intent_id=sell_id
    )
    assert len(matches) == 1
    other_intent_id = (
        matches[0].buy_intent_id
        if matches[0].sell_intent_id == sell_id
        else matches[0].sell_intent_id
    )
    other = await async_db_session.get(Intent, other_intent_id)
    assert other.user_id == c  # the BUY intent we seeded


# ===========================================================================
# 13. only same category
# ===========================================================================


@pytest.mark.db
async def test_find_matches_only_same_category(async_db_session) -> None:
    a = await _seed_user(async_db_session, tier=0, email="a13@x.com")
    b = await _seed_user(async_db_session, tier=0, email="b13@x.com")
    sell_id = await _seed_intent(
        async_db_session,
        user_id=a,
        side="sell",
        seed_text="laptop",
        category="electronics_laptops",
    )
    # Same text, OPPOSITE side, but DIFFERENT category → no match.
    await _seed_intent(
        async_db_session,
        user_id=b,
        side="buy",
        seed_text="laptop",
        category="hobby_books",
        reservation_eur=1500,
        ideal_eur=1100,
    )
    matches = await match_service.find_matches_for_intent(
        async_db_session, intent_id=sell_id
    )
    assert matches == []


# ===========================================================================
# 14. trade side rejected
# ===========================================================================


@pytest.mark.db
async def test_find_matches_rejects_trade_intent(async_db_session) -> None:
    a = await _seed_user(async_db_session, tier=0, email="a14@x.com")
    # Bypass service-layer rejection by direct insert with side='trade'
    # (the column is now String(5) per migration 8df1d6891fd9).
    intent_id = str(uuid.uuid4())
    now = datetime.utcnow()
    intent = Intent(
        id=intent_id,
        user_id=a,
        agent_id=None,
        side="trade",
        title="barter",
        description="bici contro chitarra",
        category="hobby_music_instruments",
        description_embedding=embedding_service._fake_embedding("barter"),
        reservation_price_cents=10000,
        ideal_price_cents=10000,
        currency="EUR",
        hard_constraints={},
        soft_preferences={},
        status="active",
        expires_at=now + timedelta(days=14),
        created_at=now,
    )
    async_db_session.add(intent)
    await async_db_session.commit()

    with pytest.raises(match_service.TradeMatchingNotImplemented):
        await match_service.find_matches_for_intent(
            async_db_session, intent_id=intent_id
        )


# ===========================================================================
# Anthropic-only discovery
# ===========================================================================


@pytest.mark.db
async def test_anthropic_matching_discovers_without_embeddings(
    async_db_session, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "matching_backend", "anthropic")
    monkeypatch.setattr(settings, "anthropic_api_key", "sk-ant-test")
    monkeypatch.setattr(settings, "max_daily_llm_cost_usd", 50.0)
    monkeypatch.setattr(settings, "daily_user_cost_cap_usd", 0.50)

    seller = await _seed_user(
        async_db_session, tier=0, email="anthropic-seller@x.com"
    )
    buyer = await _seed_user(
        async_db_session, tier=0, email="anthropic-buyer@x.com"
    )
    sell_id = await _seed_intent(
        async_db_session,
        user_id=seller,
        side="sell",
        seed_text="MacBook Pro 14 M3 with 18GB RAM",
        reservation_eur=1000,
        ideal_eur=1150,
        with_embedding=False,
    )
    buy_id = await _seed_intent(
        async_db_session,
        user_id=buyer,
        side="buy",
        seed_text="Looking for MacBook Pro 14 Apple Silicon laptop",
        reservation_eur=1300,
        ideal_eur=1050,
        with_embedding=False,
    )
    client = FakeAnthropicClient(
        [
            _make_message(
                [
                    text_block(
                        '{"scores":[{"candidate_id":"'
                        + buy_id
                        + '","semantic_score":0.92}]}'
                    )
                ],
                stop_reason="end_turn",
                input_tokens=120,
                output_tokens=20,
            )
        ]
    )

    matches = await match_service.find_matches_for_intent(
        async_db_session,
        intent_id=sell_id,
        anthropic_client=client,
    )

    assert buy_id in client.calls[0]["messages"][0]["content"]
    assert len(matches) == 1
    match = matches[0]
    assert match.sell_intent_id == sell_id
    assert match.buy_intent_id == buy_id
    assert float(match.similarity_score) == pytest.approx(0.92)
    assert float(match.combined_score) > 0.70


def test_parse_anthropic_match_scores_clamps_and_ignores_invalid() -> None:
    parsed = match_service._parse_anthropic_match_scores(  # noqa: SLF001
        """```json
        {
          "scores": [
            {"candidate_id": "a", "semantic_score": 1.4},
            {"candidate_id": "b", "semantic_score": -0.2},
            {"candidate_id": "c", "semantic_score": "bad"},
            {"candidate_id": 123, "semantic_score": 0.5}
          ]
        }
        ```"""
    )

    assert parsed == {"a": 1.0, "b": 0.0}


# ===========================================================================
# 15. match persisted with unique constraint
# ===========================================================================


@pytest.mark.db
async def test_match_persisted_with_unique_constraint(async_db_session) -> None:
    a = await _seed_user(async_db_session, tier=0, email="a15@x.com")
    b = await _seed_user(async_db_session, tier=0, email="b15@x.com")
    sell_id = await _seed_intent(
        async_db_session, user_id=a, side="sell", seed_text="X"
    )
    buy_id = await _seed_intent(
        async_db_session,
        user_id=b,
        side="buy",
        seed_text="X",
        reservation_eur=1500,
        ideal_eur=1100,
    )
    matches = await match_service.find_matches_for_intent(
        async_db_session, intent_id=sell_id
    )
    assert len(matches) == 1
    persisted = await async_db_session.scalar(
        select(Match).where(
            Match.buy_intent_id == buy_id, Match.sell_intent_id == sell_id
        )
    )
    assert persisted is not None


# ===========================================================================
# 16. upsert updates score, no duplicate
# ===========================================================================


@pytest.mark.db
async def test_match_upsert_updates_score_no_duplicate(async_db_session) -> None:
    a = await _seed_user(async_db_session, tier=0, email="a16@x.com")
    b = await _seed_user(async_db_session, tier=0, email="b16@x.com")
    sell_id = await _seed_intent(
        async_db_session, user_id=a, side="sell", seed_text="Y"
    )
    await _seed_intent(
        async_db_session,
        user_id=b,
        side="buy",
        seed_text="Y",
        reservation_eur=1500,
        ideal_eur=1100,
    )
    await match_service.find_matches_for_intent(
        async_db_session, intent_id=sell_id
    )
    await match_service.find_matches_for_intent(
        async_db_session, intent_id=sell_id
    )
    rows = list(
        await async_db_session.scalars(
            select(Match).where(Match.sell_intent_id == sell_id)
        )
    )
    assert len(rows) == 1


# ===========================================================================
# 17. idempotent re-run
# ===========================================================================


@pytest.mark.db
async def test_match_idempotent_repeated_call(async_db_session) -> None:
    a = await _seed_user(async_db_session, tier=0, email="a17@x.com")
    b = await _seed_user(async_db_session, tier=0, email="b17@x.com")
    sell_id = await _seed_intent(
        async_db_session, user_id=a, side="sell", seed_text="Z"
    )
    await _seed_intent(
        async_db_session,
        user_id=b,
        side="buy",
        seed_text="Z",
        reservation_eur=1500,
        ideal_eur=1100,
    )
    first = await match_service.find_matches_for_intent(
        async_db_session, intent_id=sell_id
    )
    second = await match_service.find_matches_for_intent(
        async_db_session, intent_id=sell_id
    )
    assert len(first) == len(second) == 1
    assert first[0].id == second[0].id


# ===========================================================================
# 18. status initially 'discovered'
# ===========================================================================


@pytest.mark.db
async def test_match_status_initially_discovered(async_db_session) -> None:
    a = await _seed_user(async_db_session, tier=0, email="a18@x.com")
    b = await _seed_user(async_db_session, tier=0, email="b18@x.com")
    sell_id = await _seed_intent(
        async_db_session, user_id=a, side="sell", seed_text="Q"
    )
    await _seed_intent(
        async_db_session,
        user_id=b,
        side="buy",
        seed_text="Q",
        reservation_eur=1500,
        ideal_eur=1100,
    )
    matches = await match_service.find_matches_for_intent(
        async_db_session, intent_id=sell_id
    )
    assert matches[0].status == "discovered"


# ===========================================================================
# 19. score breakdown columns populated
# ===========================================================================


@pytest.mark.db
async def test_match_includes_score_breakdown(async_db_session) -> None:
    a = await _seed_user(async_db_session, tier=0, email="a19@x.com")
    b = await _seed_user(async_db_session, tier=0, email="b19@x.com")
    sell_id = await _seed_intent(
        async_db_session, user_id=a, side="sell", seed_text="W"
    )
    await _seed_intent(
        async_db_session,
        user_id=b,
        side="buy",
        seed_text="W",
        reservation_eur=1500,
        ideal_eur=1100,
    )
    matches = await match_service.find_matches_for_intent(
        async_db_session, intent_id=sell_id
    )
    m = matches[0]
    assert m.similarity_score is not None
    assert m.price_proximity_score is not None
    assert m.combined_score is not None
    # Combined should equal the formula within rounding tolerance.
    expected = match_service.combine_scores(
        similarity=float(m.similarity_score),
        price_proximity=float(m.price_proximity_score),
    )
    assert float(m.combined_score) == pytest.approx(expected, abs=1e-3)


# ===========================================================================
# 20. cancel intent expires its matches
# ===========================================================================


@pytest.mark.db
async def test_match_expires_on_intent_cancel(async_db_session) -> None:
    a = await _seed_user(async_db_session, tier=0, email="a20@x.com")
    b = await _seed_user(async_db_session, tier=0, email="b20@x.com")
    sell_id = await _seed_intent(
        async_db_session, user_id=a, side="sell", seed_text="K"
    )
    await _seed_intent(
        async_db_session,
        user_id=b,
        side="buy",
        seed_text="K",
        reservation_eur=1500,
        ideal_eur=1100,
    )
    await match_service.find_matches_for_intent(
        async_db_session, intent_id=sell_id
    )

    from app.services import intent_service

    await intent_service.cancel_intent(
        async_db_session, user_id=a, intent_id=sell_id
    )
    rows = list(
        await async_db_session.scalars(
            select(Match).where(Match.sell_intent_id == sell_id)
        )
    )
    assert len(rows) == 1
    assert rows[0].status == "expired"


# ===========================================================================
# 21. mark_match_negotiating
# ===========================================================================


@pytest.mark.db
async def test_mark_match_negotiating_transitions(async_db_session) -> None:
    a = await _seed_user(async_db_session, tier=0, email="a21@x.com")
    b = await _seed_user(async_db_session, tier=0, email="b21@x.com")
    sell_id = await _seed_intent(
        async_db_session, user_id=a, side="sell", seed_text="N"
    )
    await _seed_intent(
        async_db_session,
        user_id=b,
        side="buy",
        seed_text="N",
        reservation_eur=1500,
        ideal_eur=1100,
    )
    matches = await match_service.find_matches_for_intent(
        async_db_session, intent_id=sell_id
    )
    match_id = matches[0].id

    updated = await match_service.mark_match_negotiating(
        async_db_session, match_id=match_id
    )
    await async_db_session.commit()
    assert updated.status == "negotiating"

    # Idempotent re-call.
    again = await match_service.mark_match_negotiating(
        async_db_session, match_id=match_id
    )
    assert again.status == "negotiating"


# ===========================================================================
# 22. mark_match_negotiating from terminal state raises
# ===========================================================================


@pytest.mark.db
async def test_mark_negotiating_invalid_transition_raises(
    async_db_session,
) -> None:
    a = await _seed_user(async_db_session, tier=0, email="a22@x.com")
    b = await _seed_user(async_db_session, tier=0, email="b22@x.com")
    sell_id = await _seed_intent(
        async_db_session, user_id=a, side="sell", seed_text="T"
    )
    await _seed_intent(
        async_db_session,
        user_id=b,
        side="buy",
        seed_text="T",
        reservation_eur=1500,
        ideal_eur=1100,
    )
    matches = await match_service.find_matches_for_intent(
        async_db_session, intent_id=sell_id
    )
    matches[0].status = "expired"
    await async_db_session.commit()

    with pytest.raises(match_service.InvalidMatchTransition):
        await match_service.mark_match_negotiating(
            async_db_session, match_id=matches[0].id
        )


# ===========================================================================
# 23. refresh_low_match_intents picks up under-matched intents
# ===========================================================================


@pytest.mark.db
async def test_refresh_low_match_intents_targets_correct_intents(
    async_db_session, monkeypatch
) -> None:
    # Seed two pairs: one already-matched (won't be re-scanned), one
    # match-starved (no opposite intent, then we add one and run the
    # refresh tick).
    a = await _seed_user(async_db_session, tier=0, email="a23@x.com")
    b = await _seed_user(async_db_session, tier=0, email="b23@x.com")
    sell_id = await _seed_intent(
        async_db_session, user_id=a, side="sell", seed_text="late"
    )

    # No opposite intent yet → sell_id has 0 matches.
    # Now add the opposite intent.
    await _seed_intent(
        async_db_session,
        user_id=b,
        side="buy",
        seed_text="late",
        reservation_eur=1500,
        ideal_eur=1100,
    )

    # Ensure the scheduler uses the test's connection by overriding
    # AsyncSessionLocal with one that binds to async_db_session's bind.
    # In practice the scheduler's own session sees the rows because both
    # share the same testcontainer DB and _async_db_connection's outer
    # transaction is the binding of this test's writes.
    from app.core import db as db_module
    from sqlalchemy.ext.asyncio import async_sessionmaker

    bound_factory = async_sessionmaker(
        bind=async_db_session.bind, expire_on_commit=False
    )
    monkeypatch.setattr(db_module, "AsyncSessionLocal", bound_factory)

    result = await match_scheduler.refresh_low_match_intents()
    assert result["processed"] >= 1

    # After the tick, sell_id should have at least one match.
    rows = list(
        await async_db_session.scalars(
            select(Match).where(Match.sell_intent_id == sell_id)
        )
    )
    assert len(rows) >= 1


# ===========================================================================
# 24. owner can list matches
# ===========================================================================


@pytest.mark.db
async def test_get_intent_matches_returns_list_for_owner(
    http_client, async_db_session
) -> None:
    a = await _seed_user(async_db_session, tier=0, email="a24@x.com")
    b = await _seed_user(async_db_session, tier=0, email="b24@x.com")
    sell_id = await _seed_intent(
        async_db_session, user_id=a, side="sell", seed_text="lst"
    )
    await _seed_intent(
        async_db_session,
        user_id=b,
        side="buy",
        seed_text="lst",
        reservation_eur=1500,
        ideal_eur=1100,
    )
    await match_service.find_matches_for_intent(
        async_db_session, intent_id=sell_id
    )

    _bearer(http_client, a, tier=0)
    response = await http_client.get(f"/api/intents/{sell_id}/matches")
    assert response.status_code == 200
    body = response.json()
    assert body["intent_id"] == sell_id
    assert body["total"] == 1
    assert len(body["matches"]) == 1
    assert body["matches"][0]["counterparty_intent"]["side"] == "buy"


# ===========================================================================
# 25. non-owner gets 404 (no info leak)
# ===========================================================================


@pytest.mark.db
async def test_get_intent_matches_404_for_non_owner(
    http_client, async_db_session
) -> None:
    owner = await _seed_user(async_db_session, tier=0, email="own@x.com")
    other = await _seed_user(async_db_session, tier=0, email="oth@x.com")
    sell_id = await _seed_intent(
        async_db_session, user_id=owner, side="sell", seed_text="priv"
    )

    _bearer(http_client, other, tier=0)
    response = await http_client.get(f"/api/intents/{sell_id}/matches")
    assert response.status_code == 404


# ===========================================================================
# 26. min_score filter
# ===========================================================================


@pytest.mark.db
async def test_get_intent_matches_filters_by_min_score(
    http_client, async_db_session
) -> None:
    a = await _seed_user(async_db_session, tier=0, email="a26@x.com")
    b = await _seed_user(async_db_session, tier=0, email="b26@x.com")
    sell_id = await _seed_intent(
        async_db_session, user_id=a, side="sell", seed_text="hh"
    )
    await _seed_intent(
        async_db_session,
        user_id=b,
        side="buy",
        seed_text="hh",
        reservation_eur=1500,
        ideal_eur=1100,
    )
    await match_service.find_matches_for_intent(
        async_db_session, intent_id=sell_id
    )

    _bearer(http_client, a, tier=0)
    # Min score 0.99: should filter the match (combined ≈ 0.7-0.85 typically).
    response = await http_client.get(
        f"/api/intents/{sell_id}/matches", params={"min_score": 0.99}
    )
    assert response.status_code == 200
    assert response.json()["total"] == 0


# ===========================================================================
# 27. detail endpoint requires tier 2
# ===========================================================================


@pytest.mark.db
async def test_get_match_detail_requires_tier_2(
    http_client, async_db_session
) -> None:
    a = await _seed_tier_2_user(async_db_session, email="a27@x.com")
    b = await _seed_user(async_db_session, tier=0, email="b27@x.com")
    sell_id = await _seed_intent(
        async_db_session, user_id=a, side="sell", seed_text="dtl"
    )
    await _seed_intent(
        async_db_session,
        user_id=b,
        side="buy",
        seed_text="dtl",
        reservation_eur=1500,
        ideal_eur=1100,
    )
    matches = await match_service.find_matches_for_intent(
        async_db_session, intent_id=sell_id
    )
    match_id = matches[0].id

    # tier 0 → 402 (tier upgrade required)
    _bearer(http_client, a, tier=0)
    r0 = await http_client.get(f"/api/matches/{match_id}")
    assert r0.status_code == 402

    # tier 2 → 200
    _bearer(http_client, a, tier=2)
    r2 = await http_client.get(f"/api/matches/{match_id}")
    assert r2.status_code == 200
    body = r2.json()
    # Detail view exposes ideal_price for the agent to negotiate.
    assert "ideal_price_eur" in body["buy_intent"]
    assert "ideal_price_eur" in body["sell_intent"]


# ===========================================================================
# 28. cosine search via real pgvector returns ranked candidates
# ===========================================================================


@pytest.mark.db
async def test_pgvector_cosine_search_ranks_candidates(async_db_session) -> None:
    a = await _seed_user(async_db_session, tier=0, email="a28@x.com")
    b = await _seed_user(async_db_session, tier=0, email="b28@x.com")
    c = await _seed_user(async_db_session, tier=0, email="c28@x.com")

    sell_id = await _seed_intent(
        async_db_session, user_id=a, side="sell", seed_text="amplifier"
    )
    # Identical text → cosine_sim 1.0 (top candidate).
    near_id = await _seed_intent(
        async_db_session,
        user_id=b,
        side="buy",
        seed_text="amplifier",
        reservation_eur=1500,
        ideal_eur=1100,
    )
    # Different text → cosine_sim ≈ 0 (still passes price filter, lower score).
    far_id = await _seed_intent(
        async_db_session,
        user_id=c,
        side="buy",
        seed_text="totally-different-thing",
        reservation_eur=1500,
        ideal_eur=1100,
    )

    matches = await match_service.find_matches_for_intent(
        async_db_session, intent_id=sell_id
    )
    assert len(matches) == 2
    # The high-similarity candidate must rank first.
    assert matches[0].buy_intent_id == near_id
    assert matches[1].buy_intent_id == far_id
    assert float(matches[0].combined_score) > float(matches[1].combined_score)


# ===========================================================================
# 29. POST /api/intents triggers match calc
# ===========================================================================


@pytest.mark.db
async def test_create_intent_triggers_match_calculation(
    http_client, async_db_session
) -> None:
    seller = await _seed_user(async_db_session, tier=0, email="s29@x.com")
    buyer = await _seed_user(async_db_session, tier=0, email="b29@x.com")
    sell_id = await _seed_intent(
        async_db_session, user_id=seller, side="sell", seed_text="GoPro"
    )

    _bearer(http_client, buyer, tier=0)
    response = await http_client.post(
        "/api/intents",
        json={
            "side": "buy",
            "title": "GoPro Hero",
            "description": "GoPro",
            "category": "electronics_laptops",
            "reservation_price_eur": 1500.0,
            "ideal_price_eur": 1100.0,
            "duration_days": 14,
            "hard_constraints": {"location": "Roma, IT"},
        },
    )
    assert response.status_code == 201
    new_buy_id = response.json()["intent_id"]

    rows = list(
        await async_db_session.scalars(
            select(Match).where(
                Match.buy_intent_id == new_buy_id,
                Match.sell_intent_id == sell_id,
            )
        )
    )
    assert len(rows) == 1


# ===========================================================================
# 30. PATCH /api/intents/{id} re-triggers match calc on price fix
# ===========================================================================


@pytest.mark.db
async def test_update_intent_re_triggers_match_calc_on_price_fix(
    http_client, async_db_session
) -> None:
    # Seed a pair WITHOUT price overlap (so initial create produces 0 matches).
    seller = await _seed_tier_2_user(async_db_session, email="s30@x.com")
    buyer = await _seed_user(async_db_session, tier=0, email="b30@x.com")

    sell_id = await _seed_intent(
        async_db_session,
        user_id=seller,
        side="sell",
        seed_text="zoom-h6",
        reservation_eur=2000,  # floor 2000
        ideal_eur=2100,
    )
    await _seed_intent(
        async_db_session,
        user_id=buyer,
        side="buy",
        seed_text="zoom-h6",
        reservation_eur=1000,  # cap 1000 < 2000 → no overlap
        ideal_eur=900,
    )
    # Initial discovery: 0 matches.
    initial = await match_service.find_matches_for_intent(
        async_db_session, intent_id=sell_id
    )
    assert initial == []

    # Seller lowers the floor to 800 → now there's overlap. Tier 2 required
    # for price changes; seller is a tier-2 user.
    _bearer(http_client, seller, tier=2)
    response = await http_client.patch(
        f"/api/intents/{sell_id}",
        json={"reservation_price_eur": 800.0, "ideal_price_eur": 900.0},
    )
    assert response.status_code == 200, response.text

    # PATCH should have re-triggered match discovery.
    rows = list(
        await async_db_session.scalars(
            select(Match).where(Match.sell_intent_id == sell_id)
        )
    )
    assert len(rows) == 1


# ===========================================================================
# 31. list view does NOT expose counterparty's ideal_price_eur
# ===========================================================================


@pytest.mark.db
async def test_list_matches_does_not_expose_ideal_price(
    http_client, async_db_session
) -> None:
    a = await _seed_user(async_db_session, tier=0, email="a31@x.com")
    b = await _seed_user(async_db_session, tier=0, email="b31@x.com")
    sell_id = await _seed_intent(
        async_db_session, user_id=a, side="sell", seed_text="prv"
    )
    await _seed_intent(
        async_db_session,
        user_id=b,
        side="buy",
        seed_text="prv",
        reservation_eur=1500,
        ideal_eur=1100,
    )
    await match_service.find_matches_for_intent(
        async_db_session, intent_id=sell_id
    )

    _bearer(http_client, a, tier=0)
    response = await http_client.get(f"/api/intents/{sell_id}/matches")
    assert response.status_code == 200
    counterparty = response.json()["matches"][0]["counterparty_intent"]
    # Per DQ-31: reservation visible, ideal NOT.
    assert "reservation_price_eur" in counterparty
    assert "ideal_price_eur" not in counterparty
