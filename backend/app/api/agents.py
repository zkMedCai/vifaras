"""Agents API — list endpoints for the current user.

V0 caller: frontend mandate creation wizard needs `agent_id` pre-draft.
Future: agent dashboard, agent management UI can reuse the same endpoint.
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.security import CurrentUser, require_tier
from app.models.schema import Agent

router = APIRouter(prefix="/api/agents", tags=["agents"])


class AgentMineItem(BaseModel):
    id: str
    # Nullable: agents created via tier 0→1 upgrade have no name (only the
    # SQL dev stub or a future "rename agent" feature populates it).
    name: str | None
    # Status enum: pending_mandate | active | paused | revoked
    status: str
    pubkey: str
    created_at: datetime
    last_tick_at: datetime | None = None


class AgentMineResponse(BaseModel):
    agents: list[AgentMineItem]


@router.get(
    "/mine",
    response_model=AgentMineResponse,
    summary="List agents owned by current user",
)
async def list_my_agents(
    user: CurrentUser = Depends(require_tier(1)),
    db: AsyncSession = Depends(get_db),
) -> AgentMineResponse:
    """Return all agents owned by current user, most recent first."""
    stmt = (
        select(Agent)
        .where(Agent.user_id == user.user_id)
        .order_by(Agent.created_at.desc())
    )
    result = await db.execute(stmt)
    agents = result.scalars().all()

    return AgentMineResponse(
        agents=[
            AgentMineItem(
                id=str(a.id),
                name=a.name,
                status=a.status,
                pubkey=a.pubkey,
                created_at=a.created_at,
                last_tick_at=a.last_tick_at,
            )
            for a in agents
        ]
    )
