"""Negotiations API — turn-based offer/counter-offer endpoints (brief task 5.1).

Five endpoints:

  POST /api/negotiations               — start or continue (tier ≥ 1)
  POST /api/negotiations/{id}/accept   — accept counterparty's last turn (tier ≥ 2)
  POST /api/negotiations/{id}/reject   — reject counterparty's last turn (tier ≥ 1)
  GET  /api/negotiations/{id}          — read state (tier ≥ 1, party-only)
  GET  /api/negotiations               — list caller's negotiations (tier ≥ 1)

Tier gating recap:
  - 1+ for start/continue/reject/read: agent can be `pending_mandate`
    (tier 1 has Self ZK proof but no mandate yet — they can still
    initiate / explore negotiations).
  - 2 for accept: accepting commits the user toward a deal (5.3), and
    deal closure requires an active mandate. Hard split.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.security import CurrentUser, require_tier
from app.services import negotiation_service

router = APIRouter(prefix="/api/negotiations", tags=["negotiations"])


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class StartOrContinueRequest(BaseModel):
    match_id: str
    agent_id: str
    price_cents: int = Field(gt=0)
    message: str | None = None


class AcceptRequest(BaseModel):
    agent_id: str


class RejectRequest(BaseModel):
    agent_id: str
    reason: str | None = None


class TurnPayload(BaseModel):
    turn_number: int
    agent_id: str
    type: str
    price_cents: int
    message: str
    timestamp: str


class TurnResponse(BaseModel):
    negotiation_id: str
    rounds_used: int
    max_rounds: int
    is_final_round: bool
    last_turn: TurnPayload
    status: str
    created_new: bool


class AcceptResponse(BaseModel):
    negotiation_id: str
    match_id: str
    agreed_price_cents: int
    next_step: str


class RejectResponse(BaseModel):
    negotiation_id: str
    match_id: str
    reason: str | None


class NegotiationStateResponse(BaseModel):
    negotiation_id: str
    match_id: str
    status: str
    rounds_used: int
    max_rounds: int
    is_final_round: bool
    final_status: str | None
    agreed_price_cents: int | None
    turns: list[TurnPayload]
    started_at: datetime
    closed_at: datetime | None


class NegotiationListItem(BaseModel):
    negotiation_id: str
    match_id: str
    status: str
    rounds_used: int
    max_rounds: int
    started_at: datetime
    closed_at: datetime | None


class NegotiationListResponse(BaseModel):
    negotiations: list[NegotiationListItem]
    total: int
    limit: int
    offset: int


# ---------------------------------------------------------------------------
# Error mapping
# ---------------------------------------------------------------------------


def _to_http(exc: negotiation_service.NegotiationError) -> HTTPException:
    return HTTPException(
        status_code=exc.http_status,
        detail={"code": exc.code, "message": str(exc)},
    )


def _state_to_response(nego) -> NegotiationStateResponse:
    state = nego.state or {}
    turns = [TurnPayload(**t) for t in state.get("turns") or []]
    return NegotiationStateResponse(
        negotiation_id=nego.id,
        match_id=nego.match_id,
        status=nego.status,
        rounds_used=nego.rounds_used or 0,
        max_rounds=nego.max_rounds or negotiation_service.MAX_ROUNDS,
        is_final_round=bool(state.get("is_final_round")),
        final_status=state.get("final_status"),
        agreed_price_cents=state.get("agreed_price_cents"),
        turns=turns,
        started_at=nego.started_at,
        closed_at=nego.closed_at,
    )


def _list_item(nego) -> NegotiationListItem:
    return NegotiationListItem(
        negotiation_id=nego.id,
        match_id=nego.match_id,
        status=nego.status,
        rounds_used=nego.rounds_used or 0,
        max_rounds=nego.max_rounds or negotiation_service.MAX_ROUNDS,
        started_at=nego.started_at,
        closed_at=nego.closed_at,
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("", response_model=TurnResponse)
async def start_or_continue_endpoint(
    body: StartOrContinueRequest,
    user: CurrentUser = Depends(require_tier(1)),
    db: AsyncSession = Depends(get_db),
) -> TurnResponse:
    try:
        result = await negotiation_service.start_or_continue(
            db,
            user_id=user.user_id,
            agent_id=body.agent_id,
            match_id=body.match_id,
            price_cents=body.price_cents,
            message=body.message,
        )
    except negotiation_service.NegotiationError as exc:
        raise _to_http(exc) from exc
    return TurnResponse(
        negotiation_id=result.negotiation_id,
        rounds_used=result.rounds_used,
        max_rounds=result.max_rounds,
        is_final_round=result.is_final_round,
        last_turn=TurnPayload(**result.last_turn),
        status=result.status,
        created_new=result.created_new,
    )


@router.post("/{negotiation_id}/accept", response_model=AcceptResponse)
async def accept_endpoint(
    negotiation_id: str,
    body: AcceptRequest,
    user: CurrentUser = Depends(require_tier(2)),
    db: AsyncSession = Depends(get_db),
) -> AcceptResponse:
    try:
        result = await negotiation_service.accept_offer(
            db,
            user_id=user.user_id,
            agent_id=body.agent_id,
            negotiation_id=negotiation_id,
        )
    except negotiation_service.NegotiationError as exc:
        raise _to_http(exc) from exc
    return AcceptResponse(
        negotiation_id=result.negotiation_id,
        match_id=result.match_id,
        agreed_price_cents=result.agreed_price_cents,
        next_step=result.next_step,
    )


@router.post("/{negotiation_id}/reject", response_model=RejectResponse)
async def reject_endpoint(
    negotiation_id: str,
    body: RejectRequest,
    user: CurrentUser = Depends(require_tier(1)),
    db: AsyncSession = Depends(get_db),
) -> RejectResponse:
    try:
        result = await negotiation_service.reject_offer(
            db,
            user_id=user.user_id,
            agent_id=body.agent_id,
            negotiation_id=negotiation_id,
            reason=body.reason,
        )
    except negotiation_service.NegotiationError as exc:
        raise _to_http(exc) from exc
    return RejectResponse(
        negotiation_id=result.negotiation_id,
        match_id=result.match_id,
        reason=result.reason,
    )


@router.get("/{negotiation_id}", response_model=NegotiationStateResponse)
async def get_negotiation_endpoint(
    negotiation_id: str,
    user: CurrentUser = Depends(require_tier(1)),
    db: AsyncSession = Depends(get_db),
) -> NegotiationStateResponse:
    try:
        nego = await negotiation_service.get_negotiation_state(
            db, user_id=user.user_id, negotiation_id=negotiation_id
        )
    except negotiation_service.NegotiationError as exc:
        raise _to_http(exc) from exc
    return _state_to_response(nego)


@router.get("", response_model=NegotiationListResponse)
async def list_negotiations_endpoint(
    user: CurrentUser = Depends(require_tier(1)),
    db: AsyncSession = Depends(get_db),
    status: str | None = Query(default=None),
    limit: int = Query(
        default=negotiation_service.DEFAULT_LIST_LIMIT, ge=1, le=50
    ),
    offset: int = Query(default=0, ge=0),
) -> NegotiationListResponse:
    page = await negotiation_service.list_negotiations_for_user(
        db,
        user_id=user.user_id,
        status=status,
        limit=limit,
        offset=offset,
    )
    return NegotiationListResponse(
        negotiations=[_list_item(n) for n in page.rows],
        total=page.total,
        limit=page.limit,
        offset=page.offset,
    )
