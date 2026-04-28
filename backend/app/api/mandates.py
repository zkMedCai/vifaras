"""Mandates API — draft + submit (brief task 2.4)."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.security import CurrentUser, require_tier
from app.services import mandate_revocation_service, mandate_service

router = APIRouter(prefix="/api/mandates", tags=["mandates"])


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class DraftRequest(BaseModel):
    agent_id: str
    limits: mandate_service.DraftLimitsInput = Field(
        default_factory=mandate_service.DraftLimitsInput
    )
    constraints: mandate_service.DraftConstraintsInput = Field(
        default_factory=mandate_service.DraftConstraintsInput
    )
    expires_in_days: int | None = Field(default=None, ge=1, le=90)


class DraftResponse(BaseModel):
    draft_id: str
    payload: dict[str, Any]
    payload_summary: dict[str, Any]
    challenge: str
    expires_at_utc: datetime


class SubmitRequest(BaseModel):
    draft_id: str
    webauthn_assertion: mandate_service.WebAuthnAssertionPayload


class SubmitResponse(BaseModel):
    mandate_id: str
    agent_id: str
    agent_status: str
    expires_at: datetime
    new_access_token: str
    token_type: str = "bearer"
    next_step: dict[str, Any]


# ---------------------------------------------------------------------------
# Error mapping
# ---------------------------------------------------------------------------


def _to_http(exc: mandate_service.MandateError) -> HTTPException:
    return HTTPException(
        status_code=exc.http_status,
        detail={"code": exc.code, "message": str(exc)},
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/draft", response_model=DraftResponse)
async def draft_mandate(
    body: DraftRequest,
    user: CurrentUser = Depends(require_tier(1)),
    db: AsyncSession = Depends(get_db),
) -> DraftResponse:
    """Create a pending mandate draft for the caller's agent."""
    try:
        result = await mandate_service.create_draft(
            db,
            user_id=user.user_id,
            agent_id=body.agent_id,
            user_limits=body.limits,
            user_constraints=body.constraints,
            expires_in_days=body.expires_in_days,
        )
    except mandate_service.MandateError as exc:
        raise _to_http(exc) from exc
    return DraftResponse(
        draft_id=result.draft_id,
        payload=result.payload,
        payload_summary=result.payload_summary,
        challenge=result.challenge_b64url,
        expires_at_utc=result.expires_at_utc,
    )


@router.post("/submit", response_model=SubmitResponse)
async def submit_mandate(
    body: SubmitRequest,
    user: CurrentUser = Depends(require_tier(1)),
    db: AsyncSession = Depends(get_db),
) -> SubmitResponse:
    """Verify the user's WebAuthn signature, persist mandate, upgrade tier."""
    try:
        result = await mandate_service.submit_signed_mandate(
            db,
            user_id=user.user_id,
            draft_id=body.draft_id,
            assertion=body.webauthn_assertion,
        )
    except mandate_service.MandateError as exc:
        raise _to_http(exc) from exc
    return SubmitResponse(
        mandate_id=result.mandate_id,
        agent_id=result.agent_id,
        agent_status=result.agent_status,
        expires_at=result.expires_at,
        new_access_token=result.new_access_token,
        next_step=result.next_step,
    )


# ---------------------------------------------------------------------------
# Revocation (brief task 2.5)
# ---------------------------------------------------------------------------


class RevokeDraftRequest(BaseModel):
    reason: str  # one of REVOCATION_REASONS_V0


class RevokeDraftResponse(BaseModel):
    revocation_draft_id: str | None
    payload: dict[str, Any]
    challenge: str | None
    expires_at_utc: datetime | None
    already_revoked: bool


class RevokeSubmitRequest(BaseModel):
    revocation_draft_id: str
    webauthn_assertion: mandate_service.WebAuthnAssertionPayload


class CancellationCountsModel(BaseModel):
    negotiations_cancelled: int
    deals_cancelled: int
    intents_paused: int
    pending_drafts_invalidated: int
    pending_step_ups_invalidated: int


class RevokeSubmitResponse(BaseModel):
    revoked: bool
    already_revoked: bool
    mandate_id: str
    agent_id: str
    agent_status: str
    revoked_at: datetime | None
    cancellations: CancellationCountsModel


def _to_http_rev(
    exc: mandate_revocation_service.RevocationError,
) -> HTTPException:
    return HTTPException(
        status_code=exc.http_status,
        detail={"code": exc.code, "message": str(exc)},
    )


@router.post("/{mandate_id}/revoke/draft", response_model=RevokeDraftResponse)
async def revoke_draft(
    mandate_id: str,
    body: RevokeDraftRequest,
    user: CurrentUser = Depends(require_tier(2)),
    db: AsyncSession = Depends(get_db),
) -> RevokeDraftResponse:
    """Stage a revocation: returns canonical payload + WebAuthn challenge."""
    try:
        result = await mandate_revocation_service.create_revocation_draft(
            db,
            user_id=user.user_id,
            mandate_id=mandate_id,
            reason=body.reason,
        )
    except mandate_revocation_service.RevocationError as exc:
        raise _to_http_rev(exc) from exc
    return RevokeDraftResponse(
        revocation_draft_id=result.revocation_draft_id,
        payload=result.payload,
        challenge=result.challenge_b64url,
        expires_at_utc=result.expires_at_utc,
        already_revoked=result.already_revoked,
    )


@router.post(
    "/{mandate_id}/revoke/submit", response_model=RevokeSubmitResponse
)
async def revoke_submit(
    mandate_id: str,
    body: RevokeSubmitRequest,
    user: CurrentUser = Depends(require_tier(2)),
    db: AsyncSession = Depends(get_db),
) -> RevokeSubmitResponse:
    """Verify the revocation signature; cascade-cancel agent state."""
    try:
        result = await mandate_revocation_service.submit_revocation(
            db,
            user_id=user.user_id,
            mandate_id=mandate_id,
            draft_id=body.revocation_draft_id,
            assertion=body.webauthn_assertion,
        )
    except mandate_revocation_service.RevocationError as exc:
        raise _to_http_rev(exc) from exc
    return RevokeSubmitResponse(
        revoked=result.revoked,
        already_revoked=result.already_revoked,
        mandate_id=result.mandate_id,
        agent_id=result.agent_id,
        agent_status=result.agent_status,
        revoked_at=result.revoked_at,
        cancellations=CancellationCountsModel(**result.cancellations.__dict__),
    )
