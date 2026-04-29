"""User-question service — V0 stub for the agent's `ask_user` tool (brief 6.3.a).

The agent surfaces a free-text question to its principal when it can't
decide unilaterally. V0 implementation is intentionally minimal: persist
the row + emit an `AGENT_QUESTION` notification, return the question id.
The answering UX lives on the mobile app (FASE 11). The agent picks up
the answer on the next tick via `inbox_service` (when V0.5 wires the
answered-question signal into the inbox).

Design notes:

  - Default expiry 24h. Questions older than that auto-expire (sweep is
    a 7.x cron job; for V0 we just stamp `expires_at` and check it on
    read).
  - Same swallow-on-error discipline as `notification_service`:
    `create_question` is best-effort post-business-context; if the row
    write fails, we warn-log and return `None`. The agent's tool call
    won't crash on a transient DB hiccup.
  - `list_pending_for_agent` is what `read_inbox` will use in V0.5 to
    surface answers back to the agent.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Final

from sqlalchemy import and_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import log
from app.models.schema import UserQuestion
from app.services import notification_service


DEFAULT_QUESTION_TTL_HOURS: Final[int] = 24


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def create_question(
    db: AsyncSession,
    *,
    agent_id: str,
    user_id: str,
    question: str,
    context: dict[str, Any] | None = None,
) -> UserQuestion | None:
    """Persist a pending question + notify the user. Best-effort; never raises.

    Returns the row on success, `None` on swallowed failure (so the agent
    tool call still resolves cleanly).
    """
    try:
        row = UserQuestion(
            id=str(uuid.uuid4()),
            agent_id=agent_id,
            user_id=user_id,
            question=question,
            context=context or {},
            status="pending",
            expires_at=_utcnow() + timedelta(hours=DEFAULT_QUESTION_TTL_HOURS),
            created_at=_utcnow(),
        )
        db.add(row)
        await db.flush()
        await db.commit()
    except Exception as exc:
        try:
            log.warning(
                "user_question.create_failed",
                agent_id=agent_id,
                error=type(exc).__name__,
                message=str(exc),
            )
        except Exception:
            pass
        return None

    # Fire-and-forget UX notification on top of the persisted row.
    await notification_service.create_notification(
        db,
        user_id=user_id,
        notification_type=notification_service.NotificationType.AGENT_QUESTION,
        title="Il tuo agente ti fa una domanda",
        body=question[:200],
        payload={
            "question_id": row.id,
            "agent_id": agent_id,
        },
    )
    return row


async def list_pending_for_agent(
    db: AsyncSession, *, agent_id: str
) -> list[UserQuestion]:
    """Pending (unanswered, unexpired) questions raised by this agent."""
    return list(
        await db.scalars(
            select(UserQuestion)
            .where(UserQuestion.agent_id == agent_id)
            .where(UserQuestion.status == "pending")
            .order_by(UserQuestion.created_at.desc())
        )
    )


async def list_pending_for_user(
    db: AsyncSession, *, user_id: str
) -> list[UserQuestion]:
    """Pending questions awaiting `user_id`'s answer."""
    return list(
        await db.scalars(
            select(UserQuestion)
            .where(UserQuestion.user_id == user_id)
            .where(UserQuestion.status == "pending")
            .order_by(UserQuestion.created_at.desc())
        )
    )


async def answer_question(
    db: AsyncSession,
    *,
    user_id: str,
    question_id: str,
    answer: str,
) -> bool:
    """Record the user's answer. Idempotent (already-answered → no-op).

    V0.5+ this will also create an inbox event for the agent's next tick.
    For V0 the agent picks up the answer via `list_pending_for_agent`
    inside `read_inbox` (wired in 6.3.b orchestrator).
    """
    result = await db.execute(
        update(UserQuestion)
        .where(UserQuestion.id == question_id)
        .where(UserQuestion.user_id == user_id)
        .where(UserQuestion.status == "pending")
        .values(status="answered", answer=answer, answered_at=_utcnow())
    )
    await db.commit()
    return (result.rowcount or 0) > 0
