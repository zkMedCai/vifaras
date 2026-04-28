"""Notification service — V0 stub (brief task 2.5).

V0 ships console-log only: real APNs/FCM push happens in V1+. The
mobile app polls `GET /api/step-up/pending` (every ~15s when an agent
is active) for now.

Sync interface for the §5 scaffold (`tool_layer.py`) and any other
sync caller. When async push notifications come, swap or add async
overloads — callers won't have to change shape.

These functions never raise: a notification failure must not break
the underlying business action (mandate signed, step-up created, etc.).
"""
from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.core.logging import log


def push_step_up_request(
    db: Session,
    *,
    agent_id: str,
    action: str,
    params: dict[str, Any],
    reason: str,
    step_up_id: str | None = None,
) -> None:
    """Notify the user that an agent action requires their step-up signature.

    V0: structured log on stdout. V1: APNs/FCM push.
    """
    try:
        log.info(
            "notification.step_up_request",
            agent_id=agent_id,
            step_up_id=step_up_id,
            action=action,
            reason=reason,
            params_keys=sorted(params.keys()),
        )
    except Exception as exc:
        try:
            log.warning(
                "notification.step_up_request.emit_failed",
                error=type(exc).__name__,
                message=str(exc),
            )
        except Exception:
            pass


def push_question(
    db: Session,
    *,
    agent_id: str,
    question: str,
    context: str = "",
) -> None:
    """Notify the user of an `ask_user` tool call by an agent."""
    try:
        log.info(
            "notification.question",
            agent_id=agent_id,
            question_preview=question[:80],
            has_context=bool(context),
        )
    except Exception as exc:
        try:
            log.warning(
                "notification.question.emit_failed",
                error=type(exc).__name__,
                message=str(exc),
            )
        except Exception:
            pass
