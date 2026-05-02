"""GET /api/agents/mine — list agents owned by the current user (10.1.1.1).

Coverage:

  1. happy path — user with N agents → response carries all N, ordered desc by created_at
  2. empty list — tier-1 user with no agents → 200 + empty array
  3. user isolation — multi-user setup, response includes only the caller's agents
  4. tier guard — tier-0 caller → 403
"""
from __future__ import annotations

import uuid as _uuid
from datetime import datetime, timedelta

import pytest

from app.models.schema import Agent, User
from .factories import default_user_kwargs


def _new_agent(*, user_id: str, status: str, name: str | None, created_at: datetime) -> Agent:
    """Helper: build an Agent ORM object (caller adds + commits)."""
    return Agent(
        id=str(_uuid.uuid4()),
        user_id=user_id,
        name=name,
        pubkey=f"test-pubkey-{_uuid.uuid4().hex[:8]}",
        privkey_kms_ref=f"db:test-{_uuid.uuid4().hex[:8]}",
        status=status,
        created_at=created_at,
    )


async def _seed_user(async_db_session, *, tier: int = 1, label: str = "agentlist") -> str:
    """Insert a User row at the requested tier; return user_id."""
    user_id = str(_uuid.uuid4())
    email = f"{label}-{user_id[:8]}@example.com"
    user = User(id=user_id, **default_user_kwargs(tier=tier, email=email))
    async_db_session.add(user)
    await async_db_session.commit()
    return user_id


@pytest.mark.db
@pytest.mark.asyncio
async def test_agents_mine_returns_user_agents_ordered_desc(
    http_client, async_db_session, authenticated_client
) -> None:
    """User with 2 agents → response carries both, most recent first."""
    user_id = await _seed_user(async_db_session, tier=1)

    now = datetime.utcnow()
    older = _new_agent(
        user_id=user_id,
        status="pending_mandate",
        name=None,
        created_at=now - timedelta(hours=1),
    )
    newer = _new_agent(
        user_id=user_id,
        status="active",
        name="My Agent",
        created_at=now,
    )
    async_db_session.add_all([older, newer])
    await async_db_session.commit()

    client, _ = authenticated_client(tier=1, user_id=user_id)
    resp = await client.get("/api/agents/mine")
    assert resp.status_code == 200, resp.text

    body = resp.json()
    assert len(body["agents"]) == 2
    # Most recent first (created_at desc).
    assert body["agents"][0]["id"] == newer.id
    assert body["agents"][0]["name"] == "My Agent"
    assert body["agents"][0]["status"] == "active"
    assert body["agents"][1]["id"] == older.id
    assert body["agents"][1]["name"] is None  # nullable name preserved
    assert body["agents"][1]["status"] == "pending_mandate"


@pytest.mark.db
@pytest.mark.asyncio
async def test_agents_mine_returns_empty_for_user_without_agents(
    http_client, async_db_session, authenticated_client
) -> None:
    """Tier-1 user with no agent rows → 200 + empty list."""
    user_id = await _seed_user(async_db_session, tier=1)

    client, _ = authenticated_client(tier=1, user_id=user_id)
    resp = await client.get("/api/agents/mine")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"agents": []}


@pytest.mark.db
@pytest.mark.asyncio
async def test_agents_mine_filters_by_user_id(
    http_client, async_db_session, authenticated_client
) -> None:
    """Multi-user setup: response contains ONLY caller's agents."""
    caller_id = await _seed_user(async_db_session, tier=1, label="caller")
    other_id = await _seed_user(async_db_session, tier=1, label="other")

    now = datetime.utcnow()
    caller_agent = _new_agent(
        user_id=caller_id, status="active", name="caller-agent", created_at=now
    )
    other_agent = _new_agent(
        user_id=other_id, status="active", name="other-agent", created_at=now
    )
    async_db_session.add_all([caller_agent, other_agent])
    await async_db_session.commit()

    client, _ = authenticated_client(tier=1, user_id=caller_id)
    resp = await client.get("/api/agents/mine")
    assert resp.status_code == 200, resp.text

    body = resp.json()
    assert len(body["agents"]) == 1
    assert body["agents"][0]["id"] == caller_agent.id
    assert body["agents"][0]["name"] == "caller-agent"


@pytest.mark.asyncio
async def test_agents_mine_requires_tier_1(
    http_client, authenticated_client
) -> None:
    """Tier-0 caller → 402 (require_tier(1) per brief §2.5 'Tier Upgrade Required')."""
    client, _ = authenticated_client(tier=0)
    resp = await client.get("/api/agents/mine")
    assert resp.status_code == 402
    assert resp.json()["detail"]["required_tier"] == 1
    assert resp.json()["detail"]["current_tier"] == 0
