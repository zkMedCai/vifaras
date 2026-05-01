"""Matches API — list per intent + detail (brief task 4.3).

Two endpoints:

  - `GET /api/intents/{intent_id}/matches` — owner-only list view.
    Returns top-N matches with score breakdown + counterparty intent
    summary. **Privacy compromise**: counterparty `reservation_price_eur`
    is exposed (the existence of price overlap already implies it),
    `ideal_price_eur` is NOT (that's the strategic info we keep private).
    See DESIGN_QUESTIONS DQ-31.

  - `GET /api/matches/{match_id}` — detail endpoint for tier-2 callers
    (i.e. agents driving negotiation in 5.x). Surfaces both intents in
    full, including ideal prices, because the agent needs them to
    negotiate. tier ≥ 0 callers shouldn't see this view at all.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import get_db
from app.core.rate_limit import limiter, user_key
from app.core.security import CurrentUser, require_tier
from app.services import match_service

router = APIRouter(tags=["matches"])


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class CounterpartyIntentSummary(BaseModel):
    """Privacy-aware view: reservation visible, ideal hidden (DQ-31)."""

    intent_id: str
    side: str
    title: str
    category: str
    reservation_price_eur: float
    # ideal_price_eur intentionally omitted in list view.


class MatchScores(BaseModel):
    similarity: float
    price_proximity: float
    combined: float


class MatchListItem(BaseModel):
    match_id: str
    counterparty_intent: CounterpartyIntentSummary
    scores: MatchScores
    status: str
    discovered_at: datetime


class MatchListResponse(BaseModel):
    intent_id: str
    matches: list[MatchListItem]
    total: int
    limit: int
    offset: int


class MatchDetailIntent(BaseModel):
    intent_id: str
    user_id: str
    side: str
    title: str
    description: str | None
    category: str
    reservation_price_eur: float
    ideal_price_eur: float
    status: str


class MatchDetailResponse(BaseModel):
    match_id: str
    buy_intent: MatchDetailIntent
    sell_intent: MatchDetailIntent
    scores: MatchScores
    status: str
    discovered_at: datetime


# ---------------------------------------------------------------------------
# Error mapping
# ---------------------------------------------------------------------------


def _to_http(exc: match_service.MatchError) -> HTTPException:
    return HTTPException(
        status_code=exc.http_status,
        detail={"code": exc.code, "message": str(exc)},
    )


def _intent_to_detail(intent) -> MatchDetailIntent:
    return MatchDetailIntent(
        intent_id=intent.id,
        user_id=intent.user_id,
        side=intent.side,
        title=intent.title,
        description=intent.description,
        category=intent.category,
        reservation_price_eur=intent.reservation_price_cents / 100,
        ideal_price_eur=intent.ideal_price_cents / 100,
        status=intent.status,
    )


def _counterparty_intent(intent) -> CounterpartyIntentSummary:
    return CounterpartyIntentSummary(
        intent_id=intent.id,
        side=intent.side,
        title=intent.title,
        category=intent.category,
        reservation_price_eur=intent.reservation_price_cents / 100,
    )


def _match_scores(match) -> MatchScores:
    return MatchScores(
        similarity=float(match.similarity_score or 0.0),
        price_proximity=float(match.price_proximity_score or 0.0),
        combined=float(match.combined_score or 0.0),
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get(
    "/api/intents/{intent_id}/matches", response_model=MatchListResponse
)
@limiter.limit(lambda: settings.rate_limit_user_read, key_func=user_key)
async def list_intent_matches(
    request: Request,
    intent_id: str,
    user: CurrentUser = Depends(require_tier(0)),
    db: AsyncSession = Depends(get_db),
    limit: int = Query(default=match_service.DEFAULT_MATCH_LIMIT, ge=1, le=50),
    offset: int = Query(default=0, ge=0),
    min_score: float = Query(default=0.0, ge=0.0, le=1.0),
) -> MatchListResponse:
    try:
        page = await match_service.list_matches_for_intent(
            db,
            user_id=user.user_id,
            intent_id=intent_id,
            limit=limit,
            offset=offset,
            min_score=min_score,
        )
    except match_service.MatchError as exc:
        raise _to_http(exc) from exc

    # Build the counterparty summary for each row by loading the OPPOSITE
    # intent. The user owns one side of each match (enforced by
    # `list_matches_for_intent`), so we identify the counterparty as the
    # side that isn't `intent_id`.
    from app.models.schema import Intent

    items: list[MatchListItem] = []
    for match in page.rows:
        counterparty_intent_id = (
            match.sell_intent_id
            if match.buy_intent_id == intent_id
            else match.buy_intent_id
        )
        counterparty = await db.get(Intent, counterparty_intent_id)
        if counterparty is None:  # pragma: no cover — FK guarantees existence
            continue
        items.append(
            MatchListItem(
                match_id=match.id,
                counterparty_intent=_counterparty_intent(counterparty),
                scores=_match_scores(match),
                status=match.status,
                discovered_at=match.created_at,
            )
        )

    return MatchListResponse(
        intent_id=intent_id,
        matches=items,
        total=page.total,
        limit=page.limit,
        offset=page.offset,
    )


@router.get("/api/matches/{match_id}", response_model=MatchDetailResponse)
@limiter.limit(lambda: settings.rate_limit_user_read, key_func=user_key)
async def get_match_detail(
    request: Request,
    match_id: str,
    user: CurrentUser = Depends(require_tier(2)),
    db: AsyncSession = Depends(get_db),
) -> MatchDetailResponse:
    try:
        match = await match_service.get_match_for_user(
            db, user_id=user.user_id, match_id=match_id
        )
    except match_service.MatchError as exc:
        raise _to_http(exc) from exc

    from app.models.schema import Intent

    buy_intent = await db.get(Intent, match.buy_intent_id)
    sell_intent = await db.get(Intent, match.sell_intent_id)

    return MatchDetailResponse(
        match_id=match.id,
        buy_intent=_intent_to_detail(buy_intent),
        sell_intent=_intent_to_detail(sell_intent),
        scores=_match_scores(match),
        status=match.status,
        discovered_at=match.created_at,
    )
