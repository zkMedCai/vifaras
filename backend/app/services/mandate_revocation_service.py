"""Mandate revocation — Big Red Button (brief task 2.5).

Same WebAuthn-signed two-stage flow as mandate creation, but the signed
payload says `action: revoke_mandate` and the submit handler:

  1. Marks `mandate.revoked_at = now()` and stores the reason
  2. Sets `agent.status = 'revoked'` (irreversible in V0)
  3. Cancels active negotiations for that agent
     (status='active' → 'cancelled_due_to_revocation')
  4. Cancels pending deals for that user/agent
     (status='pending_signatures' (post-5.3) | 'pending_buyer' | 'pending_seller'
      → 'cancelled_revoked')
  5. Pauses active intents (status='active' → 'paused')
  6. Invalidates pending mandate_drafts (consumed=True)
  7. Invalidates pending step_up_requests (status='expired')
  8. Audit log

Already-revoked is idempotent: returns success silently with
`already_revoked=True` and no side effects re-applied.

Public surface:
  - errors: RevocationError + subclasses
  - RevocationDraftCreated, RevocationResult
  - allowed reasons: REVOCATION_REASONS_V0
  - create_revocation_draft(db, user_id, mandate_id, reason)
  - submit_revocation(db, user_id, draft_id, assertion)
"""
from __future__ import annotations

import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Final

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from webauthn import verify_authentication_response

from app.core import canonicalization
from app.core.config import settings
from app.core.logging import log
from app.models.schema import (
    Agent,
    Deal,
    Intent,
    Mandate,
    MandateDraft,
    MandateRevocationDraft,
    Negotiation,
    StepUpRequest,
    User,
)
from app.services.auth_service import _b64url, _b64url_decode
from app.services.mandate_service import WebAuthnAssertionPayload


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class RevocationError(Exception):
    code: str = "revocation_error"
    http_status: int = 400


class MandateNotFound(RevocationError):
    code = "mandate_not_found"
    http_status = 404


class MandateNotOwned(RevocationError):
    code = "mandate_not_owned"
    http_status = 404


class InvalidRevocationReason(RevocationError):
    code = "invalid_revocation_reason"
    http_status = 422


class RevocationDraftNotFound(RevocationError):
    code = "revocation_draft_not_found"
    http_status = 404


class RevocationDraftExpired(RevocationError):
    code = "revocation_draft_expired"
    http_status = 410


class RevocationDraftAlreadyConsumed(RevocationError):
    code = "revocation_draft_already_consumed"
    http_status = 409


class RevocationVerificationFailed(RevocationError):
    code = "revocation_verification_failed"
    http_status = 422


# ---------------------------------------------------------------------------
# Allowed values
# ---------------------------------------------------------------------------


REVOCATION_REASONS_V0: Final[tuple[str, ...]] = (
    "user_requested",
    "suspicious_activity",
    "lost_device",
)

REVOCATION_DRAFT_TTL_SECONDS = 300


# ---------------------------------------------------------------------------
# Output models
# ---------------------------------------------------------------------------


@dataclass
class RevocationDraftCreated:
    revocation_draft_id: str | None
    payload: dict[str, Any]
    challenge_b64url: str | None
    expires_at_utc: datetime | None
    already_revoked: bool


@dataclass
class CancellationCounts:
    negotiations_cancelled: int
    deals_cancelled: int
    intents_paused: int
    pending_drafts_invalidated: int
    pending_step_ups_invalidated: int


@dataclass
class RevocationResult:
    revoked: bool
    already_revoked: bool
    mandate_id: str
    agent_id: str
    agent_status: str
    revoked_at: datetime | None
    cancellations: CancellationCounts


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _iso_z(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _build_revocation_payload(
    *,
    mandate: Mandate,
    user: User,
    reason: str,
    issued_at: datetime,
    challenge_hex: str,
) -> dict[str, Any]:
    return {
        "version": "1.0",
        "action": "revoke_mandate",
        "mandate_id": mandate.id,
        "principal": {
            "user_id": user.id,
            "nullifier_hash": user.nullifier_hash,
        },
        "agent_id": mandate.agent_id,
        "reason": reason,
        "issued_at": _iso_z(issued_at),
        "challenge": challenge_hex,
    }


async def _load_active_mandate_owned_by(
    db: AsyncSession, *, user_id: str, mandate_id: str
) -> Mandate:
    mandate = await db.scalar(
        select(Mandate).where(Mandate.id == mandate_id)
    )
    if mandate is None:
        raise MandateNotFound(mandate_id)
    if mandate.user_id != user_id:
        raise MandateNotOwned(mandate_id)
    return mandate


# ---------------------------------------------------------------------------
# /revoke/draft
# ---------------------------------------------------------------------------


async def create_revocation_draft(
    db: AsyncSession,
    *,
    user_id: str,
    mandate_id: str,
    reason: str,
) -> RevocationDraftCreated:
    """Create a pending revocation draft for `mandate_id`.

    Idempotent: if the mandate is already revoked, returns
    `already_revoked=True` with empty draft fields — no row created.
    """
    if reason not in REVOCATION_REASONS_V0:
        raise InvalidRevocationReason(
            f"reason must be one of {list(REVOCATION_REASONS_V0)!r}"
        )

    mandate = await _load_active_mandate_owned_by(
        db, user_id=user_id, mandate_id=mandate_id
    )
    if mandate.revoked_at is not None:
        return RevocationDraftCreated(
            revocation_draft_id=None,
            payload={},
            challenge_b64url=None,
            expires_at_utc=None,
            already_revoked=True,
        )

    user = await db.scalar(select(User).where(User.id == user_id))
    if user is None:
        # Should not happen — mandate ownership already checked
        raise MandateNotOwned(mandate_id)

    challenge = secrets.token_bytes(32)
    issued_at = _utcnow()
    payload = _build_revocation_payload(
        mandate=mandate,
        user=user,
        reason=reason,
        issued_at=issued_at,
        challenge_hex=challenge.hex(),
    )
    canonical = canonicalization.canonicalize(payload)

    draft_id = str(uuid.uuid4())
    expires_at = issued_at + timedelta(seconds=REVOCATION_DRAFT_TTL_SECONDS)
    draft = MandateRevocationDraft(
        id=draft_id,
        user_id=user_id,
        mandate_id=mandate_id,
        canonical_payload=canonical,
        challenge=challenge,
        expires_at=expires_at.replace(tzinfo=None),
        consumed=False,
        created_at=_utcnow_naive(),
    )
    db.add(draft)
    await db.commit()

    return RevocationDraftCreated(
        revocation_draft_id=draft_id,
        payload=payload,
        challenge_b64url=_b64url(challenge),
        expires_at_utc=expires_at,
        already_revoked=False,
    )


# ---------------------------------------------------------------------------
# /revoke/submit
# ---------------------------------------------------------------------------


async def submit_revocation(
    db: AsyncSession,
    *,
    user_id: str,
    mandate_id: str,
    draft_id: str,
    assertion: WebAuthnAssertionPayload,
) -> RevocationResult:
    """Verify the user's signature, perform full revocation cascade.

    Idempotent: if the mandate is already revoked, returns
    `already_revoked=True` with the recorded `revoked_at` and zero
    cancellation counts.
    """
    draft = await db.scalar(
        select(MandateRevocationDraft)
        .where(MandateRevocationDraft.id == draft_id)
        .where(MandateRevocationDraft.user_id == user_id)
        .with_for_update()
    )
    if draft is None:
        raise RevocationDraftNotFound(draft_id)
    if draft.consumed:
        raise RevocationDraftAlreadyConsumed(draft_id)
    if draft.expires_at < _utcnow_naive():
        raise RevocationDraftExpired(draft_id)
    if draft.mandate_id != mandate_id:
        # URL says one mandate, draft was for another. Refuse.
        raise RevocationDraftNotFound(draft_id)

    mandate = await _load_active_mandate_owned_by(
        db, user_id=user_id, mandate_id=mandate_id
    )

    # Idempotency guard: someone (this user, in another window) already
    # revoked. Mark the draft consumed (no replay) and return success.
    if mandate.revoked_at is not None:
        draft.consumed = True
        await db.commit()
        return RevocationResult(
            revoked=True,
            already_revoked=True,
            mandate_id=mandate.id,
            agent_id=mandate.agent_id,
            agent_status="revoked",
            revoked_at=mandate.revoked_at,
            cancellations=CancellationCounts(0, 0, 0, 0, 0),
        )

    user = await db.scalar(select(User).where(User.id == user_id))
    if user is None:
        raise MandateNotOwned(mandate_id)

    # Verify the WebAuthn signature against the draft's challenge.
    try:
        verified = verify_authentication_response(
            credential=assertion.model_dump(by_alias=True),
            expected_challenge=bytes(draft.challenge),
            expected_rp_id=settings.webauthn_rp_id,
            expected_origin=settings.webauthn_origin,
            credential_public_key=_b64url_decode(user.passkey_pubkey),
            credential_current_sign_count=user.passkey_sign_count or 0,
            require_user_verification=False,
        )
    except Exception as exc:
        log.info(
            "revocation.webauthn_failed",
            user_id=user_id,
            mandate_id=mandate_id,
            error=type(exc).__name__,
        )
        raise RevocationVerificationFailed(str(exc)) from exc

    user.passkey_sign_count = verified.new_sign_count
    user.last_active_at = _utcnow_naive()

    # Decode the canonical payload to extract the reason as it was signed
    # (defense vs. mismatch between draft DB and submit request body).
    import json

    payload = json.loads(bytes(draft.canonical_payload).decode("utf-8"))
    reason = payload["reason"]

    # ---- Apply the revocation cascade ----
    now_naive = _utcnow_naive()

    mandate.revoked_at = now_naive
    mandate.revocation_reason = reason

    agent = await db.scalar(select(Agent).where(Agent.id == mandate.agent_id))
    if agent is not None:
        agent.status = "revoked"

    counts = await _cascade_cancellations(
        db,
        agent_id=mandate.agent_id,
        user_id=user_id,
        mandate_id=mandate_id,
        now=now_naive,
    )

    draft.consumed = True

    try:
        await db.commit()
    except Exception:
        await db.rollback()
        raise

    log.info(
        "audit.mandate_revoked",
        user_id=user_id,
        mandate_id=mandate_id,
        agent_id=mandate.agent_id,
        reason=reason,
        cancellations=counts.__dict__,
    )

    return RevocationResult(
        revoked=True,
        already_revoked=False,
        mandate_id=mandate.id,
        agent_id=mandate.agent_id,
        agent_status="revoked",
        revoked_at=now_naive,
        cancellations=counts,
    )


# ---------------------------------------------------------------------------
# Cascade
# ---------------------------------------------------------------------------


async def _cascade_cancellations(
    db: AsyncSession,
    *,
    agent_id: str,
    user_id: str,
    mandate_id: str,
    now: datetime,
) -> CancellationCounts:
    """Apply the side-effects of mandate revocation.

    All updates run inside the caller's transaction; the caller commits
    after this returns. We don't auto-flush mid-cascade (one commit at end).
    """
    # 1. Active negotiations for this agent's intents → cancelled.
    #    Negotiation links to Match → Intent. The agent owns the intent
    #    via `intents.agent_id`, so we find negotiations through the
    #    matches → buy/sell intents pair.
    nego_count = 0
    nego_rows = await db.scalars(
        select(Negotiation).where(Negotiation.status == "active")
    )
    for nego in nego_rows:
        # cheap filter: we'd ideally join via match → intent.agent_id,
        # but that's a multi-hop async query. For V0 + 100 users the
        # full scan is cheap; refactor in 7.x if it shows up in profiles.
        # Identify whether either intent in the match belongs to this agent.
        from app.models.schema import Match

        match = await db.scalar(
            select(Match).where(Match.id == nego.match_id)
        )
        if match is None:
            continue
        buy_intent = await db.scalar(
            select(Intent).where(Intent.id == match.buy_intent_id)
        )
        sell_intent = await db.scalar(
            select(Intent).where(Intent.id == match.sell_intent_id)
        )
        agent_involved = (
            (buy_intent is not None and buy_intent.agent_id == agent_id)
            or (sell_intent is not None and sell_intent.agent_id == agent_id)
        )
        if agent_involved:
            nego.status = "cancelled_revoked"  # capped at 20 chars by schema
            nego.closed_at = now
            nego_count += 1

    # 2. Pending deals where this user is buyer or seller AND status is
    #    pending → cancelled. Confirmed/completed deals are NOT touched.
    #    5.3 unified `pending_buyer`/`pending_seller` into `pending_signatures`;
    #    legacy strings kept in the IN-list for safety until any pre-5.3
    #    rows have aged out (no live data exists, but cheap insurance).
    deal_rows = await db.scalars(
        select(Deal)
        .where(
            (Deal.buyer_user_id == user_id) | (Deal.seller_user_id == user_id)
        )
        .where(
            Deal.status.in_(
                ("pending_signatures", "pending_buyer", "pending_seller")
            )
        )
    )
    deal_count = 0
    for deal in deal_rows:
        deal.status = "cancelled_revoked"  # capped at 20 chars by schema
        deal_count += 1

    # 3. Active intents owned by this agent → paused.
    intent_count = 0
    intent_rows = await db.scalars(
        select(Intent)
        .where(Intent.agent_id == agent_id)
        .where(Intent.status == "active")
    )
    for intent in intent_rows:
        intent.status = "paused"
        intent_count += 1

    # 4. Pending mandate drafts for this user/agent → consumed.
    drafts_result = await db.execute(
        update(MandateDraft)
        .where(MandateDraft.user_id == user_id)
        .where(MandateDraft.agent_id == agent_id)
        .where(MandateDraft.consumed.is_(False))
        .values(consumed=True)
    )
    draft_count = drafts_result.rowcount or 0

    # 5. Pending step-up requests for this mandate → expired.
    step_up_result = await db.execute(
        update(StepUpRequest)
        .where(StepUpRequest.mandate_id == mandate_id)
        .where(StepUpRequest.status == "pending")
        .values(status="expired", resolved_at=now)
    )
    step_up_count = step_up_result.rowcount or 0

    return CancellationCounts(
        negotiations_cancelled=nego_count,
        deals_cancelled=deal_count,
        intents_paused=intent_count,
        pending_drafts_invalidated=draft_count,
        pending_step_ups_invalidated=step_up_count,
    )
