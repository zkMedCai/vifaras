"""OpenTelemetry tracing tests (brief task 7.2.3).

Coverage:
  1. setup_telemetry returns False when disabled (no SDK init)
  2. setup_telemetry returns True when enabled, idempotent on second call
  3. AgentOrchestrator.run_tick emits an `agent.tick` top-level span
  4. agent.tick span carries `agent.id` / `user.id` / `mandate.id` attrs
  5. agent.tick span carries success/reason/turns/tool_calls attrs at end
  6. search_matches tool dispatch emits an `agent.matching` sub-span
  7. agent.matching span carries `matches.count` attribute
  8. send_offer tool dispatch emits an `agent.negotiation` sub-span

Telemetry is global state in OTel's API: a single ProxyTracer per module
caches the resolved tracer the first time a span is requested. We bypass
that by monkey-patching the orchestrator module's `_tracer` directly to
one bound to an in-memory exporter — no global state mutation, no leak
into other tests in the same session.
"""
from __future__ import annotations

import uuid
from contextlib import asynccontextmanager, contextmanager
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.orchestrator import AgentOrchestrator
from app.core import telemetry as telemetry_module
from app.core.config import settings
from app.services import embedding_service
from tests.conftest import (
    FakeAnthropicClient,
    _make_message,
    text_block,
    tool_use_block,
)
from tests.factories import setup_active_mandate_async


# ---------------------------------------------------------------------------
# In-memory span fixture: bind orchestrator's tracer to a fresh provider
# ---------------------------------------------------------------------------


@pytest.fixture
def in_memory_spans(monkeypatch):
    """Replace orchestrator's module-level tracer with one wired to an
    InMemorySpanExporter. Spans flushed synchronously via SimpleSpanProcessor.

    Returns the exporter — call `.get_finished_spans()` to read recorded
    spans, `.clear()` between assertions if needed.
    """
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    import app.agents.orchestrator as orch_module

    monkeypatch.setattr(
        orch_module,
        "_tracer",
        provider.get_tracer("app.agents.orchestrator"),
    )
    return exporter


@pytest.fixture(autouse=True)
def _force_fake_embedding(monkeypatch):
    """Tool dispatch may touch embedding_service indirectly — keep offline."""
    monkeypatch.setenv("EMBEDDING_BACKEND", "fake")
    embedding_service._reset_singleton_for_tests()
    yield
    embedding_service._reset_singleton_for_tests()


# ---------------------------------------------------------------------------
# Telemetry setup tests (no DB, no orchestrator)
# ---------------------------------------------------------------------------


def test_telemetry_disabled_returns_false(monkeypatch):
    """When `telemetry_enabled=False`, setup_telemetry no-ops and returns False."""
    monkeypatch.setattr(settings, "telemetry_enabled", False)
    monkeypatch.setattr(telemetry_module, "_initialized", False)
    assert telemetry_module.setup_telemetry(app=None) is False


def test_telemetry_enabled_console_initializes_once(monkeypatch):
    """Enabled + console exporter → setup returns True; second call returns False."""
    monkeypatch.setattr(settings, "telemetry_enabled", True)
    monkeypatch.setattr(settings, "telemetry_exporter", "console")
    monkeypatch.setattr(telemetry_module, "_initialized", False)

    try:
        assert telemetry_module.setup_telemetry(app=None) is True
        # Idempotent: a second call short-circuits.
        assert telemetry_module.setup_telemetry(app=None) is False
    finally:
        telemetry_module.shutdown_telemetry()


# ---------------------------------------------------------------------------
# Orchestrator integration: minimal seed + fakes
# ---------------------------------------------------------------------------


class FakeVerifier:
    """Allow-everything verifier; copied minimal shape from test_orchestrator.py."""

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
    """Builds an AgentOrchestrator wired to the test connection."""

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

    def _build(responses: list[Any]) -> tuple[AgentOrchestrator, FakeAnthropicClient]:
        client = FakeAnthropicClient(responses)
        orch = AgentOrchestrator(
            anthropic_client=client,
            verifier_factory=lambda _sync_db: FakeVerifier(),
            async_session_factory=_async_factory,
            sync_session_factory=_null_sync_factory,
        )
        return orch, client

    return _build


async def _seed(db: AsyncSession) -> tuple[str, str, str]:
    return await setup_active_mandate_async(
        db, email=f"telem-{uuid.uuid4().hex[:6]}@x.com"
    )


def _spans_named(exporter, name: str) -> list[Any]:
    return [s for s in exporter.get_finished_spans() if s.name == name]


# ---------------------------------------------------------------------------
# agent.tick span
# ---------------------------------------------------------------------------


@pytest.mark.db
async def test_agent_tick_creates_top_level_span(
    make_orch, async_db_session, in_memory_spans
):
    """Orchestrator.run_tick → at least one `agent.tick` span recorded."""
    _, agent_id, _ = await _seed(async_db_session)

    orch, _ = make_orch([_make_message([text_block("Done.")], "end_turn")])
    await orch.run_tick(agent_id)

    tick_spans = _spans_named(in_memory_spans, "agent.tick")
    assert len(tick_spans) == 1


@pytest.mark.db
async def test_agent_tick_carries_identity_attributes(
    make_orch, async_db_session, in_memory_spans
):
    """agent.tick span has agent.id, user.id, mandate.id attributes."""
    user_id, agent_id, mandate_id = await _seed(async_db_session)

    orch, _ = make_orch([_make_message([text_block("Done.")], "end_turn")])
    await orch.run_tick(agent_id)

    tick_span = _spans_named(in_memory_spans, "agent.tick")[0]
    assert tick_span.attributes["agent.id"] == agent_id
    assert tick_span.attributes["user.id"] == user_id
    assert tick_span.attributes["mandate.id"] == mandate_id


@pytest.mark.db
async def test_agent_tick_carries_outcome_attributes(
    make_orch, async_db_session, in_memory_spans
):
    """End-of-tick span attrs reflect TickResult: success, reason, counts."""
    _, agent_id, _ = await _seed(async_db_session)

    orch, _ = make_orch([_make_message([text_block("Done.")], "end_turn")])
    await orch.run_tick(agent_id)

    tick_span = _spans_named(in_memory_spans, "agent.tick")[0]
    assert tick_span.attributes["agent.tick.success"] is True
    assert tick_span.attributes["agent.tick.reason"] == "tick_completed"
    assert tick_span.attributes["agent.tick.turns_used"] == 1
    assert tick_span.attributes["agent.tick.tool_calls_count"] == 0


# ---------------------------------------------------------------------------
# Tool sub-spans
# ---------------------------------------------------------------------------


@pytest.mark.db
async def test_agent_matching_sub_span_emitted_for_search_matches(
    make_orch, async_db_session, in_memory_spans
):
    """search_matches tool dispatch → child span named `agent.matching`."""
    _, agent_id, _ = await _seed(async_db_session)

    orch, _ = make_orch([
        _make_message(
            [tool_use_block("search_matches", {"intent_id": str(uuid.uuid4())})],
            stop_reason="tool_use",
        ),
        _make_message([text_block("Wrap.")], stop_reason="end_turn"),
    ])
    await orch.run_tick(agent_id)

    matching_spans = _spans_named(in_memory_spans, "agent.matching")
    assert len(matching_spans) == 1
    assert matching_spans[0].attributes["tool.name"] == "search_matches"


@pytest.mark.db
async def test_agent_matching_span_records_matches_count(
    make_orch, async_db_session, in_memory_spans, monkeypatch
):
    """When search_matches returns ok+data, span carries matches.count."""
    from app.agents import tool_layer

    async def _fake_search(self, params):
        return {"matches": [{"id": "m1"}, {"id": "m2"}, {"id": "m3"}]}

    monkeypatch.setattr(
        tool_layer.AsyncToolHandler, "_search_matches", _fake_search
    )

    _, agent_id, _ = await _seed(async_db_session)
    orch, _ = make_orch([
        _make_message(
            [tool_use_block("search_matches", {"intent_id": str(uuid.uuid4())})],
            stop_reason="tool_use",
        ),
        _make_message([text_block("Wrap.")], stop_reason="end_turn"),
    ])
    await orch.run_tick(agent_id)

    matching_span = _spans_named(in_memory_spans, "agent.matching")[0]
    assert matching_span.attributes["matches.count"] == 3
    assert matching_span.attributes["tool.status"] == "ok"


@pytest.mark.db
async def test_agent_negotiation_sub_span_emitted_for_send_offer(
    make_orch, async_db_session, in_memory_spans, monkeypatch
):
    """send_offer tool dispatch → child span named `agent.negotiation`."""
    from app.agents import tool_layer

    async def _fake_send_offer(self, params):
        return {"negotiation_id": "n1"}

    monkeypatch.setattr(
        tool_layer.AsyncToolHandler, "_send_offer", _fake_send_offer
    )

    _, agent_id, _ = await _seed(async_db_session)
    orch, _ = make_orch([
        _make_message(
            [tool_use_block(
                "send_offer",
                {
                    "match_id": str(uuid.uuid4()),
                    "price_eur": 100.0,
                    "message": "ok",
                },
            )],
            stop_reason="tool_use",
        ),
        _make_message([text_block("Wrap.")], stop_reason="end_turn"),
    ])
    await orch.run_tick(agent_id)

    negotiation_spans = _spans_named(in_memory_spans, "agent.negotiation")
    assert len(negotiation_spans) == 1
    assert negotiation_spans[0].attributes["tool.name"] == "send_offer"
