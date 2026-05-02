"""Agent orchestrator — Claude SDK loop wired to AsyncToolHandler (FASE 6.3.b).

A "tick" is one agent waking up, looking at its world, deciding 0..N
actions, executing them via tools, then sleeping again. The scheduler
(6.3.c) decides *when* to tick. This module decides *how* to tick.

Lifecycle:

  PRE-TICK
    - Acquire AsyncSession + (lazily) SyncSession.
    - Load `AgentFullState` via `agent_state_service.get_full_state`.
    - Gate on agent.status == 'active' and a non-revoked mandate.

  TOOL LOOP (≤ MAX_TURNS)
    - Build system prompt + initial user message from the state snapshot.
    - Loop: `client.messages.create(...)` → dispatch each `tool_use` block
      through `AsyncToolHandler.handle()` → feed `tool_result` back.
    - Break on `stop_reason in {'end_turn', 'stop_sequence'}` or turn cap.

  POST-TICK
    - On success: write `agents.last_tick_at` + `last_tick_summary`,
      audit `tick_completed`. The cursor advance is what makes inbox
      delta queries see "new since last tick" on the next round.
    - On failure (early-return or Claude error): audit only, leave the
      cursor untouched so the next tick still sees the same inbox.

The `MandateVerifier` is sync (battle-tested §5 scaffold). The orchestrator
opens one `SyncSession` per tick and closes it via `with` — that's the
DQ-34 hybrid bridge. Verifier denials surface as `ToolResult` shapes the
prompt has been taught to handle (`step_up_required` / `limit_exceeded`).

V0 simplifications (documented for 7.x revisits):
  - No retry on Claude API failure: scheduler re-fires next minute.
  - No soft lock on `last_tick_at`: scheduler de-dup at job level.
  - 4096 max output tokens / 10 turn cap → ~$0.30 worst-case per tick.
"""
from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from anthropic import AsyncAnthropic
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session as SyncSession

from app.agents.tool_layer import AGENT_TOOLS, AsyncToolHandler
from app.core.db import AsyncSessionLocal, SyncSessionLocal
from app.core.logging import log
from app.core.telemetry import get_tracer
from app.models.schema import Agent
from app.models.views import AgentFullState
from app.services import anthropic_pricing, audit_service, cost_tracking_service
from app.services.agent_state_service import AgentNotFound, get_full_state
from app.services.audit_service import AgentActions
from app.services.mandate_verifier import MandateVerifier

_tracer = get_tracer("app.agents.orchestrator")

# Tool name → manual-span name. Tools not listed here fall through to the
# generic ``agent.tool`` span. The split mirrors the brief 7.2.3 taxonomy
# (matching / negotiation / signing) so dashboards can filter by intent.
_TOOL_SPAN_NAMES: dict[str, str] = {
    "search_matches": "agent.matching",
    "send_offer": "agent.negotiation",
    "send_counter_offer": "agent.negotiation",
    "reject_offer": "agent.negotiation",
    "accept_offer": "agent.signing",
}


# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

CLAUDE_MODEL: str = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-5")
MAX_TURNS_PER_TICK: int = 10
MAX_TOKENS_PER_RESPONSE: int = 4096

# Per-call pricing now lives in `app.services.anthropic_pricing`.
# Cost estimate is for the in-process cap accumulator + audit summary;
# real billing comes from the Anthropic dashboard. Prompt-cache
# discounts are NOT modeled in V0.

PROMPT_VERSION: str = "1.0"


# ---------------------------------------------------------------------------
# TickResult
# ---------------------------------------------------------------------------


@dataclass
class TickResult:
    """The orchestrator's verdict for a single tick.

    `reason` is the actionable signal for the scheduler (6.3.c) — it
    decides whether to retry sooner, back off, or page someone:

      - 'tick_completed'           the model called 0+ tools and ended.
      - 'early_return:not_active'  agent.status != 'active'.
      - 'early_return:no_mandate'  no non-revoked mandate.
      - 'agent_not_found'          DB row missing.
      - 'max_turns_exceeded'       hit the turn cap; partial work persisted.
      - 'claude_error'             API call raised; nothing was committed.
    """

    agent_id: str
    success: bool
    reason: str
    turns_used: int = 0
    tool_calls_count: int = 0
    estimated_cost_usd: float = 0.0
    final_response_text: str | None = None
    error: str | None = None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


class AgentOrchestrator:
    """One instance, many ticks. The Anthropic client is reused across
    ticks (stateless from its end); DB sessions are per-tick.
    """

    def __init__(
        self,
        anthropic_client: AsyncAnthropic | None = None,
        *,
        verifier_factory: Callable[[SyncSession | None], Any] | None = None,
        async_session_factory: Callable[[], Any] | None = None,
        sync_session_factory: Callable[[], Any] | None = None,
    ) -> None:
        """Three test seams, all None in production:

        - `verifier_factory(sync_db)` returns the verifier instance.
          Default builds a real `MandateVerifier(sync_db)`.
        - `async_session_factory()` returns an async context manager
          yielding an `AsyncSession`. Default is `AsyncSessionLocal`.
        - `sync_session_factory()` returns a sync context manager
          yielding a `Session` (or `None` when tests stub the verifier).
          Default is `SyncSessionLocal`.

        Tests bind factories to the per-test DB connection so that
        writes performed inside `run_tick` are visible to the test's
        outer transaction (and rolled back on teardown).
        """
        self.client = anthropic_client or AsyncAnthropic()
        self._verifier_factory = verifier_factory or (
            lambda sync_db: MandateVerifier(sync_db)
        )
        self._async_session_factory = async_session_factory or AsyncSessionLocal
        self._sync_session_factory = sync_session_factory or SyncSessionLocal

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    async def run_tick(self, agent_id: str) -> TickResult:
        """Execute one tick for `agent_id`. Atomic in outcome, never partial.

        DB sessions live for the duration of the tick. The sync session
        used by `MandateVerifier` is opened lazily — only after pre-tick
        gates pass — so skipped ticks don't consume a connection.
        """
        agent_id = str(agent_id)
        with _tracer.start_as_current_span("agent.tick") as tick_span:
            tick_span.set_attribute("agent.id", agent_id)
            result = await self._run_tick_inner(agent_id, tick_span)
            tick_span.set_attribute("agent.tick.reason", result.reason)
            tick_span.set_attribute("agent.tick.success", result.success)
            tick_span.set_attribute("agent.tick.turns_used", result.turns_used)
            tick_span.set_attribute(
                "agent.tick.tool_calls_count", result.tool_calls_count
            )
            tick_span.set_attribute(
                "agent.tick.estimated_cost_usd", result.estimated_cost_usd
            )
            return result

    async def _run_tick_inner(
        self, agent_id: str, tick_span: Any
    ) -> TickResult:
        async with self._async_session_factory() as async_db:
            # Pre-tick: load state + gate.
            try:
                state = await get_full_state(async_db, agent_id=agent_id)
            except AgentNotFound:
                return TickResult(
                    agent_id=agent_id,
                    success=False,
                    reason="agent_not_found",
                    error=f"agent {agent_id!r} not found",
                )

            tick_span.set_attribute("user.id", str(state.user_id))
            if state.mandate is not None:
                tick_span.set_attribute(
                    "mandate.id", str(state.mandate.mandate_id)
                )

            if state.agent_status != "active":
                return await self._record_skip(
                    async_db,
                    state=state,
                    reason="early_return:not_active",
                    detail=f"agent_status={state.agent_status!r}",
                )

            if state.mandate is None:
                return await self._record_skip(
                    async_db,
                    state=state,
                    reason="early_return:no_mandate",
                    detail="no non-revoked mandate",
                )

            # Run the tool loop with a sync session bound to the verifier.
            # `with` guarantees close on raise — so the connection returns
            # to the pool even if the loop crashes mid-turn.
            with self._sync_session_factory() as sync_db:
                verifier = self._verifier_factory(sync_db)
                handler = AsyncToolHandler(
                    async_db, agent_id, verifier=verifier
                )
                result = await self._run_tool_loop(
                    handler=handler,
                    state=state,
                )

            # Post-tick: persist cursor + summary on success only. A
            # failed tick MUST NOT advance `last_tick_at`, otherwise the
            # next tick's inbox delta query would skip events the agent
            # never actually processed.
            if result.success:
                await self._record_tick_outcome(async_db, state, result)
            else:
                await self._record_tick_failure(async_db, state, result)

            return result

    # ------------------------------------------------------------------
    # Tool loop
    # ------------------------------------------------------------------

    async def _run_tool_loop(
        self,
        *,
        handler: AsyncToolHandler,
        state: AgentFullState,
    ) -> TickResult:
        system_prompt = self._build_system_prompt(state)
        initial_message = self._build_initial_user_message(state)
        messages: list[dict[str, Any]] = [
            {"role": "user", "content": initial_message},
        ]

        turns = 0
        tool_calls_count = 0
        cost_acc = 0.0
        final_text: str | None = None
        tool_calls_log: list[dict[str, Any]] = []
        last_stop_reason: str | None = None

        while turns < MAX_TURNS_PER_TICK:
            turns += 1

            from app.core.metrics import AGENT_API_CALLS_TOTAL, COST_USD_TOTAL
            try:
                response = await self.client.messages.create(
                    model=CLAUDE_MODEL,
                    max_tokens=MAX_TOKENS_PER_RESPONSE,
                    system=system_prompt,
                    tools=AGENT_TOOLS,
                    messages=messages,
                )
                AGENT_API_CALLS_TOTAL.labels(status="success").inc()
            except Exception as exc:
                AGENT_API_CALLS_TOTAL.labels(status="error").inc()
                log.error(
                    "orchestrator.claude_call_failed",
                    agent_id=state.agent_id,
                    turn=turns,
                    error=type(exc).__name__,
                    message=str(exc),
                )
                return TickResult(
                    agent_id=state.agent_id,
                    success=False,
                    reason="claude_error",
                    turns_used=turns - 1,  # the failing turn didn't run
                    tool_calls_count=tool_calls_count,
                    estimated_cost_usd=cost_acc,
                    final_response_text=final_text,
                    tool_calls=tool_calls_log,
                    error=f"{type(exc).__name__}: {exc}",
                )

            turn_cost = self._estimate_cost(response.usage)
            cost_acc += turn_cost
            # Per-turn increment so the counter reflects actual API spend
            # in real time (not just at tick close). `response.model` is
            # the canonical model the API actually ran (Anthropic may
            # round to a dated alias); fall back to the configured model
            # only when the SDK doesn't surface it (test fakes typically
            # don't).
            COST_USD_TOTAL.labels(
                user_id=state.user_id,
                model=getattr(response, "model", None) or CLAUDE_MODEL,
            ).inc(turn_cost)
            last_stop_reason = response.stop_reason

            # Capture any text the model emitted this turn — keep the
            # latest non-empty as the "final response" for audit.
            text_blocks = [b for b in response.content if b.type == "text"]
            if text_blocks:
                final_text = "\n".join(b.text for b in text_blocks).strip() or final_text

            # Terminal stop reasons: model is done. No tool dispatch.
            if response.stop_reason in ("end_turn", "stop_sequence"):
                break

            # `tool_use` stop reason → echo the assistant turn back into
            # the message list so the model can see its own request, then
            # dispatch each block and append the results as one user turn.
            tool_use_blocks = [b for b in response.content if b.type == "tool_use"]
            if not tool_use_blocks:
                # Defensive: no terminal stop, no tool calls — the model
                # is in a degenerate state. Treat as end_turn.
                break

            messages.append({"role": "assistant", "content": response.content})

            tool_results: list[dict[str, Any]] = []
            for block in tool_use_blocks:
                tool_calls_count += 1
                span_name = _TOOL_SPAN_NAMES.get(block.name, "agent.tool")
                with _tracer.start_as_current_span(span_name) as tool_span:
                    tool_span.set_attribute("tool.name", block.name)
                    tool_result = await handler.handle(block.name, block.input)
                    tool_span.set_attribute("tool.status", tool_result.status)
                    if (
                        block.name == "search_matches"
                        and tool_result.status == "ok"
                        and isinstance(tool_result.data, dict)
                    ):
                        matches = tool_result.data.get("matches") or []
                        tool_span.set_attribute("matches.count", len(matches))
                tool_calls_log.append({
                    "tool": block.name,
                    "status": tool_result.status,
                })
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(tool_result.to_dict()),
                })

            messages.append({"role": "user", "content": tool_results})

        # Cap is hit only if the model still wanted to call tools when we
        # broke out — i.e. the last response was a tool_use turn we
        # answered, not a terminal end_turn/stop_sequence.
        hit_cap = (
            turns >= MAX_TURNS_PER_TICK
            and last_stop_reason not in ("end_turn", "stop_sequence")
        )

        return TickResult(
            agent_id=state.agent_id,
            success=not hit_cap,
            reason="max_turns_exceeded" if hit_cap else "tick_completed",
            turns_used=turns,
            tool_calls_count=tool_calls_count,
            estimated_cost_usd=cost_acc,
            final_response_text=final_text,
            tool_calls=tool_calls_log,
        )

    # ------------------------------------------------------------------
    # Prompt construction
    # ------------------------------------------------------------------

    def _build_system_prompt(self, state: AgentFullState) -> str:
        """Compose the per-tick system prompt.

        Personalised: mandate limits / remaining capacity / pseudonym
        get interpolated. The prompt teaches Claude:
          1. Who it is (agent for a verified user).
          2. The 9 tools and their result shape.
          3. The 4 ToolResult statuses and the right reaction.
          4. Negotiation strategy + when to stop.
        """
        m = state.mandate
        lr = state.limits_remaining
        # Defensive: mandate is non-None here (gated upstream) but mypy
        # doesn't know that, and lr can be None if mandate was loaded
        # without limits (shouldn't happen in V0, but guard cheaply).
        assert m is not None, "system prompt requires an active mandate"

        limits_lines = [
            f"- Per-deal max: €{m.limits.get('max_price_per_deal_eur', 'n/a')}",
            f"- Daily volume max: €{m.limits.get('max_total_volume_eur_per_day', 'n/a')}",
            f"- Mandate-total max: €{m.limits.get('max_total_volume_eur_per_mandate', 'n/a')}",
            f"- Daily deals max: {m.limits.get('max_deals_per_day', 'n/a')}",
        ]
        if lr is not None:
            remaining_lines = [
                f"- Daily volume remaining: €{lr.daily_volume_remaining_cents / 100:.2f}",
                f"- Mandate-total remaining: €{lr.mandate_total_volume_remaining_cents / 100:.2f}",
                f"- Deals remaining today: {lr.deals_remaining_today}",
            ]
        else:
            remaining_lines = ["- (limits-remaining view unavailable)"]

        return f"""You are an AI agent representing a verified human user in Vifaras, a peer-to-peer marketplace. You act on behalf of your principal user to buy or sell items by interacting with other agents through a fixed set of tools.

# IDENTITY
- Agent ID: {state.agent_id}
- Principal pseudonym: {state.nullifier_pseudonym or "n/a"}
- Mandate ID: {m.mandate_id}
- Mandate expires in {m.days_until_expiry} day(s) ({m.expires_at.isoformat()})

# PRINCIPLES
1. Act strictly within your mandate. Allowed actions: {", ".join(m.allowed_actions) or "(none)"}. The system enforces this — don't even try denied actions.
2. For each of your intents, treat `reservation_price_eur` as the floor (sell) or cap (buy) and `ideal_price_eur` as the target you negotiate toward.
3. The state in the first user message is fresh as of this tick — DO NOT call `check_state` to re-read it. Only call `check_state` if you suspect an external change mid-tick (rare).
4. Step-up is normal. The user signs offline; you cannot sign anything yourself.
5. Never include personally identifying information (names, addresses, phone numbers) in offer messages — communication is pseudonymous.

# THE 9 TOOLS
- `create_intent` — post a new buy/sell intent
- `search_matches` — find compatible counterparts for one of your intents
- `send_offer` — start a negotiation with a matched counterpart
- `send_counter_offer` — respond to an offer with a counter-proposal
- `accept_offer` — finalize a negotiation (creates a pending Deal)
- `reject_offer` — definitively decline a negotiation
- `read_inbox` — read recent events since last tick
- `check_state` — re-read your full state (use sparingly; already provided)
- `ask_user` — queue a question for your principal (offline answer)

# TOOL RESULT FORMAT
Every tool returns `{{"status": ..., "data"?: {{}}, "error"?: "...", "error_code"?: "..."}}` with one of four statuses. React as follows:

- `"ok"` → action succeeded; use `data` and continue.
- `"error"` → reconsider strategy (use `error_code` to choose) or stop that path.
- `"step_up_required"` → STOP that action. The user will sign offline; next tick you'll see the result in your inbox. Do NOT retry this turn.
- `"limit_exceeded"` → STOP that action permanently for this tick. Mandate cap hit; you cannot retry.

# NEGOTIATION STRATEGY
1. Anchor first offers near your `ideal_price` (high if selling, low if buying).
2. Counter-offer with incremental moves toward the deal zone — no giant concessions.
3. On `is_final_round=true`, make your best-and-final and say so.
4. Accept any counterparty offer at-or-better than your `reservation_price`. Don't chase a hypothetical better deal at the cost of a real acceptable one.
5. Multiple matches can be pursued in parallel (mini-auction). Whoever accepts first wins; others auto-cancel.

# WHEN TO STOP
End your turn (no further tool calls) when:
- Inbox is empty and no negotiation needs your move.
- All active negotiations are awaiting the counterparty.
- You've completed all reasonable actions for this tick.

Before ending, emit a short text summary of what you did this tick.

# YOUR LIMITS
{chr(10).join(limits_lines)}

# REMAINING THIS PERIOD
{chr(10).join(remaining_lines)}

You'll receive your full current state as the first user message. Plan first, then act."""

    def _build_initial_user_message(self, state: AgentFullState) -> str:
        """JSON dump of the world snapshot, framed for the model."""
        payload = state.model_dump(mode="json")
        return (
            "Here is your full current state for this tick. Plan your "
            "actions, then call tools — or end your turn with a brief "
            "summary if no action is needed.\n\n"
            f"```json\n{json.dumps(payload, indent=2, default=str)}\n```"
        )

    # ------------------------------------------------------------------
    # Cost
    # ------------------------------------------------------------------

    @staticmethod
    def _estimate_cost(usage: Any) -> float:
        """USD estimate from Anthropic usage block. Tolerates missing fields."""
        return anthropic_pricing.calculate_cost_usd(
            CLAUDE_MODEL,
            input_tokens=getattr(usage, "input_tokens", 0) or 0,
            output_tokens=getattr(usage, "output_tokens", 0) or 0,
        )

    # ------------------------------------------------------------------
    # Persistence helpers
    # ------------------------------------------------------------------

    async def _record_skip(
        self,
        db: AsyncSession,
        *,
        state: AgentFullState,
        reason: str,
        detail: str,
    ) -> TickResult:
        """Pre-tick gate failure: audit only, no cursor advance."""
        await audit_service.log_agent_event(
            db,
            user_id=state.user_id,
            agent_id=state.agent_id,
            mandate_id=state.mandate.mandate_id if state.mandate else None,
            action=AgentActions.TICK_SKIPPED,
            params={"reason": reason, "detail": detail},
            success=False,
            error_code=reason,
        )
        await db.commit()
        return TickResult(
            agent_id=state.agent_id,
            success=False,
            reason=reason,
            error=detail,
        )

    async def _record_tick_outcome(
        self,
        db: AsyncSession,
        state: AgentFullState,
        result: TickResult,
    ) -> None:
        agent = await db.get(Agent, state.agent_id)
        if agent is not None:
            agent.last_tick_at = datetime.now(timezone.utc).replace(tzinfo=None)
            agent.last_tick_summary = self._build_summary(result)
        await audit_service.log_agent_event(
            db,
            user_id=state.user_id,
            agent_id=state.agent_id,
            mandate_id=state.mandate.mandate_id if state.mandate else None,
            action=AgentActions.TICK_COMPLETED,
            params=self._build_summary(result),
        )
        await cost_tracking_service.upsert_daily_cost(
            db,
            user_id=state.user_id,
            cost_usd=result.estimated_cost_usd,
        )
        await db.commit()

    async def _record_tick_failure(
        self,
        db: AsyncSession,
        state: AgentFullState,
        result: TickResult,
    ) -> None:
        """Audit a tick that started but didn't finish. No cursor advance.

        Cost still accrues — even a failed tick burns input tokens up to
        the failure point. We persist whatever we accumulated so the
        daily cap reflects real spend, not just successful spend.
        """
        await audit_service.log_agent_event(
            db,
            user_id=state.user_id,
            agent_id=state.agent_id,
            mandate_id=state.mandate.mandate_id if state.mandate else None,
            action=AgentActions.TICK_FAILED,
            params=self._build_summary(result),
            success=False,
            error_code=result.reason,
        )
        if result.estimated_cost_usd > 0:
            await cost_tracking_service.upsert_daily_cost(
                db,
                user_id=state.user_id,
                cost_usd=result.estimated_cost_usd,
            )
        await db.commit()

    @staticmethod
    def _build_summary(result: TickResult) -> dict[str, Any]:
        return {
            "reason": result.reason,
            "turns": result.turns_used,
            "tool_calls": result.tool_calls_count,
            "cost_usd": round(result.estimated_cost_usd, 6),
            "final_response": (result.final_response_text or "")[:500],
            "tools": result.tool_calls,
            "prompt_version": PROMPT_VERSION,
        }


