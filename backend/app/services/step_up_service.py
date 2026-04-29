"""Step-up service — pending agent actions awaiting user signature (brief task 2.5).

The flow:

  1. Agent calls a tool that triggers a step-up rule (e.g. `accept_offer`
     above €100). `MandateVerifier._check_step_up` raises `StepUpRequired`.
  2. `tool_layer.ToolHandler._queue_step_up` catches it and calls
     `create_pending_request_sync(...)` here, persisting a `StepUpRequest`
     row with status='pending', a 32-byte challenge, and the
     JCS-canonicalized payload the user will sign.
  3. The mobile app polls `GET /api/step-up/pending` and surfaces the
     request to the user.
  4. The user signs via `POST /api/step-up/{id}/sign` → `sign(...)` here
     verifies the WebAuthn assertion and marks `status='approved'`,
     storing the signature.
  5. Agent re-attempts the tool call on its next tick with the captured
     signature attached → MandateVerifier sees `step_up_signature` in
     params and bypasses the step-up gate.

Public surface:

  Sync (used by the §5 sync scaffold `tool_layer.py`):
    - create_pending_request_sync(db, agent_id, mandate_id, user_id,
                                  action, action_params, reason) → step_up_id

  Async (used by API endpoints):
    - StepUpError + subclasses
    - WebAuthnAssertionPayload (re-uses mandate_service shape)
    - PendingStepUp, SignedStepUp, RejectedStepUp (return models)
    - get_pending_for_user(db, user_id) → list[PendingStepUp]
    - get_for_signing(db, user_id, step_up_id) → SigningPayload
    - sign(db, user_id, step_up_id, assertion) → SignedStepUp
    - reject(db, user_id, step_up_id) → RejectedStepUp
    - mark_expired(db) → int (cleanup; intended for cron in 7.x)

V0 simplifying assumptions (DESIGN_QUESTIONS DQ-22):
  - One pending step-up per (agent, action) at a time. A second request
    for the same combo will succeed (no DB constraint), but the agent's
    re-try logic should pick the latest. Documented; revisit if abuse.
  - `sign` does not run the original tool action — that is the agent's
    job on its next tick. We just record the approval + signature.
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session
from webauthn import verify_authentication_response

from app.core import canonicalization
from app.core.config import settings
from app.core.logging import log
from app.models.schema import StepUpRequest, User
from app.services.auth_service import _b64url, _b64url_decode
from app.services.mandate_service import WebAuthnAssertionPayload


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class StepUpError(Exception):
    code: str = "step_up_error"
    http_status: int = 400


class StepUpNotFound(StepUpError):
    code = "step_up_not_found"
    http_status = 404


class StepUpAlreadyResolved(StepUpError):
    code = "step_up_already_resolved"
    http_status = 409


class StepUpExpired(StepUpError):
    code = "step_up_expired"
    http_status = 410


class StepUpVerificationFailed(StepUpError):
    code = "step_up_verification_failed"
    http_status = 422


# ---------------------------------------------------------------------------
# Output models
# ---------------------------------------------------------------------------


@dataclass
class PendingStepUp:
    step_up_id: str
    agent_id: str
    action: str
    reason: str
    expires_at: datetime
    created_at: datetime


@dataclass
class SigningPayload:
    step_up_id: str
    payload: dict[str, Any]
    challenge_b64url: str
    expires_at: datetime


@dataclass
class SignedStepUp:
    step_up_id: str
    status: str  # "approved"
    resolved_at: datetime


@dataclass
class RejectedStepUp:
    step_up_id: str
    status: str  # "rejected"
    resolved_at: datetime


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


# Step-up requests live for 10 minutes; after that, the agent will see
# them as `expired` on its next tick and decide whether to re-attempt.
STEP_UP_TTL_SECONDS = 600


def _utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _iso_z(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _build_canonical_payload(
    *,
    step_up_id: str,
    user_id: str,
    nullifier_hash: str,
    agent_id: str,
    mandate_id: str,
    action: str,
    action_params: dict[str, Any],
    issued_at: datetime,
    challenge_hex: str,
) -> tuple[dict[str, Any], bytes]:
    """Return (payload_dict, canonical_bytes) for a step-up confirmation.

    Shared between the sync and async creation paths so both produce the
    exact same signed bytes.
    """
    payload = {
        "version": "1.0",
        "action": "step_up_approval",
        "step_up_id": step_up_id,
        "principal": {
            "user_id": user_id,
            "nullifier_hash": nullifier_hash,
        },
        "agent_id": agent_id,
        "mandate_id": mandate_id,
        "approved_action": {
            "action_name": action,
            "params": action_params,
        },
        "issued_at": _iso_z(issued_at),
        "challenge": challenge_hex,
    }
    return payload, canonicalization.canonicalize(payload)


# ---------------------------------------------------------------------------
# Sync entry — used by tool_layer.py (legacy scaffold)
# ---------------------------------------------------------------------------


def create_pending_request_sync(
    db: Session,
    *,
    agent_id: str,
    mandate_id: str,
    user_id: str,
    nullifier_hash: str,
    action: str,
    action_params: dict[str, Any],
    reason: str,
) -> str:
    """Persist a pending step-up request. Returns its id.

    Sync because the only V0 caller is `tool_layer.ToolHandler` which
    runs on a `sqlalchemy.orm.Session`. When the agent runtime moves
    to async (FASE 6+), swap to the async sibling above.
    """
    import uuid

    step_up_id = str(uuid.uuid4())
    challenge = secrets.token_bytes(32)
    issued_at = _utcnow_naive()
    payload, canonical = _build_canonical_payload(
        step_up_id=step_up_id,
        user_id=user_id,
        nullifier_hash=nullifier_hash,
        agent_id=agent_id,
        mandate_id=mandate_id,
        action=action,
        action_params=action_params,
        issued_at=issued_at,
        challenge_hex=challenge.hex(),
    )
    request = StepUpRequest(
        id=step_up_id,
        agent_id=agent_id,
        mandate_id=mandate_id,
        user_id=user_id,
        action=action,
        action_params=action_params,
        reason=reason,
        challenge=challenge,
        canonical_payload=canonical,
        status="pending",
        expires_at=issued_at + timedelta(seconds=STEP_UP_TTL_SECONDS),
        resolved_at=None,
        signature=None,
        created_at=issued_at,
    )
    db.add(request)
    db.commit()
    return step_up_id


async def create_pending_request_async(
    db: AsyncSession,
    *,
    agent_id: str,
    mandate_id: str,
    user_id: str,
    nullifier_hash: str,
    action: str,
    action_params: dict[str, Any],
    reason: str,
) -> str:
    """Async sibling of `create_pending_request_sync` (brief task 6.3.a).

    Used by the modernized `AsyncToolHandler` in `tool_layer.py`. Same
    behavior — random challenge bytes + canonical payload + persisted
    `StepUpRequest` row — but on an `AsyncSession`. Also fires the
    `STEP_UP_REQUIRED` notification post-commit (closes the V0 gap noted
    in 6.1: the sync path was dead-code, the async path is the one that
    actually runs in 6.3+).
    """
    import uuid

    from app.services import notification_service

    step_up_id = str(uuid.uuid4())
    challenge = secrets.token_bytes(32)
    issued_at = _utcnow_naive()
    payload, canonical = _build_canonical_payload(
        step_up_id=step_up_id,
        user_id=user_id,
        nullifier_hash=nullifier_hash,
        agent_id=agent_id,
        mandate_id=mandate_id,
        action=action,
        action_params=action_params,
        issued_at=issued_at,
        challenge_hex=challenge.hex(),
    )
    request = StepUpRequest(
        id=step_up_id,
        agent_id=agent_id,
        mandate_id=mandate_id,
        user_id=user_id,
        action=action,
        action_params=action_params,
        reason=reason,
        challenge=challenge,
        canonical_payload=canonical,
        status="pending",
        expires_at=issued_at + timedelta(seconds=STEP_UP_TTL_SECONDS),
        resolved_at=None,
        signature=None,
        created_at=issued_at,
    )
    db.add(request)
    await db.commit()

    # Fire-and-forget UX notification — closes the 6.1 wire-on-modernization
    # note in IDEAS_BACKLOG.
    await notification_service.create_notification(
        db,
        user_id=user_id,
        notification_type=notification_service.NotificationType.STEP_UP_REQUIRED,
        title="Conferma richiesta",
        body=f"Il tuo agente ha bisogno di firmare: {action}",
        payload={
            "step_up_id": step_up_id,
            "action": action,
            "reason": reason,
        },
    )

    return step_up_id


# ---------------------------------------------------------------------------
# Async API
# ---------------------------------------------------------------------------


async def get_pending_for_user(
    db: AsyncSession, *, user_id: str
) -> list[PendingStepUp]:
    """Return non-expired pending step-up requests for the user."""
    now = _utcnow_naive()
    result = await db.scalars(
        select(StepUpRequest)
        .where(StepUpRequest.user_id == user_id)
        .where(StepUpRequest.status == "pending")
        .where(StepUpRequest.expires_at > now)
        .order_by(StepUpRequest.created_at.asc())
    )
    return [
        PendingStepUp(
            step_up_id=r.id,
            agent_id=r.agent_id,
            action=r.action,
            reason=r.reason,
            expires_at=r.expires_at,
            created_at=r.created_at,
        )
        for r in result
    ]


async def get_for_signing(
    db: AsyncSession, *, user_id: str, step_up_id: str
) -> SigningPayload:
    """Return the canonical payload + challenge for the user to sign."""
    request = await _load_pending(db, user_id=user_id, step_up_id=step_up_id)
    import json

    payload_dict = json.loads(bytes(request.canonical_payload).decode("utf-8"))
    return SigningPayload(
        step_up_id=request.id,
        payload=payload_dict,
        challenge_b64url=_b64url(bytes(request.challenge)),
        expires_at=request.expires_at,
    )


async def sign(
    db: AsyncSession,
    *,
    user_id: str,
    step_up_id: str,
    assertion: WebAuthnAssertionPayload,
) -> SignedStepUp:
    """Verify the user's WebAuthn signature; mark request as approved."""
    request = await _load_pending(
        db, user_id=user_id, step_up_id=step_up_id, lock=True
    )

    user = await db.scalar(select(User).where(User.id == user_id))
    if user is None:
        raise StepUpNotFound(user_id)

    try:
        verified = verify_authentication_response(
            credential=assertion.model_dump(by_alias=True),
            expected_challenge=bytes(request.challenge),
            expected_rp_id=settings.webauthn_rp_id,
            expected_origin=settings.webauthn_origin,
            credential_public_key=_b64url_decode(user.passkey_pubkey),
            credential_current_sign_count=user.passkey_sign_count or 0,
            require_user_verification=False,
        )
    except Exception as exc:
        log.info(
            "step_up.webauthn_failed",
            user_id=user_id,
            step_up_id=step_up_id,
            error=type(exc).__name__,
        )
        raise StepUpVerificationFailed(str(exc)) from exc

    user.passkey_sign_count = verified.new_sign_count
    user.last_active_at = _utcnow_naive()

    request.status = "approved"
    request.resolved_at = _utcnow_naive()
    request.signature = {
        "algorithm": "webauthn",
        "credential_id": assertion.id,
        "raw_id": assertion.raw_id,
        "response": dict(assertion.response),
    }

    try:
        await db.commit()
    except Exception:
        await db.rollback()
        raise

    # 6.1 — fire-and-forget UX notification (post-commit, never raises).
    from app.services import notification_service

    await notification_service.create_notification(
        db,
        user_id=user_id,
        notification_type=notification_service.NotificationType.STEP_UP_APPROVED,
        title="Firma confermata",
        body=f"Hai approvato l'azione: {request.action}",
        payload={
            "step_up_id": request.id,
            "action": request.action,
        },
    )

    return SignedStepUp(
        step_up_id=request.id,
        status="approved",
        resolved_at=request.resolved_at,
    )


async def reject(
    db: AsyncSession, *, user_id: str, step_up_id: str
) -> RejectedStepUp:
    """User explicitly rejected this step-up. Action will be cancelled."""
    request = await _load_pending(
        db, user_id=user_id, step_up_id=step_up_id, lock=True
    )
    request.status = "rejected"
    request.resolved_at = _utcnow_naive()
    try:
        await db.commit()
    except Exception:
        await db.rollback()
        raise

    # 6.1 — fire-and-forget UX notification.
    from app.services import notification_service

    await notification_service.create_notification(
        db,
        user_id=user_id,
        notification_type=notification_service.NotificationType.STEP_UP_REJECTED,
        title="Firma rifiutata",
        body=f"Hai rifiutato l'azione: {request.action}",
        payload={
            "step_up_id": request.id,
            "action": request.action,
        },
    )

    return RejectedStepUp(
        step_up_id=request.id,
        status="rejected",
        resolved_at=request.resolved_at,
    )


async def mark_expired(db: AsyncSession) -> int:
    """Sweep expired pending requests → status='expired'.

    Intended for a periodic cleanup job (cron in 7.x). Safe to call
    multiple times; no-op for already-resolved rows.
    """
    now = _utcnow_naive()
    result = await db.scalars(
        select(StepUpRequest)
        .where(StepUpRequest.status == "pending")
        .where(StepUpRequest.expires_at <= now)
    )
    expired_count = 0
    for request in result:
        request.status = "expired"
        request.resolved_at = now
        expired_count += 1
    if expired_count:
        await db.commit()
    return expired_count


# ---------------------------------------------------------------------------
# Internal
# ---------------------------------------------------------------------------


async def _load_pending(
    db: AsyncSession,
    *,
    user_id: str,
    step_up_id: str,
    lock: bool = False,
) -> StepUpRequest:
    stmt = (
        select(StepUpRequest)
        .where(StepUpRequest.id == step_up_id)
        .where(StepUpRequest.user_id == user_id)
    )
    if lock:
        stmt = stmt.with_for_update()
    request = await db.scalar(stmt)
    if request is None:
        raise StepUpNotFound(step_up_id)
    if request.status != "pending":
        # Already approved / rejected / expired
        raise StepUpAlreadyResolved(
            f"step-up {step_up_id} is in status={request.status!r}"
        )
    if request.expires_at <= _utcnow_naive():
        request.status = "expired"
        request.resolved_at = _utcnow_naive()
        try:
            await db.commit()
        except Exception:
            await db.rollback()
        raise StepUpExpired(step_up_id)
    return request
