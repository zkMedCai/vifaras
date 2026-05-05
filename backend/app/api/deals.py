"""Deals API — list/detail + sign/cancel signing flow + chat (brief task 5.3).

Nine endpoints, all `tier ≥ 2`:

  GET  /api/deals                           — list caller's deals
  GET  /api/deals/{id}                      — detail (party-only)
  GET  /api/deals/{id}/trade-window         — confirmed-deal logistics window
  POST /api/deals/{id}/sign/draft           — buyer/seller request sign payload
  POST /api/deals/{id}/sign/submit          — verify + apply signature
  POST /api/deals/{id}/cancel/draft         — request cancel payload
  POST /api/deals/{id}/cancel/submit        — verify + cancel
  POST /api/deals/{id}/messages             — send E2E chat message
  GET  /api/deals/{id}/messages             — read chat history
"""
from __future__ import annotations

import base64
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import get_db
from app.core.rate_limit import limiter, user_key
from app.core.security import CurrentUser, require_tier
from app.services import (
    deal_message_service,
    deal_service,
    deal_trade_window_service,
)
from app.services.mandate_service import WebAuthnAssertionPayload

router = APIRouter(prefix="/api/deals", tags=["deals"])


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class CancelDraftRequest(BaseModel):
    reason: str | None = None


class SignSubmitRequest(BaseModel):
    draft_id: str
    webauthn_assertion: WebAuthnAssertionPayload


class CancelSubmitRequest(BaseModel):
    draft_id: str
    webauthn_assertion: WebAuthnAssertionPayload


class SignDraftResponse(BaseModel):
    draft_id: str
    payload: dict[str, Any]
    payload_summary: dict[str, Any] = Field(default_factory=dict)
    challenge: str
    expires_at_utc: datetime
    role: str
    kind: str


class SignSubmitResponse(BaseModel):
    deal_id: str
    role: str
    signed_at: datetime
    deal_confirmed: bool
    confirmed_at: datetime | None


class CancelSubmitResponse(BaseModel):
    deal_id: str
    cancelled_at: datetime
    cancellation_reason: str
    intents_reverted: int
    matches_reverted: int


class DealDetailResponse(BaseModel):
    deal_id: str
    negotiation_id: str
    buyer_user_id: str
    seller_user_id: str
    buy_intent_id: str
    sell_intent_id: str
    agreed_price_cents: int
    currency: str
    status: str
    buyer_signed_at: datetime | None
    seller_signed_at: datetime | None
    expires_at: datetime
    confirmed_at: datetime | None
    cancelled_at: datetime | None
    cancellation_reason: str | None
    created_at: datetime


class DealListItem(BaseModel):
    deal_id: str
    status: str
    agreed_price_cents: int
    currency: str
    expires_at: datetime
    confirmed_at: datetime | None
    created_at: datetime


class DealListResponse(BaseModel):
    deals: list[DealListItem]
    total: int
    limit: int
    offset: int


class TradeWindowResponse(BaseModel):
    deal_id: str
    status: str
    buyer_user_id: str
    seller_user_id: str
    terms_summary: dict[str, Any]
    confirmed_at: datetime
    expires_at: datetime | None
    shipping_status: str
    next_required_action: str


class SendMessageRequest(BaseModel):
    encrypted_content_b64: str
    nonce_b64: str


class MessageItem(BaseModel):
    message_id: str
    sender_user_id: str
    encrypted_content_b64: str
    nonce_b64: str
    sent_at: datetime


class MessageListResponse(BaseModel):
    messages: list[MessageItem]
    total: int
    limit: int


# ---------------------------------------------------------------------------
# Error mapping
# ---------------------------------------------------------------------------


def _to_http(exc: deal_service.DealError) -> HTTPException:
    return HTTPException(
        status_code=exc.http_status,
        detail={"code": exc.code, "message": str(exc)},
    )


def _deal_to_detail(deal) -> DealDetailResponse:
    return DealDetailResponse(
        deal_id=deal.id,
        negotiation_id=deal.negotiation_id,
        buyer_user_id=deal.buyer_user_id,
        seller_user_id=deal.seller_user_id,
        buy_intent_id=deal.buy_intent_id,
        sell_intent_id=deal.sell_intent_id,
        agreed_price_cents=deal.agreed_price_cents,
        currency=deal.currency,
        status=deal.status,
        buyer_signed_at=deal.buyer_signed_at,
        seller_signed_at=deal.seller_signed_at,
        expires_at=deal.expires_at,
        confirmed_at=deal.confirmed_at,
        cancelled_at=deal.cancelled_at,
        cancellation_reason=deal.cancellation_reason,
        created_at=deal.created_at,
    )


def _deal_to_list_item(deal) -> DealListItem:
    return DealListItem(
        deal_id=deal.id,
        status=deal.status,
        agreed_price_cents=deal.agreed_price_cents,
        currency=deal.currency,
        expires_at=deal.expires_at,
        confirmed_at=deal.confirmed_at,
        created_at=deal.created_at,
    )


def _trade_window_to_response(state) -> TradeWindowResponse:
    return TradeWindowResponse(
        deal_id=state.deal_id,
        status=state.status,
        buyer_user_id=state.buyer_user_id,
        seller_user_id=state.seller_user_id,
        terms_summary=state.terms_summary,
        confirmed_at=state.confirmed_at,
        expires_at=state.expires_at,
        shipping_status=state.shipping_status,
        next_required_action=state.next_required_action,
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("", response_model=DealListResponse)
@limiter.limit(lambda: settings.rate_limit_user_read, key_func=user_key)
async def list_deals_endpoint(
    request: Request,
    user: CurrentUser = Depends(require_tier(2)),
    db: AsyncSession = Depends(get_db),
    status: str | None = Query(default=None),
    limit: int = Query(default=deal_service.DEFAULT_LIST_LIMIT, ge=1, le=50),
    offset: int = Query(default=0, ge=0),
) -> DealListResponse:
    page = await deal_service.list_deals_for_user(
        db,
        user_id=user.user_id,
        status=status,
        limit=limit,
        offset=offset,
    )
    return DealListResponse(
        deals=[_deal_to_list_item(d) for d in page.rows],
        total=page.total,
        limit=page.limit,
        offset=page.offset,
    )


@router.get("/{deal_id}", response_model=DealDetailResponse)
@limiter.limit(lambda: settings.rate_limit_user_read, key_func=user_key)
async def get_deal_endpoint(
    request: Request,
    deal_id: str,
    user: CurrentUser = Depends(require_tier(2)),
    db: AsyncSession = Depends(get_db),
) -> DealDetailResponse:
    try:
        deal = await deal_service.get_deal_for_user(
            db, user_id=user.user_id, deal_id=deal_id
        )
    except deal_service.DealError as exc:
        raise _to_http(exc) from exc
    return _deal_to_detail(deal)


@router.get("/{deal_id}/trade-window", response_model=TradeWindowResponse)
@limiter.limit(lambda: settings.rate_limit_user_read, key_func=user_key)
async def get_trade_window_endpoint(
    request: Request,
    deal_id: str,
    user: CurrentUser = Depends(require_tier(2)),
    db: AsyncSession = Depends(get_db),
) -> TradeWindowResponse:
    try:
        state = await deal_trade_window_service.get_trade_window_for_user(
            db, user_id=user.user_id, deal_id=deal_id
        )
    except deal_service.DealError as exc:
        raise _to_http(exc) from exc
    return _trade_window_to_response(state)


@router.post("/{deal_id}/sign/draft", response_model=SignDraftResponse)
@limiter.limit(lambda: settings.rate_limit_post_strict, key_func=user_key)
async def sign_draft_endpoint(
    request: Request,
    deal_id: str,
    user: CurrentUser = Depends(require_tier(2)),
    db: AsyncSession = Depends(get_db),
) -> SignDraftResponse:
    try:
        result = await deal_service.request_sign_draft(
            db, user_id=user.user_id, deal_id=deal_id
        )
    except deal_service.DealError as exc:
        raise _to_http(exc) from exc
    return SignDraftResponse(
        draft_id=result.draft_id,
        payload=result.payload,
        challenge=result.challenge_b64url,
        expires_at_utc=result.expires_at_utc,
        role=result.role,
        kind=result.kind,
    )


@router.post("/{deal_id}/sign/submit", response_model=SignSubmitResponse)
@limiter.limit(lambda: settings.rate_limit_mandate_critical, key_func=user_key)
async def sign_submit_endpoint(
    request: Request,
    deal_id: str,
    body: SignSubmitRequest,
    user: CurrentUser = Depends(require_tier(2)),
    db: AsyncSession = Depends(get_db),
) -> SignSubmitResponse:
    try:
        result = await deal_service.submit_signature(
            db,
            user_id=user.user_id,
            draft_id=body.draft_id,
            assertion=body.webauthn_assertion,
        )
    except deal_service.DealError as exc:
        raise _to_http(exc) from exc
    return SignSubmitResponse(
        deal_id=result.deal_id,
        role=result.role,
        signed_at=result.signed_at,
        deal_confirmed=result.deal_confirmed,
        confirmed_at=result.confirmed_at,
    )


@router.post("/{deal_id}/cancel/draft", response_model=SignDraftResponse)
@limiter.limit(lambda: settings.rate_limit_post_strict, key_func=user_key)
async def cancel_draft_endpoint(
    request: Request,
    deal_id: str,
    body: CancelDraftRequest = CancelDraftRequest(),
    user: CurrentUser = Depends(require_tier(2)),
    db: AsyncSession = Depends(get_db),
) -> SignDraftResponse:
    # `body.reason` is reserved for the future audit-context payload; V0
    # always uses the canonical `user_cancelled` reason at submit time.
    try:
        result = await deal_service.request_cancel_draft(
            db, user_id=user.user_id, deal_id=deal_id
        )
    except deal_service.DealError as exc:
        raise _to_http(exc) from exc
    return SignDraftResponse(
        draft_id=result.draft_id,
        payload=result.payload,
        challenge=result.challenge_b64url,
        expires_at_utc=result.expires_at_utc,
        role=result.role,
        kind=result.kind,
    )


@router.post("/{deal_id}/cancel/submit", response_model=CancelSubmitResponse)
@limiter.limit(lambda: settings.rate_limit_mandate_critical, key_func=user_key)
async def cancel_submit_endpoint(
    request: Request,
    deal_id: str,
    body: CancelSubmitRequest,
    user: CurrentUser = Depends(require_tier(2)),
    db: AsyncSession = Depends(get_db),
) -> CancelSubmitResponse:
    try:
        result = await deal_service.submit_cancel(
            db,
            user_id=user.user_id,
            draft_id=body.draft_id,
            assertion=body.webauthn_assertion,
        )
    except deal_service.DealError as exc:
        raise _to_http(exc) from exc
    return CancelSubmitResponse(
        deal_id=result.deal_id,
        cancelled_at=result.cancelled_at,
        cancellation_reason=result.cancellation_reason,
        intents_reverted=result.intents_reverted,
        matches_reverted=result.matches_reverted,
    )


# ---------------------------------------------------------------------------
# Chat endpoints
# ---------------------------------------------------------------------------


def _b64decode(s: str) -> bytes:
    return base64.b64decode(s.encode("ascii"))


def _b64encode(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


@router.post("/{deal_id}/messages", response_model=MessageItem, status_code=201)
@limiter.limit(lambda: settings.rate_limit_post_strict, key_func=user_key)
async def send_message_endpoint(
    request: Request,
    deal_id: str,
    body: SendMessageRequest,
    user: CurrentUser = Depends(require_tier(2)),
    db: AsyncSession = Depends(get_db),
) -> MessageItem:
    try:
        msg = await deal_message_service.send_message(
            db,
            user_id=user.user_id,
            deal_id=deal_id,
            encrypted_content=_b64decode(body.encrypted_content_b64),
            nonce=_b64decode(body.nonce_b64),
        )
    except deal_service.DealError as exc:
        raise _to_http(exc) from exc
    return MessageItem(
        message_id=msg.id,
        sender_user_id=msg.sender_user_id,
        encrypted_content_b64=_b64encode(bytes(msg.encrypted_content)),
        nonce_b64=_b64encode(bytes(msg.nonce)),
        sent_at=msg.sent_at,
    )


@router.get("/{deal_id}/messages", response_model=MessageListResponse)
@limiter.limit(lambda: settings.rate_limit_user_read, key_func=user_key)
async def list_messages_endpoint(
    request: Request,
    deal_id: str,
    user: CurrentUser = Depends(require_tier(2)),
    db: AsyncSession = Depends(get_db),
    limit: int = Query(
        default=deal_message_service.DEFAULT_LIST_LIMIT, ge=1, le=100
    ),
    before_id: str | None = Query(default=None),
) -> MessageListResponse:
    try:
        page = await deal_message_service.list_messages(
            db,
            user_id=user.user_id,
            deal_id=deal_id,
            limit=limit,
            before_id=before_id,
        )
    except deal_service.DealError as exc:
        raise _to_http(exc) from exc
    return MessageListResponse(
        messages=[
            MessageItem(
                message_id=m.id,
                sender_user_id=m.sender_user_id,
                encrypted_content_b64=_b64encode(bytes(m.encrypted_content)),
                nonce_b64=_b64encode(bytes(m.nonce)),
                sent_at=m.sent_at,
            )
            for m in page.rows
        ],
        total=page.total,
        limit=page.limit,
    )
