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
from app.services import agent_state_service, embedding_service

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
