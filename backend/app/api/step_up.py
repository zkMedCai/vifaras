"""Step-up API — pending agent actions awaiting user confirmation (brief task 2.5)."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.security import CurrentUser, require_tier
from app.services import mandate_service, step_up_service

router = APIRouter(prefix="/api/step-up", tags=["step-up"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class PendingItem(BaseModel):
    step_up_id: str
    agent_id: str
    action: str
    reason: str
    expires_at: datetime
    created_at: datetime


class PendingResponse(BaseModel):
    pending: list[PendingItem]


class DraftResponse(BaseModel):
    step_up_id: str
    payload: dict[str, Any]
    challenge: str
    expires_at: datetime


class SignRequest(BaseModel):
    webauthn_assertion: mandate_service.WebAuthnAssertionPayload


class SignResponse(BaseModel):
    step_up_id: str
    status: str
    resolved_at: datetime
    approved: bool = True
    action_resumed: bool = False  # V0 sync resume happens client-side


class RejectResponse(BaseModel):
    step_up_id: str
    status: str
    resolved_at: datetime


# ---------------------------------------------------------------------------
# Error mapping
# ---------------------------------------------------------------------------


def _to_http(exc: step_up_service.StepUpError) -> HTTPException:
    return HTTPException(
        status_code=exc.http_status,
        detail={"code": exc.code, "message": str(exc)},
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/pending", response_model=PendingResponse)
async def list_pending(
    user: CurrentUser = Depends(require_tier(2)),
    db: AsyncSession = Depends(get_db),
) -> PendingResponse:
    """Return all pending step-up requests for the authenticated user."""
    items = await step_up_service.get_pending_for_user(db, user_id=user.user_id)
    return PendingResponse(
        pending=[PendingItem(**item.__dict__) for item in items]
    )


@router.get("/{step_up_id}/draft", response_model=DraftResponse)
async def get_draft(
    step_up_id: str,
    user: CurrentUser = Depends(require_tier(2)),
    db: AsyncSession = Depends(get_db),
) -> DraftResponse:
    """Return the canonical payload + challenge for the user to sign."""
    try:
        result = await step_up_service.get_for_signing(
            db, user_id=user.user_id, step_up_id=step_up_id
        )
    except step_up_service.StepUpError as exc:
        raise _to_http(exc) from exc
    return DraftResponse(
        step_up_id=result.step_up_id,
        payload=result.payload,
        challenge=result.challenge_b64url,
        expires_at=result.expires_at,
    )


@router.post("/{step_up_id}/sign", response_model=SignResponse)
async def sign(
    step_up_id: str,
    body: SignRequest,
    user: CurrentUser = Depends(require_tier(2)),
    db: AsyncSession = Depends(get_db),
) -> SignResponse:
    """Verify the user's WebAuthn signature; mark step-up as approved."""
    try:
        result = await step_up_service.sign(
            db,
            user_id=user.user_id,
            step_up_id=step_up_id,
            assertion=body.webauthn_assertion,
        )
    except step_up_service.StepUpError as exc:
        raise _to_http(exc) from exc
    return SignResponse(
        step_up_id=result.step_up_id,
        status=result.status,
        resolved_at=result.resolved_at,
    )


@router.post("/{step_up_id}/reject", response_model=RejectResponse)
async def reject(
    step_up_id: str,
    user: CurrentUser = Depends(require_tier(2)),
    db: AsyncSession = Depends(get_db),
) -> RejectResponse:
    """User explicitly rejects the action — agent will see it cancelled."""
    try:
        result = await step_up_service.reject(
            db, user_id=user.user_id, step_up_id=step_up_id
        )
    except step_up_service.StepUpError as exc:
        raise _to_http(exc) from exc
    return RejectResponse(
        step_up_id=result.step_up_id,
        status=result.status,
        resolved_at=result.resolved_at,
    )
