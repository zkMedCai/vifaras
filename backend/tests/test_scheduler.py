"""Agent scheduler tests (brief task 6.3.c).

22 tests organised by concern:

  Discovery (6):
   1. eligible agent with active negotiation surfaces as candidate
   2. eligible agent with pending deal (unsigned) surfaces
   3. eligible agent with stale active intent surfaces
   4. inactive agent excluded
   5. revoked-mandate agent excluded
   6. cooldown'd agent excluded

  Ranking (3):
   7. deal_pending highest score among V0 signals
   8. recent tick penalises priority
   9. candidates returned sorted by priority desc

  Rate limiting (5):
  10. concurrent cap blocks beyond max_concurrent
  11. release frees a concurrent slot
  12. per-minute cap rejects without taking semaphore
  13. minute window slides when entries age out
  14. invalid constructor args rejected

  Dispatch (4):
  15. discover_and_dispatch_ticks calls orchestrator.run_tick per candidate
  16. tick exception swallowed by _run_tick_safely + semaphore released
  17. rate limit hit stops further dispatch and reports remaining
  18. daily cost cap short-circuits dispatch

  Cost (2):
  19. _upsert_daily_cost UPSERT increments existing row
  20. get_today_cost_usd returns 0 when no row exists

  Integration (2):
  21. start_scheduler is idempotent / disabled by settings
  22. shutdown_scheduler clears singletons
"""
from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.orchestrator import TickResult, _upsert_daily_cost
from app.core.config import settings
from app.core.rate_limiter import TickRateLimiter
from app.models.schema import (
    Agent,
    DailyCostTracking,
    Deal,
    Intent,
    Match,
    Negotiation,
)
from app.services import agent_scheduler
from app.services.agent_scheduler import (
    TickCandidate,
    compute_priority_score,
    discover_and_dispatch_ticks,
    discover_tick_candidates,
    get_today_cost_usd,
)
from app.services import embedding_service
from tests.factories import setup_active_mandate_async


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _force_fake_embedding(monkeypatch):
    monkeypatch.setenv("EMBEDDING_BACKEND", "fake")
    embedding_service._reset_singleton_for_tests()
    yield
    embedding_service._reset_singleton_for_tests()


@pytest.fixture(autouse=True)
def _reset_scheduler_singletons():
    agent_scheduler._reset_singletons_for_tests()
    yield
    agent_scheduler._reset_singletons_for_tests()


@pytest.fixture
def patch_async_session(monkeypatch, _async_db_connection):
    """Make `agent_scheduler.AsyncSessionLocal()` yield sessions bound to
    the test outer-transaction connection."""

    @asynccontextmanager
    async def _factory():
        async with AsyncSession(
            bind=_async_db_connection,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        ) as session:
            yield session

    monkeypatch.setattr(agent_scheduler, "AsyncSessionLocal", _factory)
    return _factory


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


async def _seed_agent(
    db: AsyncSession,
    *,
    status: str = "active",
    last_tick_at: datetime | None = None,
) -> tuple[str, str, str]:
    user_id, agent_id, mandate_id = await setup_active_mandate_async(
        db, email=f"sch-{uuid.uuid4().hex[:6]}@x.com"
    )
    agent = await db.get(Agent, agent_id)
    if status != "active":
        agent.status = status
    if last_tick_at is not None:
        agent.last_tick_at = last_tick_at
    if status != "active" or last_tick_at is not None:
        await db.commit()
    return user_id, agent_id, mandate_id


async def _seed_intent(
    db: AsyncSession, *, user_id: str, status: str = "active"
) -> str:
    intent_id = str(uuid.uuid4())
    now = datetime.utcnow()
    db.add(
        Intent(
            id=intent_id,
            user_id=user_id,
            agent_id=None,
            side="buy",
            title=f"intent-{intent_id[:6]}",
            description="laptop",
            category="electronics_laptops",
            description_embedding=embedding_service._fake_embedding("laptop"),
            reservation_price_cents=120000,
            ideal_price_cents=100000,
            currency="EUR",
            hard_constraints={},
            soft_preferences={},
            status=status,
            expires_at=now + timedelta(days=14),
            created_at=now,
        )
    )
    await db.commit()
    return intent_id


async def _seed_negotiation(
    db: AsyncSession,
    *,
    buyer_user_id: str,
    seller_user_id: str,
    status: str = "active",
) -> str:
    """Create the chain intent_buy + intent_sell + match + negotiation."""
    buy_id = await _seed_intent(db, user_id=buyer_user_id)
    sell_id = await _seed_intent(db, user_id=seller_user_id)
    match_id = str(uuid.uuid4())
    db.add(
        Match(
            id=match_id,
            buy_intent_id=buy_id,
            sell_intent_id=sell_id,
            similarity_score=0.9,
            price_overlap=True,
            price_proximity_score=0.85,
            combined_score=0.9,
            status="discovered",
        )
    )
    await db.commit()
    neg_id = str(uuid.uuid4())
    db.add(
        Negotiation(
            id=neg_id,
            match_id=match_id,
            state=[],
            rounds_used=0,
            max_rounds=6,
            current_price_cents=100000,
            status=status,
            started_at=datetime.utcnow(),
        )
    )
    await db.commit()
    return neg_id


async def _seed_deal(
    db: AsyncSession,
    *,
    buyer_user_id: str,
    seller_user_id: str,
    buyer_signed: bool = False,
    seller_signed: bool = False,
    status: str = "pending_signatures",
) -> str:
    """Inject a Deal row directly. Bypasses deal_service for speed."""
    buy_id = await _seed_intent(db, user_id=buyer_user_id)
    sell_id = await _seed_intent(db, user_id=seller_user_id)
    match_id = str(uuid.uuid4())
    db.add(
        Match(
            id=match_id,
            buy_intent_id=buy_id,
            sell_intent_id=sell_id,
            similarity_score=0.9,
            price_overlap=True,
            price_proximity_score=0.85,
            combined_score=0.9,
            status="agreed",
        )
    )
    await db.flush()
    neg_id = str(uuid.uuid4())
    db.add(
        Negotiation(
            id=neg_id,
            match_id=match_id,
            state=[],
            rounds_used=1,
            max_rounds=6,
            current_price_cents=100000,
            status="agreed",
            started_at=datetime.utcnow(),
        )
    )
    await db.flush()

    deal_id = str(uuid.uuid4())
    now = datetime.utcnow()
    db.add(
        Deal(
            id=deal_id,
            negotiation_id=neg_id,
            buyer_user_id=buyer_user_id,
            seller_user_id=seller_user_id,
            buy_intent_id=buy_id,
            sell_intent_id=sell_id,
            agreed_price_cents=100000,
            currency="EUR",
            buyer_signed_at=(now if buyer_signed else None),
            seller_signed_at=(now if seller_signed else None),
            status=status,
            created_at=now,
            expires_at=now + timedelta(hours=24),
            idempotency_key=str(uuid.uuid4()),
        )
    )
    await db.commit()
    return deal_id


# ===========================================================================
# Discovery
# ===========================================================================


@pytest.mark.db
async def test_discovery_surfaces_agent_with_active_negotiation(
    async_db_session, patch_async_session
):
    buyer_uid, buyer_aid, _ = await _seed_agent(async_db_session)
    seller_uid, seller_aid, _ = await _seed_agent(async_db_session)
    await _seed_negotiation(
        async_db_session,
        buyer_user_id=buyer_uid,
        seller_user_id=seller_uid,
    )

    candidates = await discover_tick_candidates(async_db_session)

    aids = {c.agent_id for c in candidates}
    assert buyer_aid in aids
    assert seller_aid in aids
    for c in candidates:
        if c.agent_id == buyer_aid:
            assert "negotiation_active" in c.work_signals


@pytest.mark.db
async def test_discovery_surfaces_agent_with_pending_unsigned_deal(
    async_db_session, patch_async_session
):
    buyer_uid, buyer_aid, _ = await _seed_agent(async_db_session)
    seller_uid, seller_aid, _ = await _seed_agent(async_db_session)
    await _seed_deal(
        async_db_session,
        buyer_user_id=buyer_uid,
        seller_user_id=seller_uid,
        buyer_signed=False,
        seller_signed=True,
    )

    candidates = await discover_tick_candidates(async_db_session)

    by_id = {c.agent_id: c for c in candidates}
    # Buyer hasn't signed → must surface with deal_pending_signature.
    assert buyer_aid in by_id
    assert "deal_pending_signature" in by_id[buyer_aid].work_signals
    # Seller already signed → not surfaced via this signal (but may
    # still surface via stale_intent below — irrelevant here).


@pytest.mark.db
async def test_discovery_surfaces_stale_agent(
    async_db_session, patch_async_session
):
    long_ago = datetime.utcnow() - timedelta(hours=settings.agent_scheduler_stale_hours + 1)
    user_id, agent_id, _ = await _seed_agent(
        async_db_session, last_tick_at=long_ago
    )
    await _seed_intent(async_db_session, user_id=user_id, status="active")

    candidates = await discover_tick_candidates(async_db_session)

    by_id = {c.agent_id: c for c in candidates}
    assert agent_id in by_id
    assert "stale_intent" in by_id[agent_id].work_signals


@pytest.mark.db
async def test_discovery_excludes_inactive_agent(
    async_db_session, patch_async_session
):
    user_id, agent_id, _ = await _seed_agent(async_db_session, status="paused")
    await _seed_intent(async_db_session, user_id=user_id)

    candidates = await discover_tick_candidates(async_db_session)
    assert all(c.agent_id != agent_id for c in candidates)


@pytest.mark.db
async def test_discovery_excludes_revoked_mandate_agent(
    async_db_session, patch_async_session
):
    from app.models.schema import Mandate

    user_id, agent_id, mandate_id = await _seed_agent(async_db_session)
    await _seed_intent(async_db_session, user_id=user_id)
    # Revoke after seeding intent.
    mandate = await async_db_session.get(Mandate, mandate_id)
    mandate.revoked_at = datetime.utcnow()
    await async_db_session.commit()

    candidates = await discover_tick_candidates(async_db_session)
    assert all(c.agent_id != agent_id for c in candidates)


@pytest.mark.db
async def test_discovery_respects_cooldown(
    async_db_session, patch_async_session
):
    """Agent ticked 5s ago → still in cooldown window (default 30s)."""
    just_now = datetime.utcnow() - timedelta(seconds=5)
    user_id, agent_id, _ = await _seed_agent(
        async_db_session, last_tick_at=just_now
    )
    await _seed_intent(async_db_session, user_id=user_id)

    candidates = await discover_tick_candidates(async_db_session)
    assert all(c.agent_id != agent_id for c in candidates)


# ===========================================================================
# Ranking
# ===========================================================================


def test_priority_deal_pending_outscores_other_signals():
    s_deal = compute_priority_score(
        last_tick_at=None, signals=["deal_pending_signature"]
    )
    s_neg = compute_priority_score(
        last_tick_at=None, signals=["negotiation_active"]
    )
    s_stale = compute_priority_score(last_tick_at=None, signals=["stale_intent"])
    assert s_deal > s_neg > s_stale


def test_priority_recent_tick_penalty_applied():
    recent = datetime.utcnow() - timedelta(minutes=1)
    score_recent = compute_priority_score(
        last_tick_at=recent, signals=["negotiation_active"]
    )
    score_old = compute_priority_score(
        last_tick_at=None, signals=["negotiation_active"]
    )
    assert score_recent < score_old


@pytest.mark.db
async def test_discovery_orders_candidates_by_priority(
    async_db_session, patch_async_session
):
    # Agent A: stale_intent only (low score)
    long_ago = datetime.utcnow() - timedelta(hours=settings.agent_scheduler_stale_hours + 1)
    user_a, agent_a, _ = await _seed_agent(async_db_session, last_tick_at=long_ago)
    await _seed_intent(async_db_session, user_id=user_a)

    # Agent B: pending deal (high score)
    user_b, agent_b, _ = await _seed_agent(async_db_session)
    user_c_other, _, _ = await _seed_agent(async_db_session)  # counterparty
    await _seed_deal(
        async_db_session,
        buyer_user_id=user_b,
        seller_user_id=user_c_other,
        buyer_signed=False,
        seller_signed=True,
    )

    candidates = await discover_tick_candidates(async_db_session)
    pos_a = next(i for i, c in enumerate(candidates) if c.agent_id == agent_a)
    pos_b = next(i for i, c in enumerate(candidates) if c.agent_id == agent_b)
    assert pos_b < pos_a, "deal_pending must rank above stale_intent"


# ===========================================================================
# Rate limiter
# ===========================================================================


async def test_rate_limiter_concurrent_cap_blocks_excess():
    rl = TickRateLimiter(max_concurrent=2, max_per_minute=100)
    a1 = await rl.acquire()
    a2 = await rl.acquire()
    assert a1 and a2
    # Third should block; we wrap in a timeout to assert non-blocking failure.
    third = asyncio.create_task(rl.acquire())
    try:
        await asyncio.wait_for(asyncio.shield(third), timeout=0.05)
        third_done = True
    except asyncio.TimeoutError:
        third_done = False
    assert not third_done
    rl.release()  # frees slot for the waiting task
    assert await asyncio.wait_for(third, timeout=0.5)
    rl.release()
    rl.release()


async def test_rate_limiter_release_frees_slot():
    rl = TickRateLimiter(max_concurrent=1, max_per_minute=100)
    assert await rl.acquire()
    rl.release()
    assert await rl.acquire()  # slot is free again
    rl.release()


async def test_rate_limiter_per_minute_cap_rejects_without_holding_semaphore():
    rl = TickRateLimiter(max_concurrent=10, max_per_minute=2)
    a1 = await rl.acquire()
    a2 = await rl.acquire()
    a3 = await rl.acquire()
    assert a1 and a2
    assert a3 is False
    # Semaphore was not taken on the rejected call — in_flight == 2.
    assert rl.in_flight == 2
    rl.release()
    rl.release()


async def test_rate_limiter_minute_window_slides_after_entries_expire():
    rl = TickRateLimiter(max_concurrent=10, max_per_minute=1)
    assert await rl.acquire()
    # Force the first entry to age out by manipulating internal deque.
    rl._minute_window[0] = datetime.utcnow() - timedelta(minutes=2)
    assert await rl.acquire()  # window has slid → new slot available
    rl.release()
    rl.release()


def test_rate_limiter_rejects_invalid_construction():
    with pytest.raises(ValueError):
        TickRateLimiter(max_concurrent=0, max_per_minute=10)
    with pytest.raises(ValueError):
        TickRateLimiter(max_concurrent=10, max_per_minute=0)


# ===========================================================================
# Dispatch
# ===========================================================================


class _FakeOrchestrator:
    def __init__(self, *, raise_on: set[str] | None = None):
        self.calls: list[str] = []
        self._raise_on = raise_on or set()

    async def run_tick(self, agent_id: str) -> TickResult:
        self.calls.append(agent_id)
        if agent_id in self._raise_on:
            raise RuntimeError(f"boom for {agent_id}")
        return TickResult(
            agent_id=agent_id,
            success=True,
            reason="tick_completed",
            turns_used=1,
            tool_calls_count=0,
            estimated_cost_usd=0.001,
        )


@pytest.mark.db
async def test_dispatch_calls_orchestrator_per_candidate(
    async_db_session, patch_async_session
):
    buyer_uid, buyer_aid, _ = await _seed_agent(async_db_session)
    seller_uid, seller_aid, _ = await _seed_agent(async_db_session)
    await _seed_negotiation(
        async_db_session,
        buyer_user_id=buyer_uid,
        seller_user_id=seller_uid,
    )

    fake = _FakeOrchestrator()
    rl = TickRateLimiter(max_concurrent=10, max_per_minute=100)

    async def _await_spawn(coro):
        await coro

    summary = await discover_and_dispatch_ticks(
        orchestrator=fake, rate_limiter=rl, spawn=_await_spawn
    )

    assert summary["dispatched"] == 2
    assert set(fake.calls) == {buyer_aid, seller_aid}
    # Rate limiter all released.
    assert rl.in_flight == 0


@pytest.mark.db
async def test_dispatch_swallows_tick_exception(
    async_db_session, patch_async_session
):
    buyer_uid, buyer_aid, _ = await _seed_agent(async_db_session)
    seller_uid, _, _ = await _seed_agent(async_db_session)
    await _seed_negotiation(
        async_db_session, buyer_user_id=buyer_uid, seller_user_id=seller_uid
    )

    fake = _FakeOrchestrator(raise_on={buyer_aid})
    rl = TickRateLimiter(max_concurrent=10, max_per_minute=100)

    async def _await_spawn(coro):
        await coro  # exception in the coroutine must NOT propagate

    summary = await discover_and_dispatch_ticks(
        orchestrator=fake, rate_limiter=rl, spawn=_await_spawn
    )
    assert summary["dispatched"] == 2
    # Even on exception, the rate limiter slot was released.
    assert rl.in_flight == 0


@pytest.mark.db
async def test_dispatch_rate_limit_stops_after_cap(
    async_db_session, patch_async_session
):
    """3 candidates, per_minute=2 → dispatched=2, rate_limited=1."""
    a_uid, _, _ = await _seed_agent(async_db_session)
    b_uid, _, _ = await _seed_agent(async_db_session)
    c_uid, _, _ = await _seed_agent(async_db_session)
    # Three pairwise negotiations to make 3 distinct candidates.
    await _seed_negotiation(
        async_db_session, buyer_user_id=a_uid, seller_user_id=b_uid
    )
    await _seed_negotiation(
        async_db_session, buyer_user_id=b_uid, seller_user_id=c_uid
    )

    fake = _FakeOrchestrator()
    rl = TickRateLimiter(max_concurrent=10, max_per_minute=2)

    async def _await_spawn(coro):
        await coro

    summary = await discover_and_dispatch_ticks(
        orchestrator=fake, rate_limiter=rl, spawn=_await_spawn
    )
    assert summary["dispatched"] == 2
    assert summary["rate_limited"] >= 1


@pytest.mark.db
async def test_dispatch_short_circuits_on_daily_cap(
    async_db_session, patch_async_session, monkeypatch
):
    # Seed cost above cap.
    monkeypatch.setattr(settings, "max_daily_llm_cost_usd", 1.0)
    await _upsert_daily_cost(async_db_session, cost_usd=2.0)
    await async_db_session.commit()

    a_uid, _, _ = await _seed_agent(async_db_session)
    b_uid, _, _ = await _seed_agent(async_db_session)
    await _seed_negotiation(
        async_db_session, buyer_user_id=a_uid, seller_user_id=b_uid
    )

    fake = _FakeOrchestrator()
    rl = TickRateLimiter(max_concurrent=10, max_per_minute=100)

    summary = await discover_and_dispatch_ticks(
        orchestrator=fake, rate_limiter=rl
    )
    assert summary["skipped_daily_cap"] is True
    assert summary["dispatched"] == 0
    assert fake.calls == []


# ===========================================================================
# Cost
# ===========================================================================


@pytest.mark.db
async def test_upsert_daily_cost_increments_existing_row(async_db_session):
    await _upsert_daily_cost(async_db_session, cost_usd=0.10)
    await _upsert_daily_cost(async_db_session, cost_usd=0.05)
    await async_db_session.commit()

    rows = (
        await async_db_session.execute(select(DailyCostTracking))
    ).scalars().all()
    assert len(rows) == 1
    assert float(rows[0].total_cost_usd) == pytest.approx(0.15, abs=1e-9)
    assert rows[0].tick_count == 2


@pytest.mark.db
async def test_get_today_cost_returns_zero_when_no_row(async_db_session):
    cost = await get_today_cost_usd(async_db_session)
    assert cost == 0.0


# ===========================================================================
# Integration: lifecycle
# ===========================================================================


def test_start_scheduler_disabled_returns_none(monkeypatch):
    monkeypatch.setattr(settings, "enable_agent_scheduler", False)
    assert agent_scheduler.start_scheduler() is None


def test_shutdown_scheduler_clears_singletons(monkeypatch):
    """Force a non-None scheduler singleton, then assert shutdown wipes it."""
    monkeypatch.setattr(settings, "enable_agent_scheduler", True)
    # We can't actually start apscheduler in a sync test cleanly; simulate the
    # post-start state by setting the global and the rate limiter, then verify
    # shutdown_scheduler clears them.
    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    fake_sched = AsyncIOScheduler()
    agent_scheduler._scheduler = fake_sched
    agent_scheduler._default_orchestrator = object()  # type: ignore[assignment]
    agent_scheduler._default_rate_limiter = TickRateLimiter()  # type: ignore[assignment]

    agent_scheduler.shutdown_scheduler()

    assert agent_scheduler._scheduler is None
    assert agent_scheduler._default_orchestrator is None
    assert agent_scheduler._default_rate_limiter is None
