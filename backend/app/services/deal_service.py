"""Deal service — dual-WebAuthn signed agreement record (brief task 5.3).

Closes the marketplace loop: when `negotiation_service.accept_offer`
agrees on a price, this module creates a `Deal` row in
`pending_signatures` state. Both parties (buyer + seller) sign with
their passkey via the standard draft+submit flow (mirrors
`mandate_service` 2.4). When both signatures land, the deal becomes
`confirmed` and post-deal chat (DealMessage) is unlocked. If a
signature fails or 24h elapses without dual sign, the deal expires
and intents revert to `active`.

V0 explicitly does NOT manage money: no Stripe, no escrow, no payment
intent. The Deal is a cryptographic record of agreement; logistics are
delegated to the post-deal chat. Trustee Service (cash escrow + delivery
confirmation) lands in V1.5+ FASE 9 (see `TRADE_WINDOW_FLOW.md`).

Public surface:
  - DealError (+ subclasses)                  — typed errors
  - create_pending_deal(db, ...)              → Deal (no commit; caller does)
  - request_sign_draft(db, ...)               → SignDraftCreated
  - submit_signature(db, ...)                 → SignSubmitResult
  - request_cancel_draft(db, ...)             → SignDraftCreated (kind=cancel)
  - submit_cancel(db, ...)                    → CancelResult
  - expire_deal(db, *, deal_id)               → ExpireResult (scheduler hook)
  - get_deal_for_user / list_deals_for_user

State machine:
  pending_signatures → confirmed (both signed)
  pending_signatures → cancelled (signed cancel by either party)
  pending_signatures → expired   (24h auto-timeout)
  confirmed          → completed (V1.5+ Trustee Service)
  confirmed          → disputed  (V1.5+ Trustee Service)

Rollback path on cancel/expire:
  Both intents revert `matched → active`. The chosen match reverts
  `agreed → discovered` so it's reusable. Competing matches expired by
  the 5.2 mini-auction stay expired — match_scheduler (4.3) will
  rediscover candidates on the next tick. Negotiation row keeps its
  agreed status as historical record.
"""
from __future__ import annotations

import json
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Final

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from webauthn import verify_authentication_response

from app.core import canonicalization
from app.core.config import settings
from app.core.logging import log
from app.models.schema import (
    Deal,
    DealSignatureDraft,
    Intent,
    Match,
    Negotiation,
    User,
)
from app.services import audit_service
from app.services.auth_service import _b64url, _b64url_decode
from app.services.mandate_service import WebAuthnAssertionPayload


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


DEAL_PENDING_TTL_HOURS: Final[int] = 24
DRAFT_TTL_SECONDS: Final[int] = 300  # 5 min, like mandate drafts

CANCELLATION_REASON_USER: Final[str] = "user_cancelled"
CANCELLATION_REASON_EXPIRED: Final[str] = "deal_expired"

DEFAULT_LIST_LIMIT: Final[int] = 20
MAX_LIST_LIMIT: Final[int] = 50


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class DealError(Exception):
    code: str = "deal_error"
    http_status: int = 400


class DealNotFound(DealError):
    code = "deal_not_found"
    http_status = 404


class NotPartyToDeal(DealError):
    code = "not_party_to_deal"
    http_status = 403


class DealNotPending(DealError):
    """Operation requires status='pending_signatures'."""

    code = "deal_not_pending"
    http_status = 409


class AlreadySigned(DealError):
    """The caller's role (buyer | seller) has already signed."""

    code = "already_signed"
    http_status = 409


class DealAlreadyExpired(DealError):
    code = "deal_already_expired"
    http_status = 410


class DealDraftNotFound(DealError):
    code = "deal_draft_not_found"
    http_status = 404


class DealDraftExpired(DealError):
    code = "deal_draft_expired"
    http_status = 410


class DealDraftAlreadyConsumed(DealError):
    code = "deal_draft_already_consumed"
    http_status = 409


class DealWebAuthnVerificationFailed(DealError):
    code = "deal_webauthn_verification_failed"
    http_status = 422


class DealNotConfirmed(DealError):
    """Chat (and other post-confirm actions) require status='confirmed'."""

    code = "deal_not_confirmed"
    http_status = 409


class CannotCancelConfirmedDeal(DealError):
    """V0 doesn't support post-confirm cancel (V1.5+ via Trustee dispute)."""

    code = "cannot_cancel_confirmed_deal"
    http_status = 409


# ---------------------------------------------------------------------------
# Output dataclasses
# ---------------------------------------------------------------------------


@dataclass
class SignDraftCreated:
    draft_id: str
    payload: dict[str, Any]
    challenge_b64url: str
    expires_at_utc: datetime
    role: str
    kind: str


@dataclass
class SignSubmitResult:
    deal_id: str
    role: str
    signed_at: datetime
    deal_confirmed: bool
    confirmed_at: datetime | None


@dataclass
class CancelResult:
    deal_id: str
    cancelled_at: datetime
    cancellation_reason: str
    intents_reverted: int
    matches_reverted: int


@dataclass
class ExpireResult:
    deal_id: str
    expired_at: datetime
    intents_reverted: int
    matches_reverted: int


@dataclass
class DealListPage:
    rows: list[Deal]
    total: int
    limit: int
    offset: int


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _utc_iso_z() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _compute_idempotency_key(
    *, negotiation_id: str, agreed_price_cents: int
) -> str:
    """One negotiation + one agreed price = one deal. Stable by construction."""
    return f"deal_v1_{negotiation_id}_{agreed_price_cents}"


def _role_for_user(*, deal: Deal, user_id: str) -> str:
    if deal.buyer_user_id == user_id:
        return "buyer"
    if deal.seller_user_id == user_id:
        return "seller"
    raise NotPartyToDeal(
        f"user {user_id!r} is not a party to deal {deal.id!r}"
    )


def _has_signed(deal: Deal, role: str) -> bool:
    if role == "buyer":
        return deal.buyer_signature is not None
    return deal.seller_signature is not None


def _build_canonical_payload(
    *,
    deal: Deal,
    user: User,
    role: str,
    kind: str,
    challenge_bytes: bytes,
) -> dict[str, Any]:
    """JCS-canonical signing payload. Same shape regardless of kind so the
    client UI can reuse the WebAuthn flow; `kind` discriminates intent.
    """
    return {
        "version": "1.0",
        "action": f"deal_{kind}",  # 'deal_sign' | 'deal_cancel'
        "deal_id": deal.id,
        "role": role,
        "principal": {
            "user_id": user.id,
            "nullifier_hash": user.nullifier_hash or "",
        },
        "agreed_price_cents": deal.agreed_price_cents,
        "currency": deal.currency,
        "buy_intent_id": deal.buy_intent_id,
        "sell_intent_id": deal.sell_intent_id,
        "negotiation_id": deal.negotiation_id,
        "issued_at": _utc_iso_z(),
        "challenge": challenge_bytes.hex(),
    }


# ---------------------------------------------------------------------------
# create_pending_deal — called from negotiation_service.accept_offer
# ---------------------------------------------------------------------------


async def create_pending_deal(
    db: AsyncSession,
    *,
    negotiation_id: str,
    buy_intent_id: str,
    sell_intent_id: str,
    buyer_user_id: str,
    seller_user_id: str,
    agreed_price_cents: int,
    currency: str = "EUR",
) -> Deal:
    """Create or fetch a pending Deal idempotently.

    Does NOT commit — the caller (`accept_offer` or test harness) owns
    the transaction boundary so the deal lives or dies atomically with
    the negotiation's accept turn.
    """
    idempotency_key = _compute_idempotency_key(
        negotiation_id=negotiation_id, agreed_price_cents=agreed_price_cents
    )

    existing = await db.scalar(
        select(Deal).where(Deal.idempotency_key == idempotency_key)
    )
    if existing is not None:
        return existing

    now = _utcnow()
    deal = Deal(
        id=str(uuid.uuid4()),
        negotiation_id=negotiation_id,
        buyer_user_id=buyer_user_id,
        seller_user_id=seller_user_id,
        buy_intent_id=buy_intent_id,
        sell_intent_id=sell_intent_id,
        agreed_price_cents=agreed_price_cents,
        currency=currency,
        status="pending_signatures",
        created_at=now,
        expires_at=now + timedelta(hours=DEAL_PENDING_TTL_HOURS),
        idempotency_key=idempotency_key,
    )
    db.add(deal)
    await db.flush()

    # Audit. Caller's audit context will commit when it commits the txn.
    await audit_service.log_intent_event(
        db,
        user_id=seller_user_id,  # arbitrary party; `params.deal_id` is canonical
        action=audit_service.DealActions.CREATE,
        params={
            "deal_id": deal.id,
            "negotiation_id": negotiation_id,
            "buy_intent_id": buy_intent_id,
            "sell_intent_id": sell_intent_id,
            "agreed_price_cents": agreed_price_cents,
        },
        result={"status": "pending_signatures"},
        success=True,
    )
    return deal


# ---------------------------------------------------------------------------
# Sign / cancel draft creation
# ---------------------------------------------------------------------------


async def _create_signature_draft(
    db: AsyncSession,
    *,
    user_id: str,
    deal_id: str,
    kind: str,  # 'sign' | 'cancel'
) -> SignDraftCreated:
    """Shared draft-creation path: validates state, builds canonical bytes
    + challenge, persists `DealSignatureDraft` row.
    """
    deal = await db.get(Deal, deal_id)
    if deal is None:
        raise DealNotFound(f"deal {deal_id!r} not found")
    if deal.status == "expired":
        raise DealAlreadyExpired(f"deal {deal_id!r} expired at {deal.expires_at}")
    if deal.status != "pending_signatures":
        # confirmed, cancelled, completed, disputed — cancel/sign not allowed
        # in V0 (V1.5+ Trustee dispute is the only post-confirm path).
        if kind == "cancel" and deal.status == "confirmed":
            raise CannotCancelConfirmedDeal(
                "deal is confirmed; cancellation requires Trustee dispute "
                "(V1.5+, FASE 9)"
            )
        raise DealNotPending(
            f"deal in status {deal.status!r}, not 'pending_signatures'"
        )

    role = _role_for_user(deal=deal, user_id=user_id)
    if kind == "sign" and _has_signed(deal, role):
        raise AlreadySigned(f"{role} already signed deal {deal.id}")

    user = await db.get(User, user_id)
    if user is None:  # pragma: no cover — FK invariant
        raise NotPartyToDeal("user not found")

    challenge_bytes = secrets.token_bytes(32)
    payload = _build_canonical_payload(
        deal=deal, user=user, role=role, kind=kind, challenge_bytes=challenge_bytes
    )
    canonical_bytes = canonicalization.canonicalize(payload)
    expires_at = _utcnow() + timedelta(seconds=DRAFT_TTL_SECONDS)

    draft = DealSignatureDraft(
        id=str(uuid.uuid4()),
        deal_id=deal.id,
        user_id=user_id,
        role=role,
        kind=kind,
        canonical_payload=canonical_bytes,
        challenge=challenge_bytes,
        expires_at=expires_at,
    )
    db.add(draft)
    await db.commit()

    return SignDraftCreated(
        draft_id=draft.id,
        payload=payload,
        challenge_b64url=_b64url(challenge_bytes),
        expires_at_utc=expires_at,
        role=role,
        kind=kind,
    )


async def request_sign_draft(
    db: AsyncSession, *, user_id: str, deal_id: str
) -> SignDraftCreated:
    """Buyer or seller requests the canonical bytes to sign for confirming."""
    return await _create_signature_draft(
        db, user_id=user_id, deal_id=deal_id, kind="sign"
    )


async def request_cancel_draft(
    db: AsyncSession, *, user_id: str, deal_id: str
) -> SignDraftCreated:
    """Buyer or seller requests the canonical bytes to sign for cancelling."""
    return await _create_signature_draft(
        db, user_id=user_id, deal_id=deal_id, kind="cancel"
    )


# ---------------------------------------------------------------------------
# Sign / cancel submit (WebAuthn verification)
# ---------------------------------------------------------------------------


async def _load_and_validate_draft(
    db: AsyncSession,
    *,
    user_id: str,
    draft_id: str,
    expected_kind: str,
) -> tuple[DealSignatureDraft, Deal, User]:
    draft = await db.scalar(
        select(DealSignatureDraft)
        .where(DealSignatureDraft.id == draft_id)
        .where(DealSignatureDraft.user_id == user_id)
        .with_for_update()
    )
    if draft is None:
        raise DealDraftNotFound(f"draft {draft_id!r} not found")
    if draft.kind != expected_kind:
        raise DealDraftNotFound(
            f"draft kind mismatch: expected {expected_kind!r}, got {draft.kind!r}"
        )
    if draft.consumed:
        raise DealDraftAlreadyConsumed("draft already used")
    if draft.expires_at < _utcnow():
        raise DealDraftExpired("draft TTL elapsed")

    deal = await db.scalar(
        select(Deal).where(Deal.id == draft.deal_id).with_for_update()
    )
    if deal is None:  # pragma: no cover — FK invariant
        raise DealNotFound("draft references missing deal")
    if deal.status == "expired":
        raise DealAlreadyExpired(f"deal {deal.id!r} expired during signing")
    if deal.status != "pending_signatures":
        raise DealNotPending(
            f"deal in status {deal.status!r}, not 'pending_signatures'"
        )

    user = await db.get(User, user_id)
    if user is None:  # pragma: no cover — FK invariant
        raise NotPartyToDeal("user not found")

    return draft, deal, user


def _verify_webauthn(
    *,
    assertion: WebAuthnAssertionPayload,
    challenge: bytes,
    user: User,
) -> int:
    try:
        verified = verify_authentication_response(
            credential=assertion.model_dump(by_alias=True),
            expected_challenge=challenge,
            expected_rp_id=settings.webauthn_rp_id,
            expected_origin=settings.webauthn_origin,
            credential_public_key=_b64url_decode(user.passkey_pubkey),
            credential_current_sign_count=user.passkey_sign_count or 0,
            require_user_verification=False,
        )
    except Exception as exc:
        log.info(
            "deal.webauthn_failed",
            user_id=user.id,
            error=type(exc).__name__,
        )
        raise DealWebAuthnVerificationFailed(str(exc)) from exc
    return verified.new_sign_count


async def submit_signature(
    db: AsyncSession,
    *,
    user_id: str,
    draft_id: str,
    assertion: WebAuthnAssertionPayload,
) -> SignSubmitResult:
    """Verify the buyer's or seller's deal-confirm signature.

    On success: marks the role's `*_signature` + `*_signed_at`. If the
    OTHER role had already signed, transitions deal → 'confirmed' and
    sets `confirmed_at`. Audit emitted per role + a separate CONFIRM
    audit when both signatures are now in.
    """
    draft, deal, user = await _load_and_validate_draft(
        db, user_id=user_id, draft_id=draft_id, expected_kind="sign"
    )
    role = draft.role  # set at draft creation time, authoritative
    if _has_signed(deal, role):
        raise AlreadySigned(f"{role} already signed")

    new_sign_count = _verify_webauthn(
        assertion=assertion,
        challenge=bytes(draft.challenge),
        user=user,
    )
    user.passkey_sign_count = new_sign_count
    user.last_active_at = _utcnow()

    signed_at = _utcnow()
    sig_blob = assertion.model_dump(by_alias=True)
    if role == "buyer":
        deal.buyer_signature = sig_blob
        deal.buyer_signed_at = signed_at
    else:
        deal.seller_signature = sig_blob
        deal.seller_signed_at = signed_at

    deal_confirmed = (
        deal.buyer_signature is not None and deal.seller_signature is not None
    )
    if deal_confirmed:
        deal.status = "confirmed"
        deal.confirmed_at = signed_at

    draft.consumed = True

    # Audit: per-role sign event.
    role_action = (
        audit_service.DealActions.BUYER_SIGN
        if role == "buyer"
        else audit_service.DealActions.SELLER_SIGN
    )
    await audit_service.log_intent_event(
        db,
        user_id=user_id,
        action=role_action,
        params={"deal_id": deal.id, "role": role},
        result={"deal_confirmed": deal_confirmed},
        success=True,
    )
    if deal_confirmed:
        await audit_service.log_intent_event(
            db,
            user_id=user_id,
            action=audit_service.DealActions.CONFIRM,
            params={
                "deal_id": deal.id,
                "agreed_price_cents": deal.agreed_price_cents,
            },
            result={"status": "confirmed"},
            success=True,
        )

    await db.commit()

    return SignSubmitResult(
        deal_id=deal.id,
        role=role,
        signed_at=signed_at,
        deal_confirmed=deal_confirmed,
        confirmed_at=deal.confirmed_at if deal_confirmed else None,
    )


async def submit_cancel(
    db: AsyncSession,
    *,
    user_id: str,
    draft_id: str,
    assertion: WebAuthnAssertionPayload,
) -> CancelResult:
    """Verify the cancel signature, transition deal → 'cancelled', rollback.

    Rollback path: both intents revert `matched → active`; the chosen
    match reverts `agreed → discovered`. Match scheduler will rediscover
    candidates on the next tick.
    """
    draft, deal, user = await _load_and_validate_draft(
        db, user_id=user_id, draft_id=draft_id, expected_kind="cancel"
    )

    new_sign_count = _verify_webauthn(
        assertion=assertion,
        challenge=bytes(draft.challenge),
        user=user,
    )
    user.passkey_sign_count = new_sign_count
    user.last_active_at = _utcnow()

    cancelled_at = _utcnow()
    deal.status = "cancelled"
    deal.cancelled_at = cancelled_at
    deal.cancellation_reason = CANCELLATION_REASON_USER

    intents_reverted, matches_reverted = await _rollback_deal_state(
        db, deal=deal
    )

    draft.consumed = True

    await audit_service.log_intent_event(
        db,
        user_id=user_id,
        action=audit_service.DealActions.CANCEL,
        params={
            "deal_id": deal.id,
            "reason": CANCELLATION_REASON_USER,
        },
        result={
            "intents_reverted": intents_reverted,
            "matches_reverted": matches_reverted,
        },
        success=True,
    )

    await db.commit()

    return CancelResult(
        deal_id=deal.id,
        cancelled_at=cancelled_at,
        cancellation_reason=CANCELLATION_REASON_USER,
        intents_reverted=intents_reverted,
        matches_reverted=matches_reverted,
    )


# ---------------------------------------------------------------------------
# Rollback + expiry
# ---------------------------------------------------------------------------


async def _rollback_deal_state(
    db: AsyncSession, *, deal: Deal
) -> tuple[int, int]:
    """Revert intents `matched → active` and the chosen match `agreed →
    discovered`. The chosen match is resolved via the linking Negotiation
    row (Deal doesn't carry match_id directly).
    """
    intents_reverted = 0
    for intent_id in (deal.buy_intent_id, deal.sell_intent_id):
        intent = await db.scalar(
            select(Intent).where(Intent.id == intent_id).with_for_update()
        )
        if intent is not None and intent.status == "matched":
            intent.status = "active"
            intents_reverted += 1

    matches_reverted = 0
    nego = await db.get(Negotiation, deal.negotiation_id)
    if nego is not None:
        chosen_match = await db.scalar(
            select(Match)
            .where(Match.id == nego.match_id)
            .with_for_update()
        )
        if chosen_match is not None and chosen_match.status == "agreed":
            chosen_match.status = "discovered"
            matches_reverted = 1

    return intents_reverted, matches_reverted


async def expire_deal(
    db: AsyncSession, *, deal_id: str
) -> ExpireResult:
    """Auto-expire a single pending deal. Called by match_scheduler tick.

    Idempotent: if deal isn't pending or doesn't exist, returns a zeroed
    result instead of raising — the scheduler iterates over a snapshot,
    expects each item may already be moot.
    """
    deal = await db.scalar(
        select(Deal).where(Deal.id == deal_id).with_for_update()
    )
    if deal is None:
        return ExpireResult(
            deal_id=deal_id,
            expired_at=_utcnow(),
            intents_reverted=0,
            matches_reverted=0,
        )
    if deal.status != "pending_signatures":
        return ExpireResult(
            deal_id=deal.id,
            expired_at=_utcnow(),
            intents_reverted=0,
            matches_reverted=0,
        )

    expired_at = _utcnow()
    deal.status = "expired"
    deal.cancelled_at = expired_at
    deal.cancellation_reason = CANCELLATION_REASON_EXPIRED

    intents_reverted, matches_reverted = await _rollback_deal_state(
        db, deal=deal
    )

    await audit_service.log_intent_event(
        db,
        user_id=deal.buyer_user_id,  # arbitrary; deal_id is canonical
        action=audit_service.DealActions.EXPIRE,
        params={
            "deal_id": deal.id,
            "reason": CANCELLATION_REASON_EXPIRED,
        },
        result={
            "intents_reverted": intents_reverted,
            "matches_reverted": matches_reverted,
        },
        success=True,
    )

    await db.commit()

    return ExpireResult(
        deal_id=deal.id,
        expired_at=expired_at,
        intents_reverted=intents_reverted,
        matches_reverted=matches_reverted,
    )


# ---------------------------------------------------------------------------
# Read API
# ---------------------------------------------------------------------------


async def get_deal_for_user(
    db: AsyncSession, *, user_id: str, deal_id: str
) -> Deal:
    deal = await db.get(Deal, deal_id)
    if deal is None:
        raise DealNotFound(f"deal {deal_id!r} not found")
    if user_id not in (deal.buyer_user_id, deal.seller_user_id):
        raise NotPartyToDeal(
            f"user {user_id!r} is not a party to deal {deal.id!r}"
        )
    return deal


async def list_deals_for_user(
    db: AsyncSession,
    *,
    user_id: str,
    status: str | None = None,
    limit: int = DEFAULT_LIST_LIMIT,
    offset: int = 0,
) -> DealListPage:
    limit = max(1, min(MAX_LIST_LIMIT, limit))
    offset = max(0, offset)

    base_filters = [
        or_(
            Deal.buyer_user_id == user_id,
            Deal.seller_user_id == user_id,
        )
    ]
    if status is not None:
        base_filters.append(Deal.status == status)

    total = int(
        await db.scalar(
            select(func.count())
            .select_from(Deal)
            .where(and_(*base_filters))
        )
        or 0
    )
    rows = list(
        await db.scalars(
            select(Deal)
            .where(and_(*base_filters))
            .order_by(Deal.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
    )
    return DealListPage(rows=rows, total=total, limit=limit, offset=offset)
