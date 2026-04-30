"""Intents API — BUY/SELL CRUD (brief task 4.1)."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import get_db
from app.core.rate_limit import limiter
from app.core.security import CurrentUser, require_tier
from app.services import intent_service, negotiation_service

router = APIRouter(prefix="/api/intents", tags=["intents"])


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class CreateIntentRequest(BaseModel):
    side: str
    title: str
    description: str | None = None
    category: str
    reservation_price_eur: float
    ideal_price_eur: float
    duration_days: int = intent_service.DEFAULT_DURATION_DAYS
    hard_constraints: dict[str, Any] = Field(default_factory=dict)
    soft_preferences: dict[str, Any] = Field(default_factory=dict)
    currency: str = "EUR"


class UpdateIntentRequest(BaseModel):
    title: str | None = None
    description: str | None = None
    reservation_price_eur: float | None = None
    ideal_price_eur: float | None = None
    duration_days: int | None = None
    soft_preferences: dict[str, Any] | None = None
    category: str | None = None
    side: str | None = None


class IntentResponse(BaseModel):
    intent_id: str
    side: str
    title: str
    description: str | None
    category: str
    reservation_price_eur: float
    ideal_price_eur: float
    currency: str
    hard_constraints: dict[str, Any]
    soft_preferences: dict[str, Any]
    status: str
    expires_at: datetime
    created_at: datetime
    closed_at: datetime | None


class CreateIntentResponse(BaseModel):
    intent_id: str
    status: str
    expires_at: datetime
    embedding_generated: bool = True


class IntentListItem(BaseModel):
    intent_id: str
    side: str
    title: str
    category: str
    reservation_price_eur: float
    ideal_price_eur: float
    status: str
    expires_at: datetime
    created_at: datetime


class IntentListResponse(BaseModel):
    intents: list[IntentListItem]
    total: int
    limit: int
    offset: int


class CancelIntentResponse(BaseModel):
    intent_id: str
    status: str
    already_cancelled: bool
    negotiations_cancelled: int
    matches_expired: int


# ---------------------------------------------------------------------------
# Error mapping
# ---------------------------------------------------------------------------


def _to_http(
    exc: intent_service.IntentError | negotiation_service.NegotiationError,
) -> HTTPException:
    detail: dict[str, Any] = {"code": exc.code, "message": str(exc)}
    if isinstance(exc, intent_service.TooManyActiveIntents):
        detail["next_step"] = {
            "action": "upgrade_tier",
            "description": (
                "Aumenta il tier per sbloccare più intent attivi simultanei."
            ),
        }
    return HTTPException(status_code=exc.http_status, detail=detail)


def _intent_to_response(intent) -> IntentResponse:
    return IntentResponse(
        intent_id=intent.id,
        side=intent.side,
        title=intent.title,
        description=intent.description,
        category=intent.category,
        reservation_price_eur=intent.reservation_price_cents / 100,
        ideal_price_eur=intent.ideal_price_cents / 100,
        currency=intent.currency,
        hard_constraints=intent.hard_constraints or {},
        soft_preferences=intent.soft_preferences or {},
        status=intent.status,
        expires_at=intent.expires_at,
        created_at=intent.created_at,
        closed_at=intent.closed_at,
    )


def _intent_to_list_item(intent) -> IntentListItem:
    return IntentListItem(
        intent_id=intent.id,
        side=intent.side,
        title=intent.title,
        category=intent.category,
        reservation_price_eur=intent.reservation_price_cents / 100,
        ideal_price_eur=intent.ideal_price_cents / 100,
        status=intent.status,
        expires_at=intent.expires_at,
        created_at=intent.created_at,
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post(
    "",
    response_model=CreateIntentResponse,
    status_code=201,
    summary="Create a new intent",
    description=(
        "Create a buy or sell intent. Tier 0+ allowed (intents can be "
        "drafted before mandate signing). Returns the persisted intent "
        "with its computed embedding."
    ),
)
@limiter.limit(lambda: settings.rate_limit_post_strict)
async def create_intent_endpoint(
    request: Request,
    body: CreateIntentRequest,
    user: CurrentUser = Depends(require_tier(0)),
    db: AsyncSession = Depends(get_db),
) -> CreateIntentResponse:
    try:
        intent = await intent_service.create_intent(
            db,
            user_id=user.user_id,
            input=intent_service.CreateIntentInput(**body.model_dump()),
        )
    except intent_service.IntentError as exc:
        raise _to_http(exc) from exc
    return CreateIntentResponse(
        intent_id=intent.id,
        status=intent.status,
        expires_at=intent.expires_at,
    )


@router.get("", response_model=IntentListResponse)
async def list_intents_endpoint(
    user: CurrentUser = Depends(require_tier(0)),
    db: AsyncSession = Depends(get_db),
    status: str | None = Query(default=None),
    side: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> IntentListResponse:
    page = await intent_service.list_user_intents(
        db,
        user_id=user.user_id,
        status=status,
        side=side,
        limit=limit,
        offset=offset,
    )
    return IntentListResponse(
        intents=[_intent_to_list_item(i) for i in page.rows],
        total=page.total,
        limit=page.limit,
        offset=page.offset,
    )


@router.get("/{intent_id}", response_model=IntentResponse)
async def get_intent_endpoint(
    intent_id: str,
    user: CurrentUser = Depends(require_tier(0)),
    db: AsyncSession = Depends(get_db),
) -> IntentResponse:
    intent = await intent_service.get_intent_for_user(
        db, user_id=user.user_id, intent_id=intent_id
    )
    if intent is None:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "intent_not_found",
                "message": f"intent {intent_id!r} not found",
            },
        )
    return _intent_to_response(intent)


@router.patch("/{intent_id}", response_model=IntentResponse)
async def update_intent_endpoint(
    intent_id: str,
    body: UpdateIntentRequest,
    user: CurrentUser = Depends(require_tier(0)),
    db: AsyncSession = Depends(get_db),
) -> IntentResponse:
    try:
        intent = await intent_service.update_intent(
            db,
            user_id=user.user_id,
            user_tier=user.tier,
            intent_id=intent_id,
            input=intent_service.UpdateIntentInput(**body.model_dump()),
        )
    except intent_service.IntentError as exc:
        raise _to_http(exc) from exc
    return _intent_to_response(intent)


@router.delete("/{intent_id}", response_model=CancelIntentResponse)
async def cancel_intent_endpoint(
    intent_id: str,
    user: CurrentUser = Depends(require_tier(0)),
    db: AsyncSession = Depends(get_db),
) -> CancelIntentResponse:
    try:
        result = await intent_service.cancel_intent(
            db, user_id=user.user_id, intent_id=intent_id
        )
    except (
        intent_service.IntentError,
        negotiation_service.NegotiationError,
    ) as exc:
        # cancel_intent can raise IntentAlreadyMatched (NegotiationError)
        # when a competing accept already promoted the intent to `matched`.
        raise _to_http(exc) from exc
    return CancelIntentResponse(
        intent_id=result.intent.id,
        status=result.intent.status,
        already_cancelled=result.already_cancelled,
        negotiations_cancelled=result.negotiations_cancelled,
        matches_expired=result.matches_expired,
    )
