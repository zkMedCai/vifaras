"""AgentOrchestrator tests (brief task 6.3.b).

25 tests organized by concern:

  Pre-tick gates (4):
   1. nonexistent agent → reason='agent_not_found'
   2. inactive agent → reason='early_return:not_active', no cursor advance
   3. agent without active mandate → reason='early_return:no_mandate'
   4. revoked-mandate agent treated same as no-mandate

  Tool loop happy path (4):
   5. Claude responds text-only → 1 turn, end_turn, success
   6. Claude calls 1 tool → tool dispatched, 2 turns, success
   7. Claude calls multiple tools in one turn
   8. tool_use blocks routed through AsyncToolHandler.handle()

  Tool result handling (4):
   9. ok result serialized into tool_result content
  10. error result preserves status + error_code
  11. step_up_required result preserves data.step_up_id
  12. limit_exceeded result preserves status

  Cap & safety (6):
  13. MAX_TURNS_PER_TICK breaks loop, success=False
  14. user daily cost cap blocks direct orchestrator calls before Claude
  15. global daily cost cap blocks direct orchestrator calls before Claude
  16. per-tick cost cap stops the loop and records failure cost
  17. Claude API raises → reason='claude_error', no cursor advance
  18. Unknown tool name handled by handler (orchestrator forwards)

  Audit & state (3):
  19. successful tick updates agents.last_tick_at
  20. successful tick writes tick_completed AuditLog row
  21. last_tick_summary includes turns + tool_calls + cost

  Cost tracking (2):
  22. cost accumulates across multiple turns
  23. cost computed from usage.input_tokens / output_tokens

  Session lifecycle (2):
  24. sync session closed even when tool loop raises
  25. async session closed even when tool loop raises
"""
from __future__ import annotations

import uuid
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents import orchestrator as orchestrator_module
from app.agents.orchestrator import (
    MAX_TURNS_PER_TICK,
    AgentOrchestrator,
    TickResult,
)
from app.models.schema import Agent, AuditLog, Mandate
from app.services import cost_tracking_service, embedding_service
from app.services.audit_service import AgentActions
from app.services.mandate_verifier import (
    ActionNotAllowed,
    LimitExceeded,
    StepUpRequired,
)
from tests.conftest import FakeAnthropicClient, _make_message, text_block, tool_use_block
from tests.factories import setup_active_mandate_async


# ---------------------------------------------------------------------------
# Module fixtures
# ---------------------------------------------------------------------------


def test_default_anthropic_client_uses_settings_api_key(monkeypatch) -> None:
    calls: dict[str, str] = {}

    class FakeDefaultClient:
        def __init__(self, *, api_key: str) -> None:
            calls["api_key"] = api_key

    monkeypatch.setattr(orchestrator_module.settings, "anthropic_api_key", "sk-test")
    monkeypatch.setattr(orchestrator_module, "AsyncAnthropic", FakeDefaultClient)

    orch = AgentOrchestrator()

    assert isinstance(orch.client, FakeDefaultClient)
    assert calls == {"api_key": "sk-test"}


def test_default_anthropic_client_requires_api_key(monkeypatch) -> None:
    monkeypatch.setattr(orchestrator_module.settings, "anthropic_api_key", "")

    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        AgentOrchestrator()


@pytest.fixture(autouse=True)
def _force_fake_embedding(monkeypatch):
    """Tool layer's `_create_intent` calls embedding_service even though we
    don't exercise it here — keep it deterministic and offline."""
    monkeypatch.setenv("EMBEDDING_BACKEND", "fake")
    embedding_service._reset_singleton_for_tests()
    yield
    embedding_service._reset_singleton_for_tests()


# ---------------------------------------------------------------------------
# Fake verifier — same shape as the one in test_tool_layer.py, copied
# locally rather than imported to avoid coupling test files.
# ---------------------------------------------------------------------------


class FakeVerifier:
    """Mimics MandateVerifier's three async methods.

    Drive via `behavior` ('ok' | 'step_up' | 'limit' | 'denied') or via
    a per-call dict `behavior_by_action` for tests that need different
    outcomes per tool name.
    """

    def __init__(
        self,
        *,
        behavior: str = "ok",
        behavior_by_action: dict[str, str] | None = None,
        deny_message: str = "denied",
        step_up_reason: str = "above threshold",
    ) -> None:
        self.behavior = behavior
        self.behavior_by_action = behavior_by_action or {}
        self.deny_message = deny_message
        self.step_up_reason = step_up_reason
        self.calls: list[tuple[str, str, dict]] = []
        self._fake_mandate = SimpleNamespace(id=str(uuid.uuid4()))

    def _resolve_behavior(self, action: str) -> str:
        return self.behavior_by_action.get(action, self.behavior)

    async def authorize_async(self, agent_id, action, params):
        self.calls.append(("authorize", action, dict(params)))
        b = self._resolve_behavior(action)
        if b == "step_up":
            raise StepUpRequired(action=action, params=params, reason=self.step_up_reason)
        if b == "limit":
            raise LimitExceeded(self.deny_message)
        if b == "denied":
            raise ActionNotAllowed(self.deny_message)
        return self._fake_mandate

    async def record_usage_async(
        self, mandate, action, params, success, result=None, error_code=None
    ):
        self.calls.append(("record_usage", action, {"success": success}))

    async def log_failed_async(self, agent_id, action, error):
        self.calls.append(("log_failed", action, {"error": type(error).__name__}))


# ---------------------------------------------------------------------------
# Test-orchestrator factory: binds sessions to the test connection
# ---------------------------------------------------------------------------


@pytest.fixture
def make_orchestrator(_async_db_connection):
    """Yields a function that builds an AgentOrchestrator wired to the
    test's outer-transaction DB connection.

    The async session factory mints sessions bound to the same
    `_async_db_connection` as `async_db_session`, so test reads see the
    orchestrator's writes (via savepoints) until rollback at teardown.

    The sync session factory yields `None` by default — the FakeVerifier
    doesn't touch the sync DB. Tests that need to assert sync-session
    lifecycle override it.
    """

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

    def _build(
        *,
        responses: list[Any] | None = None,
        verifier: Any = None,
        sync_factory: Any = None,
    ) -> tuple[AgentOrchestrator, FakeAnthropicClient, FakeVerifier]:
        client = FakeAnthropicClient(responses or [])
        v = verifier if verifier is not None else FakeVerifier()
        orch = AgentOrchestrator(
            anthropic_client=client,
            verifier_factory=lambda _sync_db: v,
            async_session_factory=_async_factory,
            sync_session_factory=sync_factory or _null_sync_factory,
        )
        return orch, client, v

    return _build


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


async def _seed_agent(db: AsyncSession, *, status: str = "active") -> tuple[str, str, str]:
    """Returns (user_id, agent_id, mandate_id). Agent is `status='active'` and
    has a fresh non-revoked mandate by default."""
    user_id, agent_id, mandate_id = await setup_active_mandate_async(
        db, email=f"orch-{uuid.uuid4().hex[:6]}@x.com"
    )
    if status != "active":
        agent = await db.get(Agent, agent_id)
        agent.status = status
        await db.commit()
    return user_id, agent_id, mandate_id


def _text_response(text: str = "Done.") -> SimpleNamespace:
    return _make_message([text_block(text)], stop_reason="end_turn")


def _tool_use_response(
    tool_name: str = "check_state",
    tool_input: dict | None = None,
    tool_id: str = "tu1",
) -> SimpleNamespace:
    return _make_message(
        [tool_use_block(tool_name, tool_input or {}, id=tool_id)],
        stop_reason="tool_use",
    )


# ===========================================================================
# Pre-tick gates
# ===========================================================================


@pytest.mark.db
async def test_nonexistent_agent_returns_agent_not_found(make_orchestrator):
    orch, client, _ = make_orchestrator(responses=[])
    result = await orch.run_tick(str(uuid.uuid4()))
    assert isinstance(result, TickResult)
    assert result.success is False
    assert result.reason == "agent_not_found"
    assert result.turns_used == 0
    assert client.calls == []  # never reached Claude


@pytest.mark.db
async def test_inactive_agent_skips_with_no_cursor_advance(
    make_orchestrator, async_db_session
):
    _, agent_id, _ = await _seed_agent(async_db_session, status="suspended")
    orch, client, _ = make_orchestrator(responses=[])

    result = await orch.run_tick(agent_id)

    assert result.success is False
    assert result.reason == "early_return:not_active"
    assert client.calls == []
    # Cursor not advanced.
    agent = await async_db_session.get(Agent, agent_id)
    await async_db_session.refresh(agent)
    assert agent.last_tick_at is None
    # Skip is audited.
    rows = (
        await async_db_session.execute(
            select(AuditLog).where(AuditLog.agent_id == agent_id)
        )
    ).scalars().all()
    assert any(r.action == AgentActions.TICK_SKIPPED for r in rows)


@pytest.mark.db
async def test_agent_without_mandate_skips(
    make_orchestrator, async_db_session
):
    _, agent_id, mandate_id = await _seed_agent(async_db_session)
    # Revoke the mandate.
    mandate = await async_db_session.get(Mandate, mandate_id)
    mandate.revoked_at = datetime.utcnow()
    await async_db_session.commit()

    orch, client, _ = make_orchestrator(responses=[])
    result = await orch.run_tick(agent_id)

    assert result.success is False
    assert result.reason == "early_return:no_mandate"
    assert client.calls == []


@pytest.mark.db
async def test_revoked_mandate_treated_as_no_mandate(
    make_orchestrator, async_db_session
):
    """Same gate as missing mandate — orchestrator can't run without one."""
    _, agent_id, mandate_id = await _seed_agent(async_db_session)
    mandate = await async_db_session.get(Mandate, mandate_id)
    mandate.revoked_at = datetime.utcnow()
    await async_db_session.commit()

    orch, _, _ = make_orchestrator(responses=[])
    result = await orch.run_tick(agent_id)
    assert result.reason == "early_return:no_mandate"


# ===========================================================================
# Tool loop happy path
# ===========================================================================


@pytest.mark.db
async def test_text_only_response_ends_turn_in_one_call(
    make_orchestrator, async_db_session
):
    _, agent_id, _ = await _seed_agent(async_db_session)
    orch, client, _ = make_orchestrator(
        responses=[_text_response("Nothing to do this tick.")]
    )

    result = await orch.run_tick(agent_id)

    assert result.success is True
    assert result.reason == "tick_completed"
    assert result.turns_used == 1
    assert result.tool_calls_count == 0
    assert result.final_response_text == "Nothing to do this tick."
    assert len(client.calls) == 1


@pytest.mark.db
async def test_single_tool_call_completes_in_two_turns(
    make_orchestrator, async_db_session
):
    _, agent_id, _ = await _seed_agent(async_db_session)
    orch, client, verifier = make_orchestrator(
        responses=[
            _tool_use_response("check_state", {}),
            _text_response("Reviewed state. Nothing actionable."),
        ]
    )

    result = await orch.run_tick(agent_id)

    assert result.success is True
    assert result.turns_used == 2
    assert result.tool_calls_count == 1
    # Verifier saw the authorize call for check_state.
    assert any(c[1] == "check_state" for c in verifier.calls if c[0] == "authorize")


@pytest.mark.db
async def test_multiple_tool_calls_in_one_turn_dispatched(
    make_orchestrator, async_db_session
):
    """Claude returns two tool_use blocks in one response → both dispatched."""
    _, agent_id, _ = await _seed_agent(async_db_session)
    multi_response = _make_message(
        [
            tool_use_block("check_state", {}, id="tu_a"),
            tool_use_block("read_inbox", {}, id="tu_b"),
        ],
        stop_reason="tool_use",
    )
    orch, client, verifier = make_orchestrator(
        responses=[multi_response, _text_response("Done.")]
    )

    result = await orch.run_tick(agent_id)

    assert result.success is True
    assert result.tool_calls_count == 2
    actions = {c[1] for c in verifier.calls if c[0] == "authorize"}
    assert {"check_state", "read_inbox"}.issubset(actions)


@pytest.mark.db
async def test_tool_use_routes_through_handler(
    make_orchestrator, async_db_session
):
    """The orchestrator must use AsyncToolHandler, not call services directly.
    We assert this by observing the verifier sees authorize() calls — only
    AsyncToolHandler invokes that path."""
    _, agent_id, _ = await _seed_agent(async_db_session)
    orch, _, verifier = make_orchestrator(
        responses=[
            _tool_use_response("check_state", {}),
            _text_response(),
        ]
    )

    await orch.run_tick(agent_id)

    auth_calls = [c for c in verifier.calls if c[0] == "authorize"]
    record_calls = [c for c in verifier.calls if c[0] == "record_usage"]
    assert len(auth_calls) == 1
    assert len(record_calls) == 1


# ===========================================================================
# Tool result handling (status forwarding)
# ===========================================================================


@pytest.mark.db
async def test_ok_result_appended_as_tool_result_content(
    make_orchestrator, async_db_session
):
    """The orchestrator must serialize ToolResult.to_dict() as JSON in the
    tool_result content block of the next user turn."""
    _, agent_id, _ = await _seed_agent(async_db_session)
    orch, client, _ = make_orchestrator(
        responses=[
            _tool_use_response("check_state", {}, tool_id="tx_ok"),
            _text_response(),
        ]
    )

    await orch.run_tick(agent_id)

    # Inspect the second create() call — its `messages` should contain
    # an assistant tool_use turn followed by a user tool_result turn.
    second_call = client.calls[1]
    msgs = second_call["messages"]
    assert msgs[-1]["role"] == "user"
    tool_results = msgs[-1]["content"]
    assert any(
        c.get("type") == "tool_result" and c.get("tool_use_id") == "tx_ok"
        and '"status": "ok"' in c.get("content", "")
        for c in tool_results
    )


@pytest.mark.db
async def test_error_result_preserves_error_code(
    make_orchestrator, async_db_session
):
    _, agent_id, _ = await _seed_agent(async_db_session)
    verifier = FakeVerifier(behavior="denied", deny_message="action not allowed")
    orch, client, _ = make_orchestrator(
        responses=[
            _tool_use_response("send_offer", {"match_id": "m1", "price_cents": 100, "message": "x"}),
            _text_response("Stopping after denial."),
        ],
        verifier=verifier,
    )

    await orch.run_tick(agent_id)

    second_call = client.calls[1]
    payload = second_call["messages"][-1]["content"][0]["content"]
    assert '"status": "error"' in payload


@pytest.mark.db
async def test_step_up_required_result_preserves_step_up_id(
    make_orchestrator, async_db_session
):
    _, agent_id, _ = await _seed_agent(async_db_session)
    verifier = FakeVerifier(behavior="step_up")
    orch, client, _ = make_orchestrator(
        responses=[
            _tool_use_response("send_offer", {"match_id": "m1", "price_cents": 5000, "message": "high"}),
            _text_response("Waiting for user signature."),
        ],
        verifier=verifier,
    )

    await orch.run_tick(agent_id)

    payload = client.calls[1]["messages"][-1]["content"][0]["content"]
    assert '"status": "step_up_required"' in payload
    assert '"step_up_id"' in payload


@pytest.mark.db
async def test_limit_exceeded_result_preserves_status(
    make_orchestrator, async_db_session
):
    _, agent_id, _ = await _seed_agent(async_db_session)
    verifier = FakeVerifier(behavior="limit", deny_message="daily cap")
    orch, client, _ = make_orchestrator(
        responses=[
            _tool_use_response("accept_offer", {"negotiation_id": "n1"}),
            _text_response("Limit hit, stopping."),
        ],
        verifier=verifier,
    )

    await orch.run_tick(agent_id)

    payload = client.calls[1]["messages"][-1]["content"][0]["content"]
    assert '"status": "limit_exceeded"' in payload


# ===========================================================================
# Cap & safety
# ===========================================================================


@pytest.mark.db
async def test_max_turns_exceeded_breaks_loop(
    make_orchestrator, async_db_session
):
    """If Claude keeps emitting tool_use forever, the loop must cut off
    at MAX_TURNS_PER_TICK and report failure."""
    _, agent_id, _ = await _seed_agent(async_db_session)
    # MAX_TURNS_PER_TICK responses, all tool_use → loop hits cap.
    responses = [
        _tool_use_response("check_state", {}, tool_id=f"tu{i}")
        for i in range(MAX_TURNS_PER_TICK)
    ]
    orch, client, _ = make_orchestrator(responses=responses)

    result = await orch.run_tick(agent_id)

    assert result.success is False
    assert result.reason == "max_turns_exceeded"
    assert result.turns_used == MAX_TURNS_PER_TICK
    assert len(client.calls) == MAX_TURNS_PER_TICK


@pytest.mark.db
async def test_user_cost_cap_blocks_direct_orchestrator_before_claude(
    make_orchestrator, async_db_session, monkeypatch
):
    user_id, agent_id, _ = await _seed_agent(async_db_session)
    monkeypatch.setattr(orchestrator_module.settings, "daily_user_cost_cap_usd", 0.50)
    monkeypatch.setattr(orchestrator_module.settings, "max_daily_llm_cost_usd", 50.0)
    await cost_tracking_service.upsert_daily_cost(
        async_db_session, user_id=user_id, cost_usd=0.50
    )
    await async_db_session.commit()

    orch, client, _ = make_orchestrator(responses=[_text_response("should not call")])
    result = await orch.run_tick(agent_id)

    assert result.success is False
    assert result.reason == "early_return:user_cost_cap"
    assert client.calls == []
    rows = (
        await async_db_session.execute(
            select(AuditLog).where(AuditLog.agent_id == agent_id)
        )
    ).scalars().all()
    assert any(
        r.action == AgentActions.TICK_SKIPPED
        and r.error_code == "early_return:user_cost_cap"
        for r in rows
    )


@pytest.mark.db
async def test_global_cost_cap_blocks_direct_orchestrator_before_claude(
    make_orchestrator, async_db_session, monkeypatch
):
    user_id, agent_id, _ = await _seed_agent(async_db_session)
    monkeypatch.setattr(orchestrator_module.settings, "max_daily_llm_cost_usd", 0.01)
    monkeypatch.setattr(orchestrator_module.settings, "daily_user_cost_cap_usd", 100.0)
    await cost_tracking_service.upsert_daily_cost(
        async_db_session, user_id=user_id, cost_usd=0.02
    )
    await async_db_session.commit()

    orch, client, _ = make_orchestrator(responses=[_text_response("should not call")])
    result = await orch.run_tick(agent_id)

    assert result.success is False
    assert result.reason == "early_return:global_cost_cap"
    assert client.calls == []


@pytest.mark.db
async def test_tick_cost_cap_stops_loop_and_records_failure_cost(
    make_orchestrator, async_db_session, monkeypatch
):
    user_id, agent_id, _ = await _seed_agent(async_db_session)
    monkeypatch.setattr(orchestrator_module.settings, "agent_tick_cost_cap_usd", 0.01)
    custom = _make_message(
        [text_block("expensive but complete")],
        stop_reason="end_turn",
        input_tokens=10_000,
        output_tokens=2_000,
    )
    orch, client, _ = make_orchestrator(responses=[custom])

    result = await orch.run_tick(agent_id)

    assert result.success is False
    assert result.reason == "tick_cost_cap_reached"
    assert result.turns_used == 1
    assert len(client.calls) == 1
    assert result.estimated_cost_usd == pytest.approx(0.06, abs=1e-9)
    agent = await async_db_session.get(Agent, agent_id)
    await async_db_session.refresh(agent)
    assert agent.last_tick_at is None
    user_cost = await cost_tracking_service.get_user_cost_today(
        async_db_session, user_id=user_id
    )
    assert user_cost == pytest.approx(0.06, abs=1e-9)


@pytest.mark.db
async def test_claude_api_error_returns_failure_no_cursor_advance(
    make_orchestrator, async_db_session
):
    _, agent_id, _ = await _seed_agent(async_db_session)
    boom = RuntimeError("upstream 503")
    orch, _, _ = make_orchestrator(responses=[boom])

    result = await orch.run_tick(agent_id)

    assert result.success is False
    assert result.reason == "claude_error"
    assert "RuntimeError" in (result.error or "")
    # Cursor unchanged so the next tick re-processes the same inbox.
    agent = await async_db_session.get(Agent, agent_id)
    await async_db_session.refresh(agent)
    assert agent.last_tick_at is None


@pytest.mark.db
async def test_unknown_tool_handled_by_handler_loop_continues(
    make_orchestrator, async_db_session
):
    """The handler returns a 'unknown_tool' error result; the orchestrator
    forwards it and lets Claude decide what to do next."""
    _, agent_id, _ = await _seed_agent(async_db_session)
    orch, client, _ = make_orchestrator(
        responses=[
            _tool_use_response("totally_made_up_tool", {}),
            _text_response("Got an error, stopping."),
        ]
    )

    result = await orch.run_tick(agent_id)

    assert result.success is True
    payload = client.calls[1]["messages"][-1]["content"][0]["content"]
    assert "unknown_tool" in payload


# ===========================================================================
# Audit & state mutations
# ===========================================================================


@pytest.mark.db
async def test_tick_completed_updates_last_tick_at(
    make_orchestrator, async_db_session
):
    _, agent_id, _ = await _seed_agent(async_db_session)
    before = (await async_db_session.get(Agent, agent_id)).last_tick_at
    assert before is None

    orch, _, _ = make_orchestrator(responses=[_text_response("ok")])
    await orch.run_tick(agent_id)

    agent = await async_db_session.get(Agent, agent_id)
    await async_db_session.refresh(agent)
    assert agent.last_tick_at is not None


@pytest.mark.db
async def test_tick_completed_writes_audit_row(
    make_orchestrator, async_db_session
):
    _, agent_id, _ = await _seed_agent(async_db_session)
    orch, _, _ = make_orchestrator(responses=[_text_response("ok")])
    await orch.run_tick(agent_id)

    rows = (
        await async_db_session.execute(
            select(AuditLog).where(AuditLog.agent_id == agent_id)
        )
    ).scalars().all()
    actions = {r.action for r in rows}
    assert AgentActions.TICK_COMPLETED in actions


@pytest.mark.db
async def test_last_tick_summary_includes_metrics(
    make_orchestrator, async_db_session
):
    _, agent_id, _ = await _seed_agent(async_db_session)
    orch, _, _ = make_orchestrator(
        responses=[
            _tool_use_response("check_state", {}),
            _text_response("done"),
        ]
    )
    await orch.run_tick(agent_id)

    agent = await async_db_session.get(Agent, agent_id)
    await async_db_session.refresh(agent)
    summary = agent.last_tick_summary
    assert summary["turns"] == 2
    assert summary["tool_calls"] == 1
    assert summary["cost_usd"] >= 0
    assert summary["reason"] == "tick_completed"
    assert "prompt_version" in summary


# ===========================================================================
# Cost tracking
# ===========================================================================


@pytest.mark.db
async def test_cost_accumulates_across_turns(
    make_orchestrator, async_db_session
):
    _, agent_id, _ = await _seed_agent(async_db_session)
    # Each response uses the default 1000 in / 200 out → known cost.
    # 3 turns total, each contributes the same per-turn cost.
    orch, _, _ = make_orchestrator(
        responses=[
            _tool_use_response("check_state", {}, tool_id="t1"),
            _tool_use_response("read_inbox", {}, tool_id="t2"),
            _text_response("done"),
        ]
    )
    result = await orch.run_tick(agent_id)

    # Per-turn: (1000/1M)*3 + (200/1M)*15 = 0.003 + 0.003 = 0.006 USD.
    # 3 turns → 0.018 USD.
    assert result.estimated_cost_usd == pytest.approx(0.018, abs=1e-9)


@pytest.mark.db
async def test_cost_computed_from_usage_block(
    make_orchestrator, async_db_session
):
    _, agent_id, _ = await _seed_agent(async_db_session)
    custom = _make_message(
        [text_block("hi")],
        stop_reason="end_turn",
        input_tokens=10_000,
        output_tokens=2_000,
    )
    orch, _, _ = make_orchestrator(responses=[custom])
    result = await orch.run_tick(agent_id)

    # (10_000/1M)*3 + (2_000/1M)*15 = 0.03 + 0.03 = 0.06 USD.
    assert result.estimated_cost_usd == pytest.approx(0.06, abs=1e-9)


# ===========================================================================
# Session lifecycle
# ===========================================================================


@pytest.mark.db
async def test_sync_session_closed_even_on_exception(
    make_orchestrator, async_db_session
):
    """If something raises mid-loop, the sync session's __exit__ must run.

    We use a tracking sync factory and force the verifier to be called by
    queueing one tool-use response, then have Claude blow up on turn 2.
    The sync session opens before the loop and must be closed even when
    the loop exits via the claude_error path.
    """
    _, agent_id, _ = await _seed_agent(async_db_session)

    opened = {"n": 0}
    closed = {"n": 0}

    @contextmanager
    def _tracking_sync_factory():
        opened["n"] += 1
        try:
            yield None
        finally:
            closed["n"] += 1

    orch, _, _ = make_orchestrator(
        responses=[
            _tool_use_response("check_state", {}),
            RuntimeError("boom"),
        ],
        sync_factory=_tracking_sync_factory,
    )

    result = await orch.run_tick(agent_id)

    assert result.reason == "claude_error"
    assert opened["n"] == 1
    assert closed["n"] == 1


@pytest.mark.db
async def test_async_session_closed_on_exception(
    make_orchestrator, async_db_session
):
    """The async session's __aexit__ must run even on Claude error.

    We track it via a wrapping factory.
    """
    _, agent_id, _ = await _seed_agent(async_db_session)

    opened = {"n": 0}
    closed = {"n": 0}

    @asynccontextmanager
    async def _tracking_async_factory():
        opened["n"] += 1
        async with AsyncSession(
            bind=async_db_session.bind,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        ) as session:
            try:
                yield session
            finally:
                closed["n"] += 1

    client = FakeAnthropicClient([RuntimeError("upstream down")])
    orch = AgentOrchestrator(
        anthropic_client=client,
        verifier_factory=lambda _sync: FakeVerifier(),
        async_session_factory=_tracking_async_factory,
        sync_session_factory=lambda: _null_cm(),
    )

    result = await orch.run_tick(agent_id)

    assert result.reason == "claude_error"
    assert opened["n"] == 1
    assert closed["n"] == 1


@contextmanager
def _null_cm():
    yield None
