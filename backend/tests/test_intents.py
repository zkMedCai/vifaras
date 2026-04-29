"""Intent service + API tests (brief task 4.1).

25 tests covering CRUD of BUY/SELL intents:

  Create (8):
   1. tier-0 BUY happy path
   2. tier-0 SELL happy path
   3. embedding generated + stored on the row
   4. side='trade' rejected with 422 trade_not_yet_available
   5. BUY ideal > reservation rejected
   6. SELL ideal < reservation rejected
   7. category in HARD_FORBIDDEN rejected
   8. category not in V0_CATEGORIES rejected

  Tier limits (3):
   9. tier-0: 6th active intent → 402 too_many_active_intents
  10. tier-1: 11th active intent → 402
  11. tier-2: limit read from mandate.limits.max_active_intents

  List (3):
  12. filter by status + side
  13. paginate with limit + offset
  14. list returns only intents owned by the caller

  Update (5):
  15. tier-0 title update succeeds
  16. tier-0 reservation_price_eur update → 402
  17. tier-2 reservation_price_eur update succeeds
  18. price update during active negotiation → 409
  19. category update always rejected with 422

  Cancel (3):
  20. cancel marks status='cancelled' + closed_at
  21. cancel cascades to active negotiations + matches
  22. cancelling an already-cancelled intent is idempotent

  Embedding integration (3):
  23. create calls embedding_service (deterministic fake matches)
  24. OpenAI failure → 503 with embedding_service_unavailable
  25. cache hit on identical text avoids re-generation

The tests run with `EMBEDDING_BACKEND=fake` so embeddings are
deterministic and hermetic — no network. The cache is cleared
between tests.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select

from app.core.security import create_access_token
from app.models.schema import Agent, Intent, Mandate, Match, Negotiation, User
from app.services import embedding_service, intent_service
from tests.factories import (
    default_user_kwargs,
    setup_active_mandate_async,
)


# ---------------------------------------------------------------------------
# Module-wide fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _force_fake_embedding(monkeypatch):
    """Every test in this module uses the deterministic SHA-256 backend.

    Resets the EmbeddingService singleton so the backend env var is
    re-read on the next `get_embedding_service()` call, and ensures cache
    state doesn't leak between tests.
    """
    monkeypatch.setenv("EMBEDDING_BACKEND", "fake")
    embedding_service._reset_singleton_for_tests()
    yield
    embedding_service._reset_singleton_for_tests()


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


async def _seed_tier_0_or_1_user(
    db, *, tier: int, email: str | None = None
) -> str:
    user_id = str(uuid.uuid4())
    email = email or f"u-{user_id[:8]}@example.com"
    user = User(id=user_id, **default_user_kwargs(tier=tier, email=email))
    db.add(user)
    await db.commit()
    return user_id


async def _seed_tier_2_user(
    db,
    *,
    email: str | None = None,
    mandate_max_active_intents: int | None = None,
) -> str:
    """tier-2 = User + Agent + active Mandate. Optional limit override."""
    email = email or f"u2-{uuid.uuid4().hex[:8]}@example.com"
    user_id, _agent_id, _mandate_id = await setup_active_mandate_async(
        db, email=email
    )
    if mandate_max_active_intents is not None:
        mandate = await db.scalar(
            select(Mandate).where(Mandate.user_id == user_id)
        )
        limits = dict(mandate.limits)
        limits["max_active_intents"] = mandate_max_active_intents
        mandate.limits = limits
        await db.commit()
    return user_id


def _bearer(client, user_id: str, tier: int) -> None:
    client.headers["Authorization"] = (
        f"Bearer {create_access_token(user_id=user_id, tier=tier)}"
    )


def _valid_create_body(**overrides: Any) -> dict[str, Any]:
    body = {
        "side": "sell",
        "title": "MacBook Pro 14 M3",
        "description": "Usato 6 mesi, condizioni perfette, scatola originale.",
        "category": "electronics_laptops",
        "reservation_price_eur": 1200.0,
        "ideal_price_eur": 1400.0,
        "duration_days": 14,
        "hard_constraints": {"location": "Roma, IT"},
        "soft_preferences": {},
    }
    body.update(overrides)
    return body


async def _create_intent_directly(
    db, *, user_id: str, side: str = "sell", status: str = "active",
    title: str | None = None,
) -> str:
    """Bypass the API: insert an Intent row for setup-heavy tests.

    Used by tier-limit and list tests where the API path is the system
    under test for *creation* but bulk-creating dozens of intents through
    HTTP would just slow the suite down for no extra coverage.
    """
    intent_id = str(uuid.uuid4())
    now = datetime.utcnow()
    intent = Intent(
        id=intent_id,
        user_id=user_id,
        agent_id=None,
        side=side,
        title=title or f"seed-{intent_id[:6]}",
        description="seeded directly",
        category="misc_other",
        description_embedding=embedding_service._fake_embedding(
            f"seed-{intent_id}"
        ),
        reservation_price_cents=10000,
        ideal_price_cents=12000 if side == "sell" else 8000,
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


# ===========================================================================
# 1. tier-0 BUY happy path
# ===========================================================================


@pytest.mark.db
async def test_create_buy_intent_tier_0_succeeds(
    http_client, async_db_session
) -> None:
    user_id = await _seed_tier_0_or_1_user(async_db_session, tier=0)
    _bearer(http_client, user_id, tier=0)

    response = await http_client.post(
        "/api/intents",
        json=_valid_create_body(
            side="buy",
            title="Cerco MacBook Pro 14",
            reservation_price_eur=1500.0,  # cap (BUY: ideal <= reservation)
            ideal_price_eur=1200.0,
        ),
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "active"
    assert body["embedding_generated"] is True

    intent = await async_db_session.scalar(
        select(Intent).where(Intent.id == body["intent_id"])
    )
    assert intent is not None
    assert intent.user_id == user_id
    assert intent.side == "buy"
    assert intent.agent_id is None  # tier-0 has no agent
    assert intent.reservation_price_cents == 150_000
    assert intent.ideal_price_cents == 120_000


# ===========================================================================
# 2. tier-0 SELL happy path
# ===========================================================================


@pytest.mark.db
async def test_create_sell_intent_tier_0_succeeds(
    http_client, async_db_session
) -> None:
    user_id = await _seed_tier_0_or_1_user(async_db_session, tier=0)
    _bearer(http_client, user_id, tier=0)

    response = await http_client.post("/api/intents", json=_valid_create_body())
    assert response.status_code == 201, response.text
    body = response.json()

    intent = await async_db_session.scalar(
        select(Intent).where(Intent.id == body["intent_id"])
    )
    assert intent.side == "sell"
    # SELL: ideal > reservation
    assert intent.ideal_price_cents > intent.reservation_price_cents


# ===========================================================================
# 3. embedding generated + stored
# ===========================================================================


@pytest.mark.db
async def test_create_intent_generates_embedding(
    http_client, async_db_session
) -> None:
    user_id = await _seed_tier_0_or_1_user(async_db_session, tier=0)
    _bearer(http_client, user_id, tier=0)

    body = _valid_create_body()
    response = await http_client.post("/api/intents", json=body)
    assert response.status_code == 201

    intent = await async_db_session.scalar(
        select(Intent).where(Intent.id == response.json()["intent_id"])
    )
    expected_text = embedding_service.build_embedding_text(
        title=body["title"], description=body["description"]
    )
    expected_embedding = embedding_service._fake_embedding(expected_text)
    assert intent.description_embedding is not None
    # pgvector returns numpy array — compare elementwise
    stored = list(intent.description_embedding)
    assert len(stored) == embedding_service.EMBEDDING_DIM
    assert pytest.approx(stored[0], rel=1e-6) == expected_embedding[0]
    assert pytest.approx(stored[-1], rel=1e-6) == expected_embedding[-1]


# ===========================================================================
# 4. side='trade' rejected
# ===========================================================================


@pytest.mark.db
async def test_create_intent_rejects_trade_side(
    http_client, async_db_session
) -> None:
    user_id = await _seed_tier_0_or_1_user(async_db_session, tier=0)
    _bearer(http_client, user_id, tier=0)

    response = await http_client.post(
        "/api/intents", json=_valid_create_body(side="trade")
    )
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["code"] == "trade_not_yet_available"


# ===========================================================================
# 5. BUY: ideal > reservation rejected
# ===========================================================================


@pytest.mark.db
async def test_create_intent_validates_price_relationship_buy(
    http_client, async_db_session
) -> None:
    user_id = await _seed_tier_0_or_1_user(async_db_session, tier=0)
    _bearer(http_client, user_id, tier=0)

    response = await http_client.post(
        "/api/intents",
        json=_valid_create_body(
            side="buy",
            reservation_price_eur=1000,
            ideal_price_eur=1500,  # bad: ideal > reservation for BUY
        ),
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "invalid_price_relationship"


# ===========================================================================
# 6. SELL: ideal < reservation rejected
# ===========================================================================


@pytest.mark.db
async def test_create_intent_validates_price_relationship_sell(
    http_client, async_db_session
) -> None:
    user_id = await _seed_tier_0_or_1_user(async_db_session, tier=0)
    _bearer(http_client, user_id, tier=0)

    response = await http_client.post(
        "/api/intents",
        json=_valid_create_body(
            side="sell",
            reservation_price_eur=1500,
            ideal_price_eur=1000,  # bad: ideal < reservation for SELL
        ),
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "invalid_price_relationship"


# ===========================================================================
# 7. forbidden category rejected
# ===========================================================================


@pytest.mark.db
async def test_create_intent_rejects_forbidden_category(
    http_client, async_db_session
) -> None:
    user_id = await _seed_tier_0_or_1_user(async_db_session, tier=0)
    _bearer(http_client, user_id, tier=0)

    response = await http_client.post(
        "/api/intents", json=_valid_create_body(category="weapons")
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "category_forbidden"


# ===========================================================================
# 8. unknown category rejected
# ===========================================================================


@pytest.mark.db
async def test_create_intent_rejects_invalid_category(
    http_client, async_db_session
) -> None:
    user_id = await _seed_tier_0_or_1_user(async_db_session, tier=0)
    _bearer(http_client, user_id, tier=0)

    response = await http_client.post(
        "/api/intents", json=_valid_create_body(category="not_in_list_xyz")
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "category_not_allowed"


# ===========================================================================
# 9. tier-0: 6th intent → 402
# ===========================================================================


@pytest.mark.db
async def test_tier_0_max_5_active_intents(
    http_client, async_db_session
) -> None:
    user_id = await _seed_tier_0_or_1_user(async_db_session, tier=0)
    for _ in range(5):
        await _create_intent_directly(async_db_session, user_id=user_id)

    _bearer(http_client, user_id, tier=0)
    response = await http_client.post("/api/intents", json=_valid_create_body())
    assert response.status_code == 402
    detail = response.json()["detail"]
    assert detail["code"] == "too_many_active_intents"
    assert detail["next_step"]["action"] == "upgrade_tier"


# ===========================================================================
# 10. tier-1: 11th intent → 402
# ===========================================================================


@pytest.mark.db
async def test_tier_1_max_10_active_intents(
    http_client, async_db_session
) -> None:
    user_id = await _seed_tier_0_or_1_user(async_db_session, tier=1)
    for _ in range(10):
        await _create_intent_directly(async_db_session, user_id=user_id)

    _bearer(http_client, user_id, tier=1)
    response = await http_client.post("/api/intents", json=_valid_create_body())
    assert response.status_code == 402
    assert response.json()["detail"]["code"] == "too_many_active_intents"


# ===========================================================================
# 11. tier-2: cap from mandate
# ===========================================================================


@pytest.mark.db
async def test_tier_2_uses_mandate_limits(
    http_client, async_db_session
) -> None:
    user_id = await _seed_tier_2_user(
        async_db_session, mandate_max_active_intents=2
    )
    for _ in range(2):
        await _create_intent_directly(async_db_session, user_id=user_id)

    _bearer(http_client, user_id, tier=2)
    response = await http_client.post("/api/intents", json=_valid_create_body())
    assert response.status_code == 402
    assert response.json()["detail"]["code"] == "too_many_active_intents"


# ===========================================================================
# 12. list: filter by status + side
# ===========================================================================


@pytest.mark.db
async def test_list_intents_filters_by_status_and_side(
    http_client, async_db_session
) -> None:
    user_id = await _seed_tier_0_or_1_user(async_db_session, tier=0)
    # 2 active buy, 1 active sell, 1 cancelled buy
    await _create_intent_directly(async_db_session, user_id=user_id, side="buy")
    await _create_intent_directly(async_db_session, user_id=user_id, side="buy")
    await _create_intent_directly(async_db_session, user_id=user_id, side="sell")
    await _create_intent_directly(
        async_db_session, user_id=user_id, side="buy", status="cancelled"
    )

    _bearer(http_client, user_id, tier=0)

    response = await http_client.get(
        "/api/intents", params={"status": "active", "side": "buy"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 2
    assert all(i["side"] == "buy" and i["status"] == "active" for i in body["intents"])


# ===========================================================================
# 13. list: paginate
# ===========================================================================


@pytest.mark.db
async def test_list_intents_paginates_correctly(
    http_client, async_db_session
) -> None:
    user_id = await _seed_tier_0_or_1_user(async_db_session, tier=1)
    for _ in range(7):
        await _create_intent_directly(async_db_session, user_id=user_id)

    _bearer(http_client, user_id, tier=1)
    response = await http_client.get(
        "/api/intents", params={"limit": 3, "offset": 2}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 7
    assert body["limit"] == 3
    assert body["offset"] == 2
    assert len(body["intents"]) == 3


# ===========================================================================
# 14. list: only owned
# ===========================================================================


@pytest.mark.db
async def test_list_intents_only_returns_user_owned(
    http_client, async_db_session
) -> None:
    user_a = await _seed_tier_0_or_1_user(
        async_db_session, tier=0, email="a@example.com"
    )
    user_b = await _seed_tier_0_or_1_user(
        async_db_session, tier=0, email="b@example.com"
    )
    a_id = await _create_intent_directly(async_db_session, user_id=user_a)
    await _create_intent_directly(async_db_session, user_id=user_b)
    await _create_intent_directly(async_db_session, user_id=user_b)

    _bearer(http_client, user_a, tier=0)
    response = await http_client.get("/api/intents")
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["intents"][0]["intent_id"] == a_id


# ===========================================================================
# 15. update title at tier 0
# ===========================================================================


@pytest.mark.db
async def test_update_title_tier_0_succeeds(
    http_client, async_db_session
) -> None:
    user_id = await _seed_tier_0_or_1_user(async_db_session, tier=0)
    intent_id = await _create_intent_directly(async_db_session, user_id=user_id)

    _bearer(http_client, user_id, tier=0)
    response = await http_client.patch(
        f"/api/intents/{intent_id}", json={"title": "Aggiornato"}
    )
    assert response.status_code == 200
    assert response.json()["title"] == "Aggiornato"


# ===========================================================================
# 16. update reservation_price at tier 0 → 402
# ===========================================================================


@pytest.mark.db
async def test_update_reservation_price_tier_0_fails_402(
    http_client, async_db_session
) -> None:
    user_id = await _seed_tier_0_or_1_user(async_db_session, tier=0)
    intent_id = await _create_intent_directly(async_db_session, user_id=user_id)

    _bearer(http_client, user_id, tier=0)
    response = await http_client.patch(
        f"/api/intents/{intent_id}",
        json={"reservation_price_eur": 200.0},
    )
    assert response.status_code == 402
    assert response.json()["detail"]["code"] == "tier_too_low_for_price_update"


# ===========================================================================
# 17. update reservation_price at tier 2 succeeds
# ===========================================================================


@pytest.mark.db
async def test_update_reservation_price_tier_2_succeeds(
    http_client, async_db_session
) -> None:
    user_id = await _seed_tier_2_user(async_db_session)
    intent_id = await _create_intent_directly(
        async_db_session, user_id=user_id, side="sell"
    )
    # SELL: ideal_price > reservation_price; current row has reservation=100,
    # ideal=120. We raise reservation to 110 (still < 120) and assert OK.
    _bearer(http_client, user_id, tier=2)
    response = await http_client.patch(
        f"/api/intents/{intent_id}",
        json={"reservation_price_eur": 110.0},
    )
    assert response.status_code == 200, response.text
    assert response.json()["reservation_price_eur"] == 110.0


# ===========================================================================
# 18. update price during active negotiation → 409
# ===========================================================================


@pytest.mark.db
async def test_update_reservation_price_during_active_negotiation_fails_409(
    http_client, async_db_session
) -> None:
    user_id = await _seed_tier_2_user(async_db_session)
    intent_id = await _create_intent_directly(
        async_db_session, user_id=user_id, side="sell"
    )
    # Seed an opposite-side intent + match + active negotiation that
    # references our intent.
    other_user = await _seed_tier_0_or_1_user(
        async_db_session, tier=1, email="opp@example.com"
    )
    other_intent_id = await _create_intent_directly(
        async_db_session, user_id=other_user, side="buy"
    )
    match = Match(
        id=str(uuid.uuid4()),
        buy_intent_id=other_intent_id,
        sell_intent_id=intent_id,
        similarity_score=0.95,
        price_overlap=True,
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
        current_price_cents=11000,
        status="active",
    )
    async_db_session.add(nego)
    await async_db_session.commit()

    _bearer(http_client, user_id, tier=2)
    response = await http_client.patch(
        f"/api/intents/{intent_id}",
        json={"reservation_price_eur": 110.0},
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "intent_in_active_negotiation"


# ===========================================================================
# 19. update category always rejected
# ===========================================================================


@pytest.mark.db
async def test_update_category_always_fails_422(
    http_client, async_db_session
) -> None:
    user_id = await _seed_tier_2_user(async_db_session)
    intent_id = await _create_intent_directly(async_db_session, user_id=user_id)

    _bearer(http_client, user_id, tier=2)
    response = await http_client.patch(
        f"/api/intents/{intent_id}", json={"category": "fashion_clothing"}
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "category_not_modifiable"


# ===========================================================================
# 20. cancel marks status='cancelled'
# ===========================================================================


@pytest.mark.db
async def test_delete_intent_marks_cancelled(
    http_client, async_db_session
) -> None:
    user_id = await _seed_tier_0_or_1_user(async_db_session, tier=0)
    intent_id = await _create_intent_directly(async_db_session, user_id=user_id)

    _bearer(http_client, user_id, tier=0)
    response = await http_client.delete(f"/api/intents/{intent_id}")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "cancelled"
    assert body["already_cancelled"] is False

    intent = await async_db_session.scalar(
        select(Intent).where(Intent.id == intent_id)
    )
    assert intent.status == "cancelled"
    assert intent.closed_at is not None


# ===========================================================================
# 21. cancel cascades to active negotiations + matches
# ===========================================================================


@pytest.mark.db
async def test_delete_intent_cancels_active_negotiations(
    http_client, async_db_session
) -> None:
    user_id = await _seed_tier_0_or_1_user(async_db_session, tier=0)
    intent_id = await _create_intent_directly(
        async_db_session, user_id=user_id, side="sell"
    )
    other_user = await _seed_tier_0_or_1_user(
        async_db_session, tier=1, email="opp2@example.com"
    )
    other_intent_id = await _create_intent_directly(
        async_db_session, user_id=other_user, side="buy"
    )
    match = Match(
        id=str(uuid.uuid4()),
        buy_intent_id=other_intent_id,
        sell_intent_id=intent_id,
        similarity_score=0.9,
        price_overlap=True,
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
        current_price_cents=11000,
        status="active",
    )
    async_db_session.add(nego)
    await async_db_session.commit()

    _bearer(http_client, user_id, tier=0)
    response = await http_client.delete(f"/api/intents/{intent_id}")
    assert response.status_code == 200
    body = response.json()
    assert body["negotiations_cancelled"] == 1
    assert body["matches_expired"] == 1

    # The API call mutated the rows via its own session; the test session's
    # identity map still holds the pre-cancel snapshot. `refresh()` re-issues
    # the SELECT under the proper async greenlet context.
    await async_db_session.refresh(nego)
    await async_db_session.refresh(match)
    assert nego.status == "cancelled"
    assert match.status == "expired"


# ===========================================================================
# 22. cancel idempotent
# ===========================================================================


@pytest.mark.db
async def test_delete_already_cancelled_is_idempotent(
    http_client, async_db_session
) -> None:
    user_id = await _seed_tier_0_or_1_user(async_db_session, tier=0)
    intent_id = await _create_intent_directly(async_db_session, user_id=user_id)

    _bearer(http_client, user_id, tier=0)
    first = await http_client.delete(f"/api/intents/{intent_id}")
    assert first.status_code == 200
    assert first.json()["already_cancelled"] is False

    second = await http_client.delete(f"/api/intents/{intent_id}")
    assert second.status_code == 200
    assert second.json()["already_cancelled"] is True


# ===========================================================================
# 23. create calls embedding_service
# ===========================================================================


@pytest.mark.db
async def test_create_intent_calls_embedding_service(
    http_client, async_db_session, monkeypatch
) -> None:
    user_id = await _seed_tier_0_or_1_user(async_db_session, tier=0)
    _bearer(http_client, user_id, tier=0)

    calls: list[str] = []
    real_generate = embedding_service.generate_embedding

    async def spy(text: str):
        calls.append(text)
        return await real_generate(text)

    monkeypatch.setattr(embedding_service, "generate_embedding", spy)

    body = _valid_create_body()
    response = await http_client.post("/api/intents", json=body)
    assert response.status_code == 201
    expected_text = embedding_service.build_embedding_text(
        title=body["title"], description=body["description"]
    )
    assert calls == [expected_text]


# ===========================================================================
# 24. embedding failure → 503
# ===========================================================================


@pytest.mark.db
async def test_create_intent_handles_embedding_failure_503(
    http_client, async_db_session, monkeypatch
) -> None:
    user_id = await _seed_tier_0_or_1_user(async_db_session, tier=0)
    _bearer(http_client, user_id, tier=0)

    async def boom(text: str):
        raise embedding_service.EmbeddingServiceUnavailable("OpenAI down")

    monkeypatch.setattr(embedding_service, "generate_embedding", boom)

    response = await http_client.post("/api/intents", json=_valid_create_body())
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "embedding_service_unavailable"

    # No row persisted
    rows = list(
        await async_db_session.scalars(
            select(Intent).where(Intent.user_id == user_id)
        )
    )
    assert rows == []


# ===========================================================================
# 25. cache hit on identical text
# ===========================================================================


@pytest.mark.db
async def test_create_intent_uses_cache_for_identical_text(
    http_client, async_db_session
) -> None:
    user_id = await _seed_tier_0_or_1_user(async_db_session, tier=0)
    _bearer(http_client, user_id, tier=0)

    body = _valid_create_body(title="Identical", description="Same text")

    r1 = await http_client.post("/api/intents", json=body)
    assert r1.status_code == 201
    cache = embedding_service.get_embedding_service().cache
    key = embedding_service._hash_text(
        embedding_service.build_embedding_text(
            title=body["title"], description=body["description"]
        )
    )
    assert key in cache
    size_after_first = len(cache)

    r2 = await http_client.post("/api/intents", json=body)
    assert r2.status_code == 201
    # Same text → no new cache entry; hit count grows.
    assert len(cache) == size_after_first
    stats = cache.stats()
    assert stats["hits"] >= 1
