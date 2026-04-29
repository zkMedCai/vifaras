"""Notification service — UX layer (brief tasks 2.5 + 6.1).

V0 ships console-log + DB persistence: real APNs/FCM push happens in V1+
(FASE 11 mobile). The mobile/web app polls
`GET /api/notifications` for now.

Two coexisting interfaces:

  - **Sync helpers** (`push_step_up_request`, `push_question`) — used by
    the legacy §5 scaffold (`tool_layer.py`) which is sync. Preserved
    AS-IS to keep the scaffold callable without async refactor (DQ-28).
    These continue to console-log only.

  - **Async API** (`create_notification`, `list_notifications`,
    `mark_read`, `mark_acted`, `cleanup_expired`) — the post-6.1 path
    used by 4.x / 5.x services and the mobile-facing endpoints. Persists
    to the `notifications` table + emits a structured log.

Discipline (from the brief): notifications are emitted
**post-commit, fire-and-forget**. If `create_notification` fails (DB
hiccup, rare), it MUST NOT raise — the caller's business outcome is
already durable. We swallow + warn-log + move on. Audit log is the
authoritative record; notifications are pure UX.

Multi-recipient pattern: callers who need to notify both parties
(`accept_offer`, `submit_signature`) call `create_notification` once
per recipient. Each call is independent — one failing doesn't block
the other.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Final

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.core.logging import log
from app.models.schema import Notification


# ---------------------------------------------------------------------------
# Taxonomy
# ---------------------------------------------------------------------------


class NotificationCategory(str, Enum):
    STEP_UP = "step_up"
    MATCH = "match"
    NEGOTIATION = "negotiation"
    DEAL = "deal"
    AGENT = "agent"


class NotificationType(str, Enum):
    """Closed list of notification kinds. Adding one is a code change.

    The string value is what's persisted in `notifications.type`. The
    `category()` method returns the bucket the UI groups this under.
    """

    # Step-up
    STEP_UP_REQUIRED = "step_up_required"
    STEP_UP_APPROVED = "step_up_approved"
    STEP_UP_REJECTED = "step_up_rejected"
    STEP_UP_EXPIRED = "step_up_expired"

    # Match
    NEW_MATCH_DISCOVERED = "new_match_discovered"
    MATCH_EXPIRED = "match_expired"

    # Negotiation
    OFFER_RECEIVED = "offer_received"
    COUNTER_OFFER_RECEIVED = "counter_offer_received"
    OFFER_ACCEPTED_BY_OTHER = "offer_accepted_by_other"
    OFFER_REJECTED_BY_OTHER = "offer_rejected_by_other"
    NEGOTIATION_FINAL_ROUND = "negotiation_final_round"

    # Deal
    DEAL_CREATED = "deal_created"
    DEAL_AWAITING_YOUR_SIGNATURE = "deal_awaiting_your_signature"
    DEAL_OTHER_PARTY_SIGNED = "deal_other_party_signed"
    DEAL_CONFIRMED = "deal_confirmed"
    DEAL_CANCELLED = "deal_cancelled"
    DEAL_EXPIRED = "deal_expired"
    DEAL_MESSAGE_RECEIVED = "deal_message_received"

    # Agent (FASE 6.3 callsites)
    AGENT_LIMIT_REACHED = "agent_limit_reached"
    AGENT_PAUSED = "agent_paused"
    AGENT_QUESTION = "agent_question"

    def category(self) -> NotificationCategory:
        v = self.value
        if v.startswith("step_up_"):
            return NotificationCategory.STEP_UP
        if v.startswith(("new_match_", "match_")):
            return NotificationCategory.MATCH
        if v.startswith(("offer_", "counter_offer_", "negotiation_")):
            return NotificationCategory.NEGOTIATION
        if v.startswith("deal_"):
            return NotificationCategory.DEAL
        if v.startswith("agent_"):
            return NotificationCategory.AGENT
        # pragma: no cover — closed enum, every value matches above
        raise ValueError(f"unknown notification type prefix: {v}")


# ---------------------------------------------------------------------------
# Default expiration windows
# ---------------------------------------------------------------------------


# Step-up requests have an inherent TTL set by `step_up_service`; the
# notification mirrors it loosely. Match notifications stay around for a
# while because users browse them. Deal/negotiation notifications match
# the lifecycle of their underlying entity.
_DEFAULT_TTL_BY_CATEGORY: Final[dict[NotificationCategory, timedelta | None]] = {
    NotificationCategory.STEP_UP: timedelta(minutes=10),
    NotificationCategory.MATCH: timedelta(days=30),
    NotificationCategory.NEGOTIATION: timedelta(days=7),
    NotificationCategory.DEAL: timedelta(days=2),
    NotificationCategory.AGENT: timedelta(days=7),
}


# ---------------------------------------------------------------------------
# Output types
# ---------------------------------------------------------------------------


@dataclass
class NotificationListPage:
    rows: list[Notification]
    total: int
    limit: int


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ---------------------------------------------------------------------------
# Async API (6.1)
# ---------------------------------------------------------------------------


async def create_notification(
    db: AsyncSession,
    *,
    user_id: str,
    notification_type: NotificationType,
    title: str,
    body: str,
    payload: dict[str, Any] | None = None,
    expires_at: datetime | None = None,
) -> Notification | None:
    """Persist + log a notification. NEVER raises.

    Designed to be called post-business-commit by 4.x/5.x services.
    Manages its own transaction (flush+commit) so the caller can keep
    its session ergonomically. If the insert fails (DB outage, etc.),
    we log and return `None` — the caller's business outcome remains
    durable.

    `expires_at` defaults to the per-category TTL above when omitted.
    """
    try:
        category = notification_type.category()
        if expires_at is None:
            ttl = _DEFAULT_TTL_BY_CATEGORY.get(category)
            expires_at = _utcnow() + ttl if ttl is not None else None

        row = Notification(
            id=str(uuid.uuid4()),
            user_id=user_id,
            type=notification_type.value,
            category=category.value,
            title=title,
            body=body,
            payload=payload or {},
            expires_at=expires_at,
            created_at=_utcnow(),
        )
        db.add(row)
        await db.flush()
        await db.commit()

        log.info(
            "notification.created",
            user_id=user_id,
            type=notification_type.value,
            category=category.value,
        )
        return row
    except Exception as exc:
        # Never let notification failure break the caller. Best-effort
        # warn + swallow.
        try:
            log.warning(
                "notification.create_failed",
                user_id=user_id,
                type=getattr(notification_type, "value", str(notification_type)),
                error=type(exc).__name__,
                message=str(exc),
            )
        except Exception:
            pass
        return None


async def list_notifications(
    db: AsyncSession,
    *,
    user_id: str,
    unread_only: bool = False,
    category: NotificationCategory | None = None,
    limit: int = 50,
    before_id: str | None = None,
) -> NotificationListPage:
    """List notifications for `user_id`, newest-first, paginated by id cursor.

    `before_id` selects rows older than the cursor's `created_at`. Cleaner
    than offset paging when notifications stream in continuously.
    """
    limit = max(1, min(200, limit))

    base_filters = [Notification.user_id == user_id]
    if unread_only:
        base_filters.append(Notification.read_at.is_(None))
    if category is not None:
        base_filters.append(Notification.category == category.value)
    if before_id is not None:
        cursor_row = await db.get(Notification, before_id)
        if cursor_row is not None:
            base_filters.append(Notification.created_at < cursor_row.created_at)

    total = int(
        await db.scalar(
            select(func.count())
            .select_from(Notification)
            .where(and_(*base_filters))
        )
        or 0
    )
    rows = list(
        await db.scalars(
            select(Notification)
            .where(and_(*base_filters))
            .order_by(Notification.created_at.desc())
            .limit(limit)
        )
    )
    return NotificationListPage(rows=rows, total=total, limit=limit)


async def unread_count(db: AsyncSession, *, user_id: str) -> int:
    """Cheap badge query — uses the partial index `ix_notifications_user_unread`."""
    return int(
        await db.scalar(
            select(func.count())
            .select_from(Notification)
            .where(Notification.user_id == user_id)
            .where(Notification.read_at.is_(None))
        )
        or 0
    )


async def mark_read(
    db: AsyncSession, *, user_id: str, notification_id: str
) -> bool:
    """Set `read_at` to NOW() if owned by user. Idempotent.

    Returns True if a row was touched (existed + owned), False otherwise.
    Uses a targeted UPDATE so reading non-existent / not-owned ids returns
    False without leaking 404 vs 403 distinction.
    """
    result = await db.execute(
        update(Notification)
        .where(Notification.id == notification_id)
        .where(Notification.user_id == user_id)
        .where(Notification.read_at.is_(None))
        .values(read_at=_utcnow())
    )
    await db.commit()
    return (result.rowcount or 0) > 0


async def mark_acted(
    db: AsyncSession, *, user_id: str, notification_id: str
) -> bool:
    """Set `acted_at` AND `read_at` (acting implies seeing). Idempotent."""
    now = _utcnow()
    result = await db.execute(
        update(Notification)
        .where(Notification.id == notification_id)
        .where(Notification.user_id == user_id)
        .values(
            acted_at=now,
            # Also stamp read_at if not already.
            read_at=func.coalesce(Notification.read_at, now),
        )
    )
    await db.commit()
    return (result.rowcount or 0) > 0


async def mark_all_read(db: AsyncSession, *, user_id: str) -> int:
    """Bulk: mark all unread notifications for user as read. Returns count."""
    result = await db.execute(
        update(Notification)
        .where(Notification.user_id == user_id)
        .where(Notification.read_at.is_(None))
        .values(read_at=_utcnow())
    )
    await db.commit()
    return int(result.rowcount or 0)


async def cleanup_expired(db: AsyncSession) -> int:
    """Background-job entry: delete notifications past `expires_at`.

    Hourly cadence in production. Returns deleted count.
    """
    from sqlalchemy import delete as _delete

    result = await db.execute(
        _delete(Notification)
        .where(Notification.expires_at.is_not(None))
        .where(Notification.expires_at < _utcnow())
    )
    await db.commit()
    return int(result.rowcount or 0)


# ---------------------------------------------------------------------------
# Sync legacy helpers (preserved for §5 tool_layer scaffold; DQ-28)
# ---------------------------------------------------------------------------


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

    V0 sync path: structured log only. The async path
    (`create_notification(NotificationType.STEP_UP_REQUIRED, ...)`) is
    invoked separately by `step_up_service.create_pending_request`.
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
