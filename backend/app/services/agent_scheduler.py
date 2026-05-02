"""Agent scheduler — discovery + dispatch loop (brief task 6.3.c).

Closes FASE 6 by giving the orchestrator a *trigger*. Without this
module, `orchestrator.run_tick(agent_id)` only runs when something
calls it manually. With it, an in-process apscheduler job fires every
60 seconds, finds agents with pending work, and dispatches ticks
within global rate + cost caps.

Architecture:

    discover_and_dispatch_ticks (every 60s)
      ├─ daily cost cap check
      ├─ discover_tick_candidates (3 SQL signal queries)
      ├─ rank by priority_score
      └─ for each: rate_limiter.acquire + create_task(_run_tick_safely)

The job is fire-and-forget per agent: ticks run as background tasks,
the next discovery (60s later) re-evaluates state. Agents in cooldown
are filtered at discovery so the next discovery won't double-dispatch
ones still mid-tick.

V0 deliberate simplifications (documented for V1+ revisits):
  - In-process scheduler. Single-worker only. Move to Redis + leader
    lock when we go multi-process.
  - No retry on tick failures: orchestrator already returns a
    `TickResult.reason` and the next discovery re-evaluates.
  - Per-agent cooldown is a discovery filter, not a true distributed
    lock. Acceptable for single-worker V0.
"""
from __future__ import annotations

import asyncio
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.orchestrator import AgentOrchestrator
from app.core.config import settings
from app.core.db import AsyncSessionLocal
from app.core.logging import log
from app.core.rate_limiter import TickRateLimiter
from app.services import audit_service, cost_tracking_service
from app.models.schema import (
    Agent,
    Deal,
    Intent,
    Mandate,
    Match,
    Negotiation,
)


# ---------------------------------------------------------------------------
# Priority weights (V0 defaults — tunable post-launch with real traffic)
# ---------------------------------------------------------------------------

_SIGNAL_WEIGHTS: dict[str, int] = {
    "deal_pending_signature": 100,  # urgent: 24h expiry on the deal
    "negotiation_active": 30,       # ongoing trade — keep it warm
    "stale_intent": 10,             # periodic refresh
}

_RECENT_TICK_PENALTY: int = 20  # points off if last tick was < 5 min ago


# ---------------------------------------------------------------------------
# TickCandidate
# ---------------------------------------------------------------------------


@dataclass
class TickCandidate:
    """One candidate for the dispatch round.

    `work_signals` is the set of signal names that fired (for debug /
    audit / future weighting). `priority_score` is the precomputed
    sort key — higher = ticked first.
    """

    agent_id: str
    user_id: str
    last_tick_at: datetime | None
    priority_score: int
    work_signals: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


async def discover_tick_candidates(
    db: AsyncSession,
    *,
    max_candidates: int | None = None,
    cooldown_seconds: int | None = None,
    stale_hours: int | None = None,
) -> list[TickCandidate]:
    """Return agents that should tick now, sorted by priority_score desc.

    Three signals fire (any one is sufficient):
      - `deal_pending_signature`: a deal involving the user is in
        `pending_signatures` and they haven't signed their side yet.
      - `negotiation_active`: an active negotiation involves an intent
        owned by the user.
      - `stale_intent`: the agent has at least one active intent and
        hasn't ticked in `stale_hours` (or has never ticked).

    All signals are gated on a base eligibility filter:
      - `agent.status == 'active'`
      - the agent has a non-revoked, non-expired mandate
      - the agent's `last_tick_at` is past cooldown (or NULL)
    """
    cooldown_s = cooldown_seconds or settings.agent_scheduler_cooldown_seconds
    stale_h = stale_hours or settings.agent_scheduler_stale_hours
    cap = max_candidates or settings.agent_scheduler_max_candidates

    now = _utcnow_naive()
    cooldown_cutoff = now - timedelta(seconds=cooldown_s)
    stale_cutoff = now - timedelta(hours=stale_h)

    eligible_rows = (
        await db.execute(
            select(Agent.id, Agent.user_id, Agent.last_tick_at)
            .join(Mandate, Mandate.agent_id == Agent.id)
            .where(
                Agent.status == "active",
                Mandate.revoked_at.is_(None),
                Mandate.expires_at > now,
                or_(
                    Agent.last_tick_at.is_(None),
                    Agent.last_tick_at < cooldown_cutoff,
                ),
            )
            .distinct()
        )
    ).all()
    if not eligible_rows:
        return []

    eligible_map: dict[str, Any] = {row.id: row for row in eligible_rows}
    eligible_agent_ids = list(eligible_map.keys())

    user_to_agents: dict[str, list[str]] = {}
    for row in eligible_rows:
        user_to_agents.setdefault(row.user_id, []).append(row.id)
    eligible_user_ids = list(user_to_agents.keys())

    sig_a = await _query_signal_negotiation_active(db, eligible_user_ids, user_to_agents)
    sig_b = await _query_signal_deal_pending(db, eligible_user_ids, user_to_agents)
    sig_c = await _query_signal_stale_intent(
        db, eligible_map, stale_cutoff
    )

    candidates: list[TickCandidate] = []
    for agent_id in eligible_agent_ids:
        signals: list[str] = []
        if agent_id in sig_b:
            signals.append("deal_pending_signature")
        if agent_id in sig_a:
            signals.append("negotiation_active")
        if agent_id in sig_c:
            signals.append("stale_intent")
        if not signals:
            continue

        priority = compute_priority_score(
            last_tick_at=eligible_map[agent_id].last_tick_at,
            signals=signals,
        )
        candidates.append(
            TickCandidate(
                agent_id=agent_id,
                user_id=eligible_map[agent_id].user_id,
                last_tick_at=eligible_map[agent_id].last_tick_at,
                priority_score=priority,
                work_signals=signals,
            )
        )

    # Sort: higher priority first, oldest tick first to break ties (NULL = oldest).
    epoch = datetime.min
    candidates.sort(
        key=lambda c: (-c.priority_score, c.last_tick_at or epoch)
    )
    return candidates[:cap]


async def _query_signal_negotiation_active(
    db: AsyncSession,
    eligible_user_ids: list[str],
    user_to_agents: dict[str, list[str]],
) -> set[str]:
    if not eligible_user_ids:
        return set()
    user_ids = (
        await db.scalars(
            select(Intent.user_id)
            .distinct()
            .join(
                Match,
                or_(
                    Match.buy_intent_id == Intent.id,
                    Match.sell_intent_id == Intent.id,
                ),
            )
            .join(Negotiation, Negotiation.match_id == Match.id)
            .where(
                Intent.user_id.in_(eligible_user_ids),
                Negotiation.status == "active",
            )
        )
    ).all()
    return {a for uid in user_ids for a in user_to_agents.get(uid, [])}


async def _query_signal_deal_pending(
    db: AsyncSession,
    eligible_user_ids: list[str],
    user_to_agents: dict[str, list[str]],
) -> set[str]:
    if not eligible_user_ids:
        return set()
    rows = (
        await db.execute(
            select(
                Deal.buyer_user_id,
                Deal.seller_user_id,
                Deal.buyer_signed_at,
                Deal.seller_signed_at,
            ).where(
                Deal.status == "pending_signatures",
                or_(
                    Deal.buyer_user_id.in_(eligible_user_ids),
                    Deal.seller_user_id.in_(eligible_user_ids),
                ),
            )
        )
    ).all()
    out: set[str] = set()
    for row in rows:
        if (
            row.buyer_user_id in user_to_agents
            and row.buyer_signed_at is None
        ):
            out.update(user_to_agents[row.buyer_user_id])
        if (
            row.seller_user_id in user_to_agents
            and row.seller_signed_at is None
        ):
            out.update(user_to_agents[row.seller_user_id])
    return out


async def _query_signal_stale_intent(
    db: AsyncSession,
    eligible_map: dict[str, Any],
    stale_cutoff: datetime,
) -> set[str]:
    """Stale = the agent has any active intent (owned by its user) AND
    its `last_tick_at` is NULL or older than `stale_cutoff`."""
    if not eligible_map:
        return set()
    eligible_user_ids = [row.user_id for row in eligible_map.values()]
    user_to_agents: dict[str, list[str]] = {}
    for aid, row in eligible_map.items():
        user_to_agents.setdefault(row.user_id, []).append(aid)

    user_ids = (
        await db.scalars(
            select(Intent.user_id)
            .distinct()
            .where(
                Intent.user_id.in_(eligible_user_ids),
                Intent.status == "active",
            )
        )
    ).all()
    has_active_intent = {
        a for uid in user_ids for a in user_to_agents.get(uid, [])
    }
    return {
        aid
        for aid in has_active_intent
        if (eligible_map[aid].last_tick_at is None)
        or (eligible_map[aid].last_tick_at < stale_cutoff)
    }


# ---------------------------------------------------------------------------
# Ranking
# ---------------------------------------------------------------------------


def compute_priority_score(
    *,
    last_tick_at: datetime | None,
    signals: Iterable[str],
) -> int:
    """Sum of signal weights minus a recent-tick penalty.

    Capped at 0 from below so a heavy penalty doesn't flip an
    otherwise-eligible candidate to negative and let a less-active
    agent overtake it.
    """
    score = sum(_SIGNAL_WEIGHTS.get(s, 0) for s in signals)
    if last_tick_at is not None:
        recent_cutoff = _utcnow_naive() - timedelta(minutes=5)
        if last_tick_at > recent_cutoff:
            score -= _RECENT_TICK_PENALTY
    return max(score, 0)


# ---------------------------------------------------------------------------
# Daily cost
# ---------------------------------------------------------------------------


async def get_today_cost_usd(db: AsyncSession) -> float:
    """Return today's cumulative LLM spend (cross-user sum).

    Thin alias over `cost_tracking_service.get_today_cost_usd`. The
    function lives here for backward compatibility with [6.3.c] callers
    (`_dev_endpoints`, internal kill-switch). Implementation moved out
    in [7.3.2] when the table grew a `user_id` column and a single-row
    read by date no longer holds the global aggregate.
    """
    from app.services import cost_tracking_service

    return await cost_tracking_service.get_today_cost_usd(db)


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


async def _run_tick_safely(
    orchestrator: AgentOrchestrator,
    agent_id: str,
    rate_limiter: TickRateLimiter,
) -> None:
    """Run one tick, log the outcome, release the rate-limiter slot.

    Catches every exception from the orchestrator. The scheduler must
    not die from a single buggy tick; the next discovery will look at
    the audit trail and decide what to do.
    """
    from app.core.metrics import AGENT_TICK_DURATION_SECONDS
    with AGENT_TICK_DURATION_SECONDS.time():
        try:
            result = await orchestrator.run_tick(agent_id)
            log.info(
                "scheduler.tick_completed",
                agent_id=agent_id,
                success=result.success,
                reason=result.reason,
                turns=result.turns_used,
                tool_calls=result.tool_calls_count,
                cost_usd=round(result.estimated_cost_usd, 6),
            )
        except Exception as exc:
            log.exception(
                "scheduler.tick_unexpected_error",
                agent_id=agent_id,
                error=type(exc).__name__,
                message=str(exc),
            )
        finally:
            rate_limiter.release()


async def discover_and_dispatch_ticks(
    *,
    orchestrator: AgentOrchestrator | None = None,
    rate_limiter: TickRateLimiter | None = None,
    spawn: Any = None,
) -> dict[str, Any]:
    """One discovery cycle. Returns telemetry dict for tests + dev endpoint.

    The `spawn` arg is a test seam: tests pass an awaitable runner so
    they can `await` the dispatch deterministically. Production passes
    None, in which case ticks run via `asyncio.create_task` and the
    discovery returns immediately while ticks proceed in background.
    """
    from app.core.metrics import (
        SCHEDULER_LAST_TICK_TIMESTAMP,
        SCHEDULER_TICK_TOTAL,
        USER_COST_CAP_HITS_TOTAL,
    )

    orch = orchestrator or _get_default_orchestrator()
    rl = rate_limiter or _get_default_rate_limiter()

    summary: dict[str, Any] = {
        "discovered": 0,
        "dispatched": 0,
        "rate_limited": 0,
        "cost_capped": 0,
        "skipped_daily_cap": False,
        "today_cost_usd": 0.0,
    }

    try:
        async with AsyncSessionLocal() as db:
            cost_today = await get_today_cost_usd(db)
            summary["today_cost_usd"] = cost_today
            if cost_today >= settings.max_daily_llm_cost_usd:
                log.warning(
                    "scheduler.daily_cap_reached",
                    cost_usd=cost_today,
                    cap=settings.max_daily_llm_cost_usd,
                )
                summary["skipped_daily_cap"] = True
                SCHEDULER_TICK_TOTAL.labels(status="success").inc()
                SCHEDULER_LAST_TICK_TIMESTAMP.set(time.time())
                return summary

            candidates = await discover_tick_candidates(db)
            summary["discovered"] = len(candidates)
            if not candidates:
                log.info("scheduler.no_candidates")
                SCHEDULER_TICK_TOTAL.labels(status="success").inc()
                SCHEDULER_LAST_TICK_TIMESTAMP.set(time.time())
                return summary

            log.info(
                "scheduler.discovery_complete",
                count=len(candidates),
                top_priorities=[c.priority_score for c in candidates[:5]],
            )

            # Soft cap check + dispatch happen inside the same session so
            # the per-candidate `get_user_cost_today` query and the audit
            # row on cap-hit share one connection. The dispatched ticks
            # themselves spawn their own sessions (orchestrator opens
            # `AsyncSessionLocal()` per tick), so this session lifetime
            # only covers the discovery/check loop.
            for candidate in candidates:
                user_cost = await cost_tracking_service.get_user_cost_today(
                    db, user_id=candidate.user_id
                )
                if user_cost >= settings.daily_user_cost_cap_usd:
                    log.info(
                        "scheduler.user_cost_cap_reached",
                        user_id=candidate.user_id,
                        agent_id=candidate.agent_id,
                        today_cost_usd=user_cost,
                        cap_usd=settings.daily_user_cost_cap_usd,
                    )
                    await audit_service.log_security_event(
                        db,
                        action=audit_service.SecurityActions.USER_COST_CAP_REACHED,
                        user_id=candidate.user_id,
                        params={
                            "agent_id": candidate.agent_id,
                            "today_cost_usd": round(user_cost, 6),
                            "cap_usd": settings.daily_user_cost_cap_usd,
                        },
                    )
                    USER_COST_CAP_HITS_TOTAL.inc()
                    summary["cost_capped"] += 1
                    continue

                acquired = await rl.acquire()
                if not acquired:
                    summary["rate_limited"] = (
                        len(candidates)
                        - summary["dispatched"]
                        - summary["cost_capped"]
                    )
                    log.warning(
                        "scheduler.rate_limit_hit",
                        remaining=summary["rate_limited"],
                    )
                    break

                coroutine = _run_tick_safely(orch, candidate.agent_id, rl)
                if spawn is None:
                    asyncio.create_task(coroutine)
                else:
                    await spawn(coroutine)

                summary["dispatched"] += 1

            await db.commit()

        SCHEDULER_TICK_TOTAL.labels(status="success").inc()
        SCHEDULER_LAST_TICK_TIMESTAMP.set(time.time())
        return summary
    except Exception:
        SCHEDULER_TICK_TOTAL.labels(status="error").inc()
        raise


# ---------------------------------------------------------------------------
# Lifecycle (apscheduler integration — mirrors match_scheduler pattern)
# ---------------------------------------------------------------------------


_scheduler: AsyncIOScheduler | None = None
_default_orchestrator: AgentOrchestrator | None = None
_default_rate_limiter: TickRateLimiter | None = None


def _get_default_orchestrator() -> AgentOrchestrator:
    global _default_orchestrator
    if _default_orchestrator is None:
        _default_orchestrator = AgentOrchestrator()
    return _default_orchestrator


def _get_default_rate_limiter() -> TickRateLimiter:
    global _default_rate_limiter
    if _default_rate_limiter is None:
        _default_rate_limiter = TickRateLimiter(
            max_concurrent=settings.agent_scheduler_max_concurrent,
            max_per_minute=settings.agent_scheduler_max_per_minute,
        )
    return _default_rate_limiter


def start_scheduler() -> AsyncIOScheduler | None:
    """Start the in-process agent scheduler if enabled. Idempotent."""
    global _scheduler
    if not settings.enable_agent_scheduler:
        log.info("agent_scheduler.disabled_by_settings")
        return None
    if _scheduler is not None:
        return _scheduler

    sched = AsyncIOScheduler()
    sched.add_job(
        discover_and_dispatch_ticks,
        "interval",
        seconds=settings.agent_scheduler_interval_seconds,
        id="agent_tick_discovery",
        replace_existing=True,
    )
    sched.start()
    _scheduler = sched
    log.info(
        "agent_scheduler.started",
        interval_seconds=settings.agent_scheduler_interval_seconds,
        max_concurrent=settings.agent_scheduler_max_concurrent,
        max_per_minute=settings.agent_scheduler_max_per_minute,
        max_daily_usd=settings.max_daily_llm_cost_usd,
    )
    return sched


def shutdown_scheduler() -> None:
    """Stop the agent scheduler. Safe to call at lifespan exit.

    Tolerates `SchedulerNotRunningError` so a double-shutdown (e.g. test
    cleanup after a forced reset, or lifespan-then-cli) doesn't raise.
    """
    global _scheduler, _default_orchestrator, _default_rate_limiter
    if _scheduler is None:
        return
    try:
        _scheduler.shutdown(wait=False)
    except Exception as exc:
        log.warning(
            "agent_scheduler.shutdown_already_stopped",
            error=type(exc).__name__,
        )
    finally:
        _scheduler = None
        # Drop singletons so a fresh start_scheduler rebuilds them.
        _default_orchestrator = None
        _default_rate_limiter = None
        log.info("agent_scheduler.shutdown")


def _reset_singletons_for_tests() -> None:
    """Test hook: drop process-level state between tests."""
    global _scheduler, _default_orchestrator, _default_rate_limiter
    _scheduler = None
    _default_orchestrator = None
    _default_rate_limiter = None


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------


def _utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _today_utc() -> date:
    return datetime.now(timezone.utc).date()
