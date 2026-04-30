"""Public health endpoint (brief task 7.0.4).

Two endpoints split by audience:

  - `GET /health` (legacy, kept for k8s/Fly.io liveness probes):
    minimal `{status, db}` shape, no auth, no extra DB reads beyond
    `SELECT 1`. Defined in `main.py`.

  - `GET /api/health` (this module): rich, structured, tailored for
    the frontend status banner ("backend offline" / "degraded" /
    "healthy"). Returns DB, scheduler, last-tick, today-cost, daily
    cap remaining. Public (no auth) but rate-limited at
    `settings.rate_limit_health` (60/min by default) so spam can't
    poll the cost / scheduler internals to fingerprint usage.

Why two endpoints: liveness probes want a 5-line check that always
returns 200 unless the DB is actually down. The frontend wants a
multi-field snapshot. Conflating them either bloats the probe or
starves the UI.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import get_db
from app.core.rate_limit import limiter
from app.models.schema import Agent, DailyCostTracking
from app.services import agent_scheduler


router = APIRouter(tags=["health"])


class HealthChecks(BaseModel):
    database: str = Field(..., description="`healthy` or `unhealthy: <reason>`")
    agent_scheduler: str = Field(
        ..., description="`running` | `stopped` | `disabled`"
    )
    last_successful_tick: datetime | None = Field(
        None,
        description="Most recent `agents.last_tick_at` across all agents (UTC).",
    )
    today_cost_usd: float = Field(
        ..., description="Cumulative LLM spend today (UTC date)."
    )
    daily_cap_remaining_usd: float = Field(
        ...,
        description="Headroom under the daily kill-switch. Zero or negative = cap reached.",
    )


class HealthResponse(BaseModel):
    """Structured health snapshot for the frontend.

    `status` collapses the individual checks into a single banner:
      - `healthy`: every dependency is operational.
      - `degraded`: at least one non-critical check is unhealthy
        (e.g. scheduler stopped) but the DB is reachable.
      - `unhealthy`: the DB is unreachable. Frontend should show
        a "backend offline" banner and skip API calls.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "status": "healthy",
                "service": "marketplace",
                "version": "0.1.0",
                "env": "dev",
                "timestamp": "2026-04-30T18:00:00",
                "checks": {
                    "database": "healthy",
                    "agent_scheduler": "running",
                    "last_successful_tick": "2026-04-30T17:59:30",
                    "today_cost_usd": 1.234567,
                    "daily_cap_remaining_usd": 48.765433,
                },
            }
        }
    )

    status: str
    service: str
    version: str
    env: str
    timestamp: datetime
    checks: HealthChecks


@router.get(
    "/api/health",
    response_model=HealthResponse,
    summary="Structured health snapshot",
    description=(
        "Returns a structured health snapshot tailored for the frontend "
        "status banner. Public endpoint (no auth) but rate-limited."
    ),
)
@limiter.limit(lambda: settings.rate_limit_health)
async def api_health(
    request: Request,  # required by slowapi
    db: AsyncSession = Depends(get_db),
) -> HealthResponse:
    db_status = "healthy"
    db_reachable = True
    try:
        await db.execute(text("SELECT 1"))
    except Exception as exc:
        db_reachable = False
        db_status = f"unhealthy: {type(exc).__name__}"

    if not settings.enable_agent_scheduler:
        scheduler_status = "disabled"
    elif agent_scheduler._scheduler is not None:
        scheduler_status = "running"
    else:
        scheduler_status = "stopped"

    last_tick: datetime | None = None
    today_cost: float = 0.0
    if db_reachable:
        try:
            last_tick = await db.scalar(
                select(func.max(Agent.last_tick_at)).where(
                    Agent.last_tick_at.is_not(None)
                )
            )
            row = await db.get(DailyCostTracking, date.today())
            if row is not None:
                today_cost = float(row.total_cost_usd)
        except Exception as exc:  # pragma: no cover — DB went down mid-request
            db_status = f"unhealthy: {type(exc).__name__}"
            db_reachable = False

    cap_remaining = max(0.0, settings.max_daily_llm_cost_usd - today_cost)

    if not db_reachable:
        overall = "unhealthy"
    elif scheduler_status == "stopped":
        overall = "degraded"
    else:
        overall = "healthy"

    return HealthResponse(
        status=overall,
        service=settings.app_name,
        version=settings.app_version,
        env=settings.app_env,
        timestamp=datetime.utcnow(),
        checks=HealthChecks(
            database=db_status,
            agent_scheduler=scheduler_status,
            last_successful_tick=last_tick,
            today_cost_usd=round(today_cost, 6),
            daily_cap_remaining_usd=round(cap_remaining, 6),
        ),
    )
