"""Dev-only diagnostics endpoints (brief task 4.2).

Distinct from `_test_endpoints` (which gates by `app_env == "dev"` and is
purely for tier-gating coverage) — these endpoints expose internal service
state (cache hit rates, OpenAI cost estimate, error counters) that should
NEVER be visible in production. Gated by `settings.enable_dev_endpoints`
(default `False`); in production this flag stays off and every request
returns 404 even though the route is registered.

The route is registered unconditionally so tests can flip the flag at
runtime (`monkeypatch.setattr(settings, "enable_dev_endpoints", True)`)
without needing to rebuild the app. The handler reads the flag per-request.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import get_db
from app.core.security import CurrentUser, require_tier
from app.services import (
    agent_scheduler,
    agent_state_service,
    anthropic_pricing,
    cost_tracking_service,
    embedding_service,
)

router = APIRouter(prefix="/api/_dev", tags=["_dev (gated)"])


@router.get("/embedding-stats")
async def embedding_stats() -> dict:
    """Snapshot of EmbeddingService telemetry. 404 unless dev flag is on."""
    if not settings.enable_dev_endpoints:
        raise HTTPException(
            status_code=404, detail={"code": "not_found", "message": "Not Found"}
        )
    return embedding_service.get_embedding_service().stats()


@router.get("/agents/{agent_id}/state")
async def agent_state_dump(
    agent_id: str,
    user: CurrentUser = Depends(require_tier(0)),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Full agent state dump for debugging. 404 unless dev flag is on.

    Even with the dev flag on, the caller must own the agent — this is
    sensitive internal state (mandate details, intent data) and we don't
    want a dev-flag flip to expose any user's state to any other user.
    """
    if not settings.enable_dev_endpoints:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": "Not Found"},
        )
    try:
        state = await agent_state_service.get_full_state(
            db, agent_id=agent_id
        )
    except agent_state_service.AgentNotFound as exc:
        raise HTTPException(
            status_code=404,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc

    if state.user_id != user.user_id:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "agent_not_owned",
                "message": "agent belongs to another user",
            },
        )
    return state.model_dump(mode="json")


@router.get("/scheduler/status")
async def scheduler_status(
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Snapshot of agent scheduler runtime state. 404 unless dev flag is on.

    Returns:
      - `enabled`: whether the scheduler is configured to run
      - `running`: whether the apscheduler instance is live in this process
      - `today_cost_usd`: cumulative daily LLM spend (UTC date)
      - `daily_cap_usd`: configured kill-switch threshold
      - `daily_cap_reached`: bool — already over the cap for today
      - `in_flight` / `minute_window`: rate limiter snapshot
    """
    if not settings.enable_dev_endpoints:
        raise HTTPException(
            status_code=404, detail={"code": "not_found", "message": "Not Found"}
        )

    today_cost = await agent_scheduler.get_today_cost_usd(db)
    rl = (
        agent_scheduler._default_rate_limiter  # noqa: SLF001 — dev introspection
        if agent_scheduler._default_rate_limiter is not None
        else None
    )
    return {
        "enabled": settings.enable_agent_scheduler,
        "running": agent_scheduler._scheduler is not None,
        "interval_seconds": settings.agent_scheduler_interval_seconds,
        "max_concurrent": settings.agent_scheduler_max_concurrent,
        "max_per_minute": settings.agent_scheduler_max_per_minute,
        "today_cost_usd": round(today_cost, 6),
        "daily_cap_usd": settings.max_daily_llm_cost_usd,
        "daily_cap_reached": today_cost >= settings.max_daily_llm_cost_usd,
        "rate_limiter": {
            "in_flight": rl.in_flight if rl else 0,
            "minute_window_count": rl.minute_window_count if rl else 0,
        },
    }


@router.get("/ai/status")
async def ai_status(
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Founder/dev AI operations snapshot. 404 unless dev flag is on.

    The endpoint is deliberately static/no-network: it confirms provider
    configuration and cost guardrails without calling Anthropic/OpenAI or
    exposing secret values.
    """
    if not settings.enable_dev_endpoints:
        raise HTTPException(
            status_code=404, detail={"code": "not_found", "message": "Not Found"}
        )

    today_cost = await cost_tracking_service.get_today_cost_usd(db)
    daily_cap = settings.max_daily_llm_cost_usd
    known_anthropic_models = set(anthropic_pricing.known_models())

    return {
        "providers": {
            "anthropic": {
                "configured": bool(settings.anthropic_api_key),
                "model": settings.anthropic_model,
                "pricing_known": (
                    settings.anthropic_model in known_anthropic_models
                ),
            },
            "openai_embeddings": {
                "configured": bool(settings.openai_api_key),
                "backend": settings.embedding_backend,
                "model": settings.openai_embedding_model,
            },
        },
        "cost": {
            "today_cost_usd": round(today_cost, 6),
            "max_daily_llm_cost_usd": daily_cap,
            "daily_cap_remaining_usd": round(
                max(daily_cap - today_cost, 0.0), 6
            ),
            "daily_cap_reached": today_cost >= daily_cap,
            "daily_user_cost_cap_usd": settings.daily_user_cost_cap_usd,
            "agent_tick_cost_cap_usd": settings.agent_tick_cost_cap_usd,
        },
        "scheduler": {
            "enabled": settings.enable_agent_scheduler,
            "running": agent_scheduler._scheduler is not None,
            "interval_seconds": settings.agent_scheduler_interval_seconds,
            "max_concurrent": settings.agent_scheduler_max_concurrent,
            "max_per_minute": settings.agent_scheduler_max_per_minute,
        },
    }
