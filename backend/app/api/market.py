"""Public market board API.

The board is the Project Deal-style shared discovery surface: active
buy/sell intents are visible enough for marketplace liquidity, while
strategic/private fields stay hidden.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import get_db
from app.core.rate_limit import limiter
from app.models.schema import Intent

router = APIRouter(prefix="/api/market", tags=["market"])


class MarketItem(BaseModel):
    intent_id: str
    side: str
    title: str
    description: str | None
    category: str
    public_price_eur: float = Field(
        ...,
        description=(
            "Public reservation price: seller floor for SELL, buyer cap for BUY."
        ),
    )
    currency: str
    location: str | None
    status: str
    created_at: datetime
    expires_at: datetime


class MarketListResponse(BaseModel):
    items: list[MarketItem]
    total: int
    limit: int
    offset: int


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _location_from_constraints(hard_constraints: dict[str, Any] | None) -> str | None:
    if not hard_constraints:
        return None
    location = hard_constraints.get("location")
    return location if isinstance(location, str) else None


def _market_item(intent: Intent) -> MarketItem:
    return MarketItem(
        intent_id=intent.id,
        side=intent.side,
        title=intent.title,
        description=intent.description,
        category=intent.category,
        public_price_eur=intent.reservation_price_cents / 100,
        currency=intent.currency,
        location=_location_from_constraints(intent.hard_constraints),
        status=intent.status,
        created_at=intent.created_at,
        expires_at=intent.expires_at,
    )


@router.get(
    "",
    response_model=MarketListResponse,
    summary="Public market board",
    description=(
        "List active public marketplace intents. No auth required. Strategic "
        "fields such as owner id, ideal price, soft preferences and "
        "negotiation transcripts are intentionally omitted."
    ),
)
@limiter.limit(lambda: settings.rate_limit_user_read)
async def list_market_endpoint(
    request: Request,
    db: AsyncSession = Depends(get_db),
    side: str | None = Query(default=None, pattern="^(buy|sell)$"),
    category: str | None = Query(default=None),
    location: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> MarketListResponse:
    filters = [Intent.status == "active", Intent.expires_at > _utcnow()]
    if side is not None:
        filters.append(Intent.side == side)
    if category is not None:
        filters.append(Intent.category == category)
    if location is not None:
        filters.append(Intent.hard_constraints["location"].astext == location)

    total = int(
        await db.scalar(
            select(func.count()).select_from(Intent).where(*filters)
        )
        or 0
    )
    rows = list(
        await db.scalars(
            select(Intent)
            .where(*filters)
            .order_by(Intent.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
    )
    return MarketListResponse(
        items=[_market_item(intent) for intent in rows],
        total=total,
        limit=limit,
        offset=offset,
    )
