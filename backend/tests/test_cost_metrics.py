"""Cost metrics tests (brief task 7.3.4).

Coverage:
  1. `vifaras_cost_usd_total{user_id, model}` increments on Anthropic calls
  2. `vifaras_cost_user_daily_usd{user_id}` gauge reflects post-upsert total
  3. Cap-skip increments `vifaras_user_cost_cap_hits_total` (in test_user_cost_cap.py)

Counter deltas are asserted (not absolute) — Prometheus counters are
process-global and accumulate across the test session.
"""
from __future__ import annotations

import uuid
from contextlib import asynccontextmanager, contextmanager
from types import SimpleNamespace
from typing import Any

import pytest
from prometheus_client import REGISTRY
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.orchestrator import AgentOrchestrator
from app.services import cost_tracking_service, embedding_service
from tests.conftest import FakeAnthropicClient, _make_message, text_block
from tests.factories import setup_active_mandate_async


def _sample(name: str, labels: dict[str, str] | None = None) -> float:
    """Snapshot a Prometheus sample value, 0.0 if absent."""
    val = REGISTRY.get_sample_value(name, labels)
    return float(val) if val is not None else 0.0


@pytest.fixture(autouse=True)
def _force_fake_embedding(monkeypatch):
    monkeypatch.setenv("EMBEDDING_BACKEND", "fake")
    embedding_service._reset_singleton_for_tests()
    yield
    embedding_service._reset_singleton_for_tests()


# ---------------------------------------------------------------------------
# Hook A: orchestrator increments COST_USD_TOTAL per Anthropic call
# ---------------------------------------------------------------------------


class _FakeVerifier:
    """No-op verifier; the orchestrator's cost path is independent of it."""

    async def authorize_async(self, agent_id, action, params):
        return SimpleNamespace(id=str(uuid.uuid4()))

    async def record_usage_async(
        self, mandate, action, params, success, result=None, error_code=None
    ):
        pass

    async def log_failed_async(self, agent_id, action, error):
        pass


@pytest.fixture
def make_orch(_async_db_connection):
    @asynccontextmanager
    async def _async_factory():
        async with AsyncSession(
            bind=_async_db_connection,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        ) as session:
            yield session

    @contextmanager
    def _null_sync_factory():
        yield None

    def _build(responses: list[Any]) -> AgentOrchestrator:
        return AgentOrchestrator(
            anthropic_client=FakeAnthropicClient(responses),
            verifier_factory=lambda _sync_db: _FakeVerifier(),
            async_session_factory=_async_factory,
            sync_session_factory=_null_sync_factory,
        )

    return _build


@pytest.mark.db
async def test_cost_usd_total_increments_post_anthropic_call(
    async_db_session, make_orch
):
    """One tick → COST_USD_TOTAL{user_id, model} +per-turn cost.

    Default Anthropic mock usage = 1000 in / 200 out tokens. At Sonnet
    list price ($3 / $15 per 1M), one turn = $0.003 + $0.003 = $0.006.
    """
    user_id, agent_id, _ = await setup_active_mandate_async(
        async_db_session, email=f"cm-{uuid.uuid4().hex[:6]}@x.com"
    )
    labels = {"user_id": user_id, "model": "claude-sonnet-4-5"}

    before = _sample("vifaras_cost_usd_total", labels)

    orch = make_orch([_make_message([text_block("Done.")], "end_turn")])
    await orch.run_tick(agent_id)

    after = _sample("vifaras_cost_usd_total", labels)
    delta = after - before
    # 1000 input * 3.00 / 1M + 200 output * 15.00 / 1M = 0.003 + 0.003 = 0.006
    assert delta == pytest.approx(0.006, abs=1e-6)


# ---------------------------------------------------------------------------
# Hook B: upsert refreshes the daily-cost gauge
# ---------------------------------------------------------------------------


@pytest.mark.db
async def test_cost_user_daily_usd_gauge_reflects_total(async_db_session):
    """Two upserts on the same user → gauge holds the cumulative total."""
    user_id, _, _ = await setup_active_mandate_async(
        async_db_session, email=f"cm-g-{uuid.uuid4().hex[:6]}@x.com"
    )
    labels = {"user_id": user_id}

    # Fresh user — gauge is 0 until first upsert.
    assert _sample("vifaras_cost_user_daily_usd", labels) == 0.0

    await cost_tracking_service.upsert_daily_cost(
        async_db_session, user_id=user_id, cost_usd=0.10
    )
    assert _sample("vifaras_cost_user_daily_usd", labels) == pytest.approx(
        0.10, abs=1e-6
    )

    await cost_tracking_service.upsert_daily_cost(
        async_db_session, user_id=user_id, cost_usd=0.05
    )
    assert _sample("vifaras_cost_user_daily_usd", labels) == pytest.approx(
        0.15, abs=1e-6
    )
