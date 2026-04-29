"""Notifications API — list + read/acted/clear (brief task 6.1).

Five endpoints, all `tier ≥ 0` (anonymous tier-0 users still have
notifications, e.g. NEW_MATCH_DISCOVERED on intents they created):

  GET  /api/notifications              — list, optional unread + category filters
  GET  /api/notifications/unread-count — single integer for UI badge
  POST /api/notifications/{id}/read    — mark a notification as read
  POST /api/notifications/{id}/acted   — mark as acted (user took action)
  POST /api/notifications/mark-all-read — bulk mark unread → read

Auth model: each call is gated to `user_id` from the JWT. Targeted
UPDATEs in the service ensure non-existent / not-owned ids return False
without distinguishing 404 vs 403 — this avoids leaking existence info
about other users' notifications.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.security import CurrentUser, require_tier
from app.services import notification_service

router = APIRouter(prefix="/api/notifications", tags=["notifications"])


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class NotificationItem(BaseModel):
    notification_id: str
    type: str
    category: str
    title: str
    body: str
    payload: dict[str, Any]
    read_at: datetime | None
    acted_at: datetime | None
    expires_at: datetime | None
    created_at: datetime


class NotificationListResponse(BaseModel):
    notifications: list[NotificationItem]
    total: int
    limit: int


class UnreadCountResponse(BaseModel):
    unread_count: int


class MarkResponse(BaseModel):
    ok: bool


class MarkAllResponse(BaseModel):
    ok: bool
    marked_count: int


def _to_item(row) -> NotificationItem:
    return NotificationItem(
        notification_id=row.id,
        type=row.type,
        category=row.category,
        title=row.title,
        body=row.body,
        payload=row.payload or {},
        read_at=row.read_at,
        acted_at=row.acted_at,
        expires_at=row.expires_at,
        created_at=row.created_at,
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("", response_model=NotificationListResponse)
async def list_notifications_endpoint(
    user: CurrentUser = Depends(require_tier(0)),
    db: AsyncSession = Depends(get_db),
    unread_only: bool = Query(default=False),
    category: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    before_id: str | None = Query(default=None),
) -> NotificationListResponse:
    cat_enum: notification_service.NotificationCategory | None = None
    if category is not None:
        try:
            cat_enum = notification_service.NotificationCategory(category)
        except ValueError as exc:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "invalid_category",
                    "message": f"unknown category {category!r}",
                },
            ) from exc

    page = await notification_service.list_notifications(
        db,
        user_id=user.user_id,
        unread_only=unread_only,
        category=cat_enum,
        limit=limit,
        before_id=before_id,
    )
    return NotificationListResponse(
        notifications=[_to_item(r) for r in page.rows],
        total=page.total,
        limit=page.limit,
    )


@router.get("/unread-count", response_model=UnreadCountResponse)
async def unread_count_endpoint(
    user: CurrentUser = Depends(require_tier(0)),
    db: AsyncSession = Depends(get_db),
) -> UnreadCountResponse:
    count = await notification_service.unread_count(db, user_id=user.user_id)
    return UnreadCountResponse(unread_count=count)


@router.post("/{notification_id}/read", response_model=MarkResponse)
async def mark_read_endpoint(
    notification_id: str,
    user: CurrentUser = Depends(require_tier(0)),
    db: AsyncSession = Depends(get_db),
) -> MarkResponse:
    ok = await notification_service.mark_read(
        db, user_id=user.user_id, notification_id=notification_id
    )
    return MarkResponse(ok=ok)


@router.post("/{notification_id}/acted", response_model=MarkResponse)
async def mark_acted_endpoint(
    notification_id: str,
    user: CurrentUser = Depends(require_tier(0)),
    db: AsyncSession = Depends(get_db),
) -> MarkResponse:
    ok = await notification_service.mark_acted(
        db, user_id=user.user_id, notification_id=notification_id
    )
    return MarkResponse(ok=ok)


@router.post("/mark-all-read", response_model=MarkAllResponse)
async def mark_all_read_endpoint(
    user: CurrentUser = Depends(require_tier(0)),
    db: AsyncSession = Depends(get_db),
) -> MarkAllResponse:
    count = await notification_service.mark_all_read(
        db, user_id=user.user_id
    )
    return MarkAllResponse(ok=True, marked_count=count)
