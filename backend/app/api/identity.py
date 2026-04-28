"""Identity API routes — tier 1 upgrade via Self Protocol (brief task 2.3)."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.security import CurrentUser, require_tier
from app.services import identity_service
from app.services.kms_service import KMSError

router = APIRouter(prefix="/api/identity", tags=["identity"])


class VerifySelfRequest(BaseModel):
    """Input from the mobile app — verbatim from Self mobile SDK."""

    proof: str = Field(min_length=1)
    public_signals: list[Any] = Field(default_factory=list, alias="publicSignals")

    model_config = {"populate_by_name": True}


class NextStep(BaseModel):
    action: str
    endpoint: str | None = None
    hint: str | None = None


class VerifySelfResponse(BaseModel):
    tier: int
    user_id: str
    agent_id: str | None
    agent_pubkey: str | None
    nullifier_hash: str
    attributes_proven: dict[str, Any]
    already_upgraded: bool
    next_step: NextStep


def _to_http(exc: identity_service.IdentityError) -> HTTPException:
    return HTTPException(
        status_code=exc.http_status,
        detail={"code": exc.code, "message": str(exc)},
    )


def _next_step_for_tier(tier: int) -> NextStep:
    """After tier 1, the user is pointed at mandate signing (tier 2)."""
    if tier >= 2:
        return NextStep(action="ready", hint="agent active and mandated")
    return NextStep(
        action="configure_mandate",
        endpoint="/api/mandates/draft",
        hint=(
            "Autorizza il tuo agente con Face ID per finalizzare il primo "
            "deal. Decidi tu i limiti."
        ),
    )


@router.post("/verify-self", response_model=VerifySelfResponse)
async def verify_self(
    body: VerifySelfRequest,
    user: CurrentUser = Depends(require_tier(0)),
    db: AsyncSession = Depends(get_db),
) -> VerifySelfResponse:
    """Submit a Self ZK proof, upgrade the caller from tier 0 to tier 1.

    Idempotent: a second call by an already-tier-1 user returns 200 with
    `already_upgraded=true` rather than 409 — the mobile app can safely
    re-issue the request after a network glitch.
    """
    proof_payload = identity_service.SelfProofPayload(
        proof=body.proof,
        public_signals=body.public_signals,
    )
    try:
        result = await identity_service.upgrade_user_to_tier_1(
            db,
            user_id=user.user_id,
            proof=proof_payload,
        )
    except KMSError as exc:
        raise HTTPException(
            status_code=500,
            detail={"code": "kms_error", "message": str(exc)},
        ) from exc
    except identity_service.NullifierCollision as exc:
        raise HTTPException(
            status_code=exc.http_status,
            detail={
                "code": exc.code,
                "message": "Questo documento è già associato a un altro account.",
                "next_step": {
                    "action": "login_with_existing_account",
                    "hint": (
                        "Se hai già un account verificato, accedi con la "
                        "passkey originale."
                    ),
                },
            },
        ) from exc
    except identity_service.IdentityError as exc:
        raise _to_http(exc) from exc

    return VerifySelfResponse(
        tier=result.tier,
        user_id=result.user_id,
        agent_id=result.agent_id,
        agent_pubkey=result.agent_pubkey,
        nullifier_hash=result.nullifier_hash,
        attributes_proven=result.attributes_proven,
        already_upgraded=result.already_upgraded,
        next_step=_next_step_for_tier(result.tier),
    )
