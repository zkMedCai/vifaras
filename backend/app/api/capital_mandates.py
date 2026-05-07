"""Autonomous Capital Mandate API."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import get_db
from app.core.rate_limit import limiter, user_key
from app.core.security import CurrentUser, require_tier
from app.services import capital_ledger_service, capital_mandate_service
from app.services.mandate_service import WebAuthnAssertionPayload

router = APIRouter(prefix="/api/capital-mandates", tags=["capital-mandates"])


class DraftRequest(capital_mandate_service.CapitalMandateDraftInput):
    pass


class DraftResponse(BaseModel):
    draft_id: str
    payload: dict[str, Any]
    payload_summary: dict[str, Any]
    challenge: str
    expires_at_utc: datetime


class SubmitRequest(BaseModel):
    draft_id: str
    webauthn_assertion: WebAuthnAssertionPayload


class BudgetStateResponse(BaseModel):
    budget_total_cents: int
    reserved_cents: int
    committed_cents: int
    available_cents: int
    realized_pnl_cents: int


class CapitalMandateResponse(BaseModel):
    capital_mandate_id: str
    user_id: str
    agent_id: str
    base_mandate_id: str
    status: str
    budget_total_cents: int
    currency: str
    starts_at: datetime
    expires_at: datetime
    duration_days: int
    max_single_purchase_cents: int
    max_open_positions: int
    max_daily_deals: int | None
    min_expected_margin_bps: int
    max_total_loss_cents: int | None
    risk_level: str
    auto_buy: bool
    auto_sell: bool
    auto_relist: bool
    requires_manual_approval: bool
    allowed_categories: list[str]
    forbidden_categories: list[str]
    geo_scope: list[str]
    constraints: dict[str, Any]
    created_at: datetime
    activated_at: datetime | None
    paused_at: datetime | None
    revoked_at: datetime | None
    revocation_reason: str | None
    settled_at: datetime | None
    budget_state: BudgetStateResponse | None = None
    positions_summary: dict[str, int] | None = None


class SubmitResponse(BaseModel):
    capital_mandate_id: str
    status: str
    budget_state: BudgetStateResponse
    expires_at: datetime


class ActiveResponse(BaseModel):
    active: bool
    mandate: CapitalMandateResponse | None
    budget_state: BudgetStateResponse | None
    positions_summary: dict[str, int] | None


class RevokeRequest(BaseModel):
    reason: str | None = None


class LedgerEntryResponse(BaseModel):
    id: str
    capital_mandate_id: str
    user_id: str
    agent_id: str
    deal_id: str | None
    position_id: str | None
    type: str
    amount_cents: int
    currency: str
    reason: str
    metadata: dict[str, Any]
    idempotency_key: str
    created_at: datetime


class LedgerResponse(BaseModel):
    entries: list[LedgerEntryResponse]
    budget_state: BudgetStateResponse


class PositionResponse(BaseModel):
    id: str
    capital_mandate_id: str
    user_id: str
    agent_id: str
    source_buy_deal_id: str | None
    resale_sell_deal_id: str | None
    item_snapshot: dict[str, Any]
    status: str
    purchase_price_cents: int | None
    expected_resale_price_cents: int | None
    expected_profit_cents: int | None
    expected_margin_bps: int | None
    realized_sale_price_cents: int | None
    realized_profit_cents: int | None
    created_at: datetime
    updated_at: datetime | None
    closed_at: datetime | None


class PositionsResponse(BaseModel):
    positions: list[PositionResponse]


def _to_http(exc: capital_mandate_service.CapitalMandateError) -> HTTPException:
    return HTTPException(
        status_code=exc.http_status,
        detail={"code": exc.code, "message": str(exc)},
    )


def _mandate_response(
    mandate,
    *,
    budget_state: dict[str, int] | None = None,
    positions_summary: dict[str, int] | None = None,
) -> CapitalMandateResponse:
    return CapitalMandateResponse(
        capital_mandate_id=mandate.id,
        user_id=mandate.user_id,
        agent_id=mandate.agent_id,
        base_mandate_id=mandate.base_mandate_id,
        status=mandate.status,
        budget_total_cents=mandate.budget_total_cents,
        currency=mandate.currency,
        starts_at=mandate.starts_at,
        expires_at=mandate.expires_at,
        duration_days=mandate.duration_days,
        max_single_purchase_cents=mandate.max_single_purchase_cents,
        max_open_positions=mandate.max_open_positions,
        max_daily_deals=mandate.max_daily_deals,
        min_expected_margin_bps=mandate.min_expected_margin_bps,
        max_total_loss_cents=mandate.max_total_loss_cents,
        risk_level=mandate.risk_level,
        auto_buy=mandate.auto_buy,
        auto_sell=mandate.auto_sell,
        auto_relist=mandate.auto_relist,
        requires_manual_approval=mandate.requires_manual_approval,
        allowed_categories=mandate.allowed_categories,
        forbidden_categories=mandate.forbidden_categories,
        geo_scope=mandate.geo_scope,
        constraints=mandate.constraints,
        created_at=mandate.created_at,
        activated_at=mandate.activated_at,
        paused_at=mandate.paused_at,
        revoked_at=mandate.revoked_at,
        revocation_reason=mandate.revocation_reason,
        settled_at=mandate.settled_at,
        budget_state=(
            BudgetStateResponse(**budget_state) if budget_state is not None else None
        ),
        positions_summary=positions_summary,
    )


@router.post("/draft", response_model=DraftResponse)
@limiter.limit(lambda: settings.rate_limit_mandate_critical, key_func=user_key)
async def create_draft_endpoint(
    request: Request,
    body: DraftRequest,
    user: CurrentUser = Depends(require_tier(2)),
    db: AsyncSession = Depends(get_db),
) -> DraftResponse:
    try:
        result = await capital_mandate_service.create_capital_mandate_draft(
            db, user_id=user.user_id, input_obj=body
        )
    except capital_mandate_service.CapitalMandateError as exc:
        raise _to_http(exc) from exc
    return DraftResponse(
        draft_id=result.draft_id,
        payload=result.payload,
        payload_summary=result.payload_summary,
        challenge=result.challenge_b64url,
        expires_at_utc=result.expires_at_utc,
    )


@router.post("/submit", response_model=SubmitResponse)
@limiter.limit(lambda: settings.rate_limit_mandate_critical, key_func=user_key)
async def submit_endpoint(
    request: Request,
    body: SubmitRequest,
    user: CurrentUser = Depends(require_tier(2)),
    db: AsyncSession = Depends(get_db),
) -> SubmitResponse:
    try:
        result = await capital_mandate_service.submit_signed_capital_mandate(
            db,
            user_id=user.user_id,
            draft_id=body.draft_id,
            assertion=body.webauthn_assertion,
        )
    except capital_mandate_service.CapitalMandateError as exc:
        raise _to_http(exc) from exc
    return SubmitResponse(
        capital_mandate_id=result.capital_mandate_id,
        status=result.status,
        budget_state=BudgetStateResponse(**result.budget_state),
        expires_at=result.expires_at,
    )


@router.get("/active", response_model=ActiveResponse)
@limiter.limit(lambda: settings.rate_limit_user_read, key_func=user_key)
async def active_endpoint(
    request: Request,
    user: CurrentUser = Depends(require_tier(2)),
    db: AsyncSession = Depends(get_db),
) -> ActiveResponse:
    result = await capital_mandate_service.get_active_capital_mandate(
        db, user_id=user.user_id
    )
    if result.mandate is None:
        return ActiveResponse(
            active=False,
            mandate=None,
            budget_state=None,
            positions_summary=None,
        )
    return ActiveResponse(
        active=result.mandate.status == capital_mandate_service.STATUS_ACTIVE,
        mandate=_mandate_response(
            result.mandate,
            budget_state=result.budget_state,
            positions_summary=result.positions_summary,
        ),
        budget_state=BudgetStateResponse(**result.budget_state),
        positions_summary=result.positions_summary,
    )


@router.get("/{capital_mandate_id}", response_model=CapitalMandateResponse)
@limiter.limit(lambda: settings.rate_limit_user_read, key_func=user_key)
async def detail_endpoint(
    request: Request,
    capital_mandate_id: str,
    user: CurrentUser = Depends(require_tier(2)),
    db: AsyncSession = Depends(get_db),
) -> CapitalMandateResponse:
    try:
        mandate = await capital_mandate_service.get_capital_mandate_for_user(
            db, user_id=user.user_id, capital_mandate_id=capital_mandate_id
        )
    except capital_mandate_service.CapitalMandateError as exc:
        raise _to_http(exc) from exc
    budget = await capital_ledger_service.compute_budget_state(
        db, capital_mandate_id=mandate.id
    )
    return _mandate_response(mandate, budget_state=budget.to_dict())


@router.post("/{capital_mandate_id}/pause", response_model=CapitalMandateResponse)
@limiter.limit(lambda: settings.rate_limit_post_strict, key_func=user_key)
async def pause_endpoint(
    request: Request,
    capital_mandate_id: str,
    user: CurrentUser = Depends(require_tier(2)),
    db: AsyncSession = Depends(get_db),
) -> CapitalMandateResponse:
    try:
        mandate = await capital_mandate_service.pause_capital_mandate(
            db, user_id=user.user_id, capital_mandate_id=capital_mandate_id
        )
    except capital_mandate_service.CapitalMandateError as exc:
        raise _to_http(exc) from exc
    budget = await capital_ledger_service.compute_budget_state(
        db, capital_mandate_id=mandate.id
    )
    return _mandate_response(mandate, budget_state=budget.to_dict())


@router.post("/{capital_mandate_id}/resume", response_model=CapitalMandateResponse)
@limiter.limit(lambda: settings.rate_limit_post_strict, key_func=user_key)
async def resume_endpoint(
    request: Request,
    capital_mandate_id: str,
    user: CurrentUser = Depends(require_tier(2)),
    db: AsyncSession = Depends(get_db),
) -> CapitalMandateResponse:
    try:
        mandate = await capital_mandate_service.resume_capital_mandate(
            db, user_id=user.user_id, capital_mandate_id=capital_mandate_id
        )
    except capital_mandate_service.CapitalMandateError as exc:
        raise _to_http(exc) from exc
    budget = await capital_ledger_service.compute_budget_state(
        db, capital_mandate_id=mandate.id
    )
    return _mandate_response(mandate, budget_state=budget.to_dict())


@router.post("/{capital_mandate_id}/revoke", response_model=CapitalMandateResponse)
@limiter.limit(lambda: settings.rate_limit_post_strict, key_func=user_key)
async def revoke_endpoint(
    request: Request,
    capital_mandate_id: str,
    body: RevokeRequest = RevokeRequest(),
    user: CurrentUser = Depends(require_tier(2)),
    db: AsyncSession = Depends(get_db),
) -> CapitalMandateResponse:
    try:
        mandate = await capital_mandate_service.revoke_capital_mandate(
            db,
            user_id=user.user_id,
            capital_mandate_id=capital_mandate_id,
            reason=body.reason,
        )
    except capital_mandate_service.CapitalMandateError as exc:
        raise _to_http(exc) from exc
    budget = await capital_ledger_service.compute_budget_state(
        db, capital_mandate_id=mandate.id
    )
    return _mandate_response(mandate, budget_state=budget.to_dict())


@router.get("/{capital_mandate_id}/ledger", response_model=LedgerResponse)
@limiter.limit(lambda: settings.rate_limit_user_read, key_func=user_key)
async def ledger_endpoint(
    request: Request,
    capital_mandate_id: str,
    user: CurrentUser = Depends(require_tier(2)),
    db: AsyncSession = Depends(get_db),
) -> LedgerResponse:
    try:
        entries = await capital_mandate_service.list_ledger_entries_for_user(
            db, user_id=user.user_id, capital_mandate_id=capital_mandate_id
        )
    except capital_mandate_service.CapitalMandateError as exc:
        raise _to_http(exc) from exc
    budget = await capital_ledger_service.compute_budget_state(
        db, capital_mandate_id=capital_mandate_id
    )
    return LedgerResponse(
        budget_state=BudgetStateResponse(**budget.to_dict()),
        entries=[
            LedgerEntryResponse(
                id=row.id,
                capital_mandate_id=row.capital_mandate_id,
                user_id=row.user_id,
                agent_id=row.agent_id,
                deal_id=row.deal_id,
                position_id=row.position_id,
                type=row.type,
                amount_cents=row.amount_cents,
                currency=row.currency,
                reason=row.reason,
                metadata=row.entry_metadata,
                idempotency_key=row.idempotency_key,
                created_at=row.created_at,
            )
            for row in entries
        ],
    )


@router.get("/{capital_mandate_id}/positions", response_model=PositionsResponse)
@limiter.limit(lambda: settings.rate_limit_user_read, key_func=user_key)
async def positions_endpoint(
    request: Request,
    capital_mandate_id: str,
    user: CurrentUser = Depends(require_tier(2)),
    db: AsyncSession = Depends(get_db),
) -> PositionsResponse:
    try:
        rows = await capital_mandate_service.list_positions_for_user(
            db, user_id=user.user_id, capital_mandate_id=capital_mandate_id
        )
    except capital_mandate_service.CapitalMandateError as exc:
        raise _to_http(exc) from exc
    return PositionsResponse(
        positions=[
            PositionResponse(
                id=row.id,
                capital_mandate_id=row.capital_mandate_id,
                user_id=row.user_id,
                agent_id=row.agent_id,
                source_buy_deal_id=row.source_buy_deal_id,
                resale_sell_deal_id=row.resale_sell_deal_id,
                item_snapshot=row.item_snapshot,
                status=row.status,
                purchase_price_cents=row.purchase_price_cents,
                expected_resale_price_cents=row.expected_resale_price_cents,
                expected_profit_cents=row.expected_profit_cents,
                expected_margin_bps=row.expected_margin_bps,
                realized_sale_price_cents=row.realized_sale_price_cents,
                realized_profit_cents=row.realized_profit_cents,
                created_at=row.created_at,
                updated_at=row.updated_at,
                closed_at=row.closed_at,
            )
            for row in rows
        ]
    )
