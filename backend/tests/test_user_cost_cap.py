"""Per-user soft cost cap enforcement tests (brief task 7.3.3).

Coverage:
  1. Scheduler skips a user whose today_cost >= cap
  2. Scheduler dispatches a user whose today_cost < cap
  3. Skip emits an `audit_log` row with `USER_COST_CAP_REACHED`
  4. Cap is per-user (A capped, B below → A skipped, B dispatched)
  5. Boundary: today_cost == cap → skipped (inclusive `>=`)

The `cost_capped` summary counter is exposed by `discover_and_dispatch_ticks`
for parity with the existing `dispatched` / `rate_limited` counters; tests
read it as a secondary assertion.
"""
from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.orchestrator import TickResult
from app.core.config import settings
from app.core.rate_limiter import TickRateLimiter
from app.models.schema import (
    Agent,
    AuditLog,
    DailyCostTracking,
    Intent,
    Match,
    Negotiation,
)
from app.services import agent_scheduler, embedding_service
from app.services.agent_scheduler import discover_and_dispatch_ticks
from app.services.audit_service import SecurityActions
from app.services.cost_tracking_service import upsert_daily_cost
from tests.factories import setup_active_mandate_async


# ---------------------------------------------------------------------------
# Fixtures (mirror test_scheduler.py — keep the suites independently runnable)
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
# Seed helpers (mini-copy of test_scheduler.py — only what 5 tests need)
# ---------------------------------------------------------------------------


async def _seed_agent(db: AsyncSession) -> tuple[str, str]:
    user_id, agent_id, _ = await setup_active_mandate_async(
        db, email=f"cap-{uuid.uuid4().hex[:6]}@x.com"
    )
    return user_id, agent_id


async def _seed_negotiation_pair(
    db: AsyncSession, *, buyer_user_id: str, seller_user_id: str
) -> None:
    """Make both users surface as candidates by giving them a live
    negotiation. Mirrors the chain in `test_scheduler.py::_seed_negotiation`.
    """
    now = datetime.utcnow()

    async def _intent(user_id: str) -> str:
        intent_id = str(uuid.uuid4())
        db.add(
            Intent(
                id=intent_id,
                user_id=user_id,
                agent_id=None,
                side="buy",
                title=f"intent-{intent_id[:6]}",
                description="laptop",
                category="electronics_laptops",
                description_embedding=embedding_service._fake_embedding(
                    "laptop"
                ),
                reservation_price_cents=120000,
                ideal_price_cents=100000,
                currency="EUR",
                hard_constraints={},
                soft_preferences={},
                status="active",
                expires_at=now + timedelta(days=14),
                created_at=now,
            )
        )
        await db.commit()
        return intent_id

    buy_id = await _intent(buyer_user_id)
    sell_id = await _intent(seller_user_id)
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
    db.add(
        Negotiation(
            id=str(uuid.uuid4()),
            match_id=match_id,
            state=[],
            rounds_used=0,
            max_rounds=6,
            current_price_cents=100000,
            status="active",
        )
    )
    await db.commit()


async def _seed_user_cost(
    db: AsyncSession, *, user_id: str, cost_usd: float
) -> None:
    await upsert_daily_cost(db, user_id=user_id, cost_usd=cost_usd)
    await db.commit()


class _FakeOrchestrator:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def run_tick(self, agent_id: str) -> TickResult:
        self.calls.append(agent_id)
        return TickResult(
            agent_id=agent_id,
            success=True,
            reason="tick_completed",
            turns_used=1,
            tool_calls_count=0,
            estimated_cost_usd=0.001,
        )


async def _await_spawn(coro: Any) -> None:
    await coro


# ---------------------------------------------------------------------------
# 1. Cap reached → skipped
# ---------------------------------------------------------------------------


@pytest.mark.db
async def test_scheduler_skips_user_when_cost_cap_reached(
    async_db_session, patch_async_session, monkeypatch
):
    monkeypatch.setattr(settings, "daily_user_cost_cap_usd", 0.50)

    buyer_uid, buyer_aid = await _seed_agent(async_db_session)
    seller_uid, seller_aid = await _seed_agent(async_db_session)
    await _seed_negotiation_pair(
        async_db_session,
        buyer_user_id=buyer_uid,
        seller_user_id=seller_uid,
    )
    # Buyer has spent past the cap — must be skipped.
    await _seed_user_cost(
        async_db_session, user_id=buyer_uid, cost_usd=0.75
    )

    fake = _FakeOrchestrator()
    rl = TickRateLimiter(max_concurrent=10, max_per_minute=100)
    summary = await discover_and_dispatch_ticks(
        orchestrator=fake, rate_limiter=rl, spawn=_await_spawn
    )

    assert summary["cost_capped"] >= 1
    assert buyer_aid not in fake.calls


# ---------------------------------------------------------------------------
# 2. Below cap → dispatched
# ---------------------------------------------------------------------------


@pytest.mark.db
async def test_scheduler_dispatches_user_when_below_cap(
    async_db_session, patch_async_session, monkeypatch
):
    monkeypatch.setattr(settings, "daily_user_cost_cap_usd", 0.50)

    buyer_uid, buyer_aid = await _seed_agent(async_db_session)
    seller_uid, seller_aid = await _seed_agent(async_db_session)
    await _seed_negotiation_pair(
        async_db_session,
        buyer_user_id=buyer_uid,
        seller_user_id=seller_uid,
    )
    # Both users well below the cap.
    await _seed_user_cost(
        async_db_session, user_id=buyer_uid, cost_usd=0.10
    )
    await _seed_user_cost(
        async_db_session, user_id=seller_uid, cost_usd=0.05
    )

    fake = _FakeOrchestrator()
    rl = TickRateLimiter(max_concurrent=10, max_per_minute=100)
    summary = await discover_and_dispatch_ticks(
        orchestrator=fake, rate_limiter=rl, spawn=_await_spawn
    )

    assert summary["cost_capped"] == 0
    assert summary["dispatched"] == 2
    assert set(fake.calls) == {buyer_aid, seller_aid}


# ---------------------------------------------------------------------------
# 3. Cap reached → audit log emitted
# ---------------------------------------------------------------------------


@pytest.mark.db
async def test_user_cost_cap_reached_emits_audit_log(
    async_db_session, patch_async_session, monkeypatch
):
    monkeypatch.setattr(settings, "daily_user_cost_cap_usd", 0.50)

    buyer_uid, buyer_aid = await _seed_agent(async_db_session)
    seller_uid, _ = await _seed_agent(async_db_session)
    await _seed_negotiation_pair(
        async_db_session,
        buyer_user_id=buyer_uid,
        seller_user_id=seller_uid,
    )
    await _seed_user_cost(
        async_db_session, user_id=buyer_uid, cost_usd=0.60
    )

    fake = _FakeOrchestrator()
    rl = TickRateLimiter(max_concurrent=10, max_per_minute=100)
    await discover_and_dispatch_ticks(
        orchestrator=fake, rate_limiter=rl, spawn=_await_spawn
    )

    rows = (
        await async_db_session.execute(
            select(AuditLog).where(
                AuditLog.action == SecurityActions.USER_COST_CAP_REACHED,
                AuditLog.user_id == buyer_uid,
            )
        )
    ).scalars().all()

    assert len(rows) == 1
    row = rows[0]
    assert row.success is True
    assert row.params["agent_id"] == buyer_aid
    assert row.params["today_cost_usd"] == pytest.approx(0.60, abs=1e-6)
    assert row.params["cap_usd"] == 0.50


# ---------------------------------------------------------------------------
# 4. Cap is per-user (A capped, B below → A skip, B dispatch)
# ---------------------------------------------------------------------------


@pytest.mark.db
async def test_cap_per_user_isolated_from_other_users(
    async_db_session, patch_async_session, monkeypatch
):
    monkeypatch.setattr(settings, "daily_user_cost_cap_usd", 0.50)

    buyer_uid, buyer_aid = await _seed_agent(async_db_session)
    seller_uid, seller_aid = await _seed_agent(async_db_session)
    await _seed_negotiation_pair(
        async_db_session,
        buyer_user_id=buyer_uid,
        seller_user_id=seller_uid,
    )
    # Buyer over the cap; seller well below.
    await _seed_user_cost(
        async_db_session, user_id=buyer_uid, cost_usd=0.80
    )
    await _seed_user_cost(
        async_db_session, user_id=seller_uid, cost_usd=0.05
    )

    fake = _FakeOrchestrator()
    rl = TickRateLimiter(max_concurrent=10, max_per_minute=100)
    summary = await discover_and_dispatch_ticks(
        orchestrator=fake, rate_limiter=rl, spawn=_await_spawn
    )

    assert summary["cost_capped"] == 1
    assert summary["dispatched"] == 1
    assert seller_aid in fake.calls
    assert buyer_aid not in fake.calls


# ---------------------------------------------------------------------------
# 5b. Hook C: cap skip increments USER_COST_CAP_HITS_TOTAL counter
# ---------------------------------------------------------------------------


@pytest.mark.db
async def test_user_cost_cap_hits_increments_on_skip(
    async_db_session, patch_async_session, monkeypatch
):
    """Cap-skip path bumps `vifaras_user_cost_cap_hits_total`."""
    from prometheus_client import REGISTRY

    monkeypatch.setattr(settings, "daily_user_cost_cap_usd", 0.50)

    buyer_uid, _ = await _seed_agent(async_db_session)
    seller_uid, _ = await _seed_agent(async_db_session)
    await _seed_negotiation_pair(
        async_db_session,
        buyer_user_id=buyer_uid,
        seller_user_id=seller_uid,
    )
    await _seed_user_cost(
        async_db_session, user_id=buyer_uid, cost_usd=0.80
    )

    before = REGISTRY.get_sample_value(
        "vifaras_user_cost_cap_hits_total"
    ) or 0.0

    fake = _FakeOrchestrator()
    rl = TickRateLimiter(max_concurrent=10, max_per_minute=100)
    summary = await discover_and_dispatch_ticks(
        orchestrator=fake, rate_limiter=rl, spawn=_await_spawn
    )

    after = REGISTRY.get_sample_value(
        "vifaras_user_cost_cap_hits_total"
    ) or 0.0

    assert summary["cost_capped"] >= 1
    assert after - before == pytest.approx(summary["cost_capped"], abs=1e-9)


# ---------------------------------------------------------------------------
# 5. Boundary: cost == cap → inclusive (skip)
# ---------------------------------------------------------------------------


@pytest.mark.db
async def test_user_cost_at_exact_cap_is_skipped(
    async_db_session, patch_async_session, monkeypatch
):
    monkeypatch.setattr(settings, "daily_user_cost_cap_usd", 0.50)

    buyer_uid, buyer_aid = await _seed_agent(async_db_session)
    seller_uid, _ = await _seed_agent(async_db_session)
    await _seed_negotiation_pair(
        async_db_session,
        buyer_user_id=buyer_uid,
        seller_user_id=seller_uid,
    )
    # Exactly at cap — inclusive `>=` gates this out.
    await _seed_user_cost(
        async_db_session, user_id=buyer_uid, cost_usd=0.50
    )

    fake = _FakeOrchestrator()
    rl = TickRateLimiter(max_concurrent=10, max_per_minute=100)
    summary = await discover_and_dispatch_ticks(
        orchestrator=fake, rate_limiter=rl, spawn=_await_spawn
    )

    assert summary["cost_capped"] >= 1
    assert buyer_aid not in fake.calls
