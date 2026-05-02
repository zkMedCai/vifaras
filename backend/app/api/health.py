"""Public health endpoint (brief task 7.0.4 + 7.2.5).

Endpoints split by audience and probe semantics:

  - `GET /health` (legacy root, k8s/Fly.io probes — defined in main.py):
    minimal `{status, db}` shape, no extra DB reads beyond `SELECT 1`.

  - `GET /api/health` (rich snapshot for the frontend status banner):
    DB, scheduler, last-tick, today-cost, daily cap remaining. Public
    (no auth) but rate-limited at `settings.rate_limit_health` so spam
    can't fingerprint cost/scheduler internals.

  - `GET /api/health/live` (Kubernetes liveness probe, 7.2.5): always
    200 if the Python process responds. No downstream checks. Failing
    liveness should trigger pod restart.

  - `GET /api/health/ready` (Kubernetes readiness probe, 7.2.5):
    `200` if DB reachable AND scheduler heartbeat fresh, else `503`.
    Failing readiness gates traffic away from this replica without
    restarting it. Distinct from liveness: a stale scheduler doesn't
    mean the process is wedged, it means we shouldn't take new work.

Why four endpoints: liveness probes want a 5-line check that always
returns 200 unless the process is wedged; readiness wants a fast
downstream gate; the frontend wants a multi-field snapshot; legacy
probes need backward-compatible /health. Conflating them either bloats
the probes or starves the UI.
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import get_db
from app.core.metrics import SCHEDULER_LAST_TICK_TIMESTAMP
from app.core.rate_limit import limiter
from app.models.schema import Agent
from app.services import agent_scheduler, cost_tracking_service


router = APIRouter(tags=["health"])


def _read_scheduler_last_tick_epoch() -> float:
    """Snapshot the in-memory `SCHEDULER_LAST_TICK_TIMESTAMP` gauge.

    Returns 0.0 if the gauge was never set (process just started, or
    the scheduler is disabled and has never run a discovery cycle).
    The Prometheus client gauge's `_value.get()` is the canonical
    in-process accessor — there is no public API for reading a gauge
    that doesn't go through the registry collector.
    """
    return SCHEDULER_LAST_TICK_TIMESTAMP._value.get()


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
            # Global today cost = SUM cross-user (per-user rows since 7.3.2).
            today_cost = await cost_tracking_service.get_today_cost_usd(db)
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


# ---------------------------------------------------------------------------
# Kubernetes-style probes (7.2.5)
# ---------------------------------------------------------------------------


class LivenessResponse(BaseModel):
    status: Literal["alive"]


class ReadinessChecks(BaseModel):
    database: str = Field(..., description="`healthy` or `unhealthy: <reason>`")
    scheduler: str = Field(
        ...,
        description=(
            "`healthy` (recent tick) | `disabled` (scheduler off in env) | "
            "`no_data` (enabled but no successful tick yet) | `stale: ...s ago`"
        ),
    )


class ReadinessResponse(BaseModel):
    status: Literal["ready", "not_ready"]
    checks: ReadinessChecks


@router.get(
    "/api/health/live",
    response_model=LivenessResponse,
    summary="Liveness probe (process responsive)",
    description=(
        "Trivial liveness probe — returns 200 if the Python process is "
        "responsive. No downstream dependency checks. Failing liveness "
        "should trigger a pod restart."
    ),
)
async def liveness() -> LivenessResponse:
    return LivenessResponse(status="alive")


@router.get(
    "/api/health/ready",
    response_model=ReadinessResponse,
    summary="Readiness probe (downstream healthy)",
    description=(
        "Readiness probe — 200 if DB reachable AND scheduler heartbeat is "
        "fresh (or scheduler is disabled by config), else 503. Use this "
        "for k8s readinessProbe / load-balancer health checks."
    ),
    responses={
        200: {"description": "Backend ready to serve requests"},
        503: {"description": "Backend not ready (DB or scheduler degraded)"},
    },
)
async def readiness(
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> ReadinessResponse:
    db_status = "healthy"
    try:
        await db.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        db_status = f"unhealthy: {type(exc).__name__}"

    if not settings.enable_agent_scheduler:
        scheduler_status = "disabled"
    else:
        last_tick_epoch = _read_scheduler_last_tick_epoch()
        if last_tick_epoch <= 0.0:
            # Scheduler enabled but never produced a successful cycle yet.
            # On a fresh boot this is normal; surface it as `no_data` so
            # readiness doesn't flap during the first interval.
            scheduler_status = "no_data"
        else:
            # Compare against `time.time()` directly. The gauge is set with
            # `time.time()` (epoch UTC) in agent_scheduler.py; using
            # `datetime.utcnow().timestamp()` would silently apply the local
            # timezone offset on naive datetimes and produce wrong deltas
            # on any non-UTC host.
            import time as _time

            seconds_since = _time.time() - last_tick_epoch
            threshold = settings.agent_scheduler_interval_seconds * 2
            if seconds_since > threshold:
                scheduler_status = f"stale: last tick {int(seconds_since)}s ago"
            else:
                scheduler_status = "healthy"

    is_ready = db_status == "healthy" and not scheduler_status.startswith("stale")
    if not is_ready:
        response.status_code = 503

    return ReadinessResponse(
        status="ready" if is_ready else "not_ready",
        checks=ReadinessChecks(database=db_status, scheduler=scheduler_status),
    )
