"""Negotiation service — turn-based offer/counter-offer state machine (brief task 5.1).

This is the structured layer where two agents (or, in V0/test, the human
calling the API directly) exchange `offer → counter_offer → ... → accept|reject`
on a discovered match. The 4 primitives below are deliberately
agent-agnostic: they don't talk to Claude, don't make UX decisions,
and don't require an active orchestrator. The FASE 6 agent runtime
will *call* them, not live inside them — keeping the negotiation
mechanics testable in isolation.

Public surface:
  - NegotiationError (+ subclasses)               — typed errors
  - start_or_continue(db, ...)         → TurnResult — first offer or counter
  - accept_offer(db, ...)              → AcceptResult
  - reject_offer(db, ...)              → RejectResult
  - get_negotiation_state(db, ...)     → Negotiation row (party-only)
  - list_negotiations_for_user(db, ...) → NegotiationListPage
  - cancel_negotiations_for_intent(db, ...) → int (intent-cancel cascade)

State machine:
  Match:        discovered ─→ negotiating ─→ agreed | rejected | expired | cancelled
  Negotiation:  active     ─→ agreed | rejected | expired | cancelled

`Negotiation.state` JSONB shape:

    {
      "turns": [
        {"turn_number": 1, "agent_id": "...", "type": "offer",
         "price_cents": 120000, "message": "...", "timestamp": "..."},
        ...
      ],
      "is_final_round": false,
      "final_status": null | "agreed" | "rejected",
      "agreed_price_cents": null | int
    }

Concurrency: pessimistic locks (`with_for_update()`) on Match + Negotiation
rows for the duration of each mutating call. Combined with the unique
constraint `negotiations.match_id`, this prevents two concurrent first-
offer calls from creating two negotiations on the same match — the
second call deadlocks-then-retries-as-continuation, OR raises on the
unique constraint, depending on isolation level.

V0 caps:
  - `MAX_ROUNDS = 6` hardcoded. V1+: `mandate.limits.max_rounds_per_negotiation`.
  - `MAX_MESSAGE_LENGTH = 500` (truncation, not validation — the brief's
    UX choice: don't reject a verbose user, just trim).
  - `MAX_PRICE_CENTS = 10_000_00` mirroring intent's per-intent cap.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Final

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.schema import Agent, Intent, Match, Negotiation
from app.services import audit_service


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


MAX_ROUNDS: Final[int] = 6
MAX_MESSAGE_LENGTH: Final[int] = 500
MAX_PRICE_CENTS: Final[int] = 10_000_00  # €10K, mirrors intent cap

DEFAULT_LIST_LIMIT: Final[int] = 20
MAX_LIST_LIMIT: Final[int] = 50


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class NegotiationError(Exception):
    code: str = "negotiation_error"
    http_status: int = 400


class MatchNotFoundForNegotiation(NegotiationError):
    code = "match_not_found"
    http_status = 404


class InvalidMatchState(NegotiationError):
    code = "invalid_match_state"
    http_status = 409


class AgentNotOwned(NegotiationError):
    """`agent_id` doesn't belong to the authenticated user."""

    code = "agent_not_owned"
    http_status = 403


class AgentNotInUsableState(NegotiationError):
    """Agent revoked / paused — can't act on its behalf."""

    code = "agent_not_in_usable_state"
    http_status = 409


class AgentNotPartyToMatch(NegotiationError):
    """Caller's user owns neither side of the match."""

    code = "agent_not_party_to_match"
    http_status = 403


class NegotiationNotFound(NegotiationError):
    code = "negotiation_not_found"
    http_status = 404


class NegotiationNotActive(NegotiationError):
    code = "negotiation_not_active"
    http_status = 409


class NegotiationNotForUser(NegotiationError):
    code = "negotiation_not_for_user"
    http_status = 403


class MaxRoundsReached(NegotiationError):
    code = "max_rounds_reached"
    http_status = 409


class NoOfferToAccept(NegotiationError):
    code = "no_offer_to_accept"
    http_status = 409


class CannotActOnOwnOffer(NegotiationError):
    """Accept/reject of one's own last turn — must come from the counterparty."""

    code = "cannot_act_on_own_offer"
    http_status = 409


class InvalidPrice(NegotiationError):
    code = "invalid_price"
    http_status = 422


# ---------------------------------------------------------------------------
# Output dataclasses
# ---------------------------------------------------------------------------


@dataclass
class TurnResult:
    negotiation_id: str
    rounds_used: int
    max_rounds: int
    is_final_round: bool
    last_turn: dict[str, Any]
    status: str
    created_new: bool


@dataclass
class AcceptResult:
    negotiation_id: str
    match_id: str
    agreed_price_cents: int
    next_step: str  # placeholder for 5.3 deal handoff


@dataclass
class RejectResult:
    negotiation_id: str
    match_id: str
    reason: str | None


@dataclass
class NegotiationListPage:
    rows: list[Negotiation]
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


def _validate_price(price_cents: int) -> None:
    if not isinstance(price_cents, int) or price_cents <= 0:
        raise InvalidPrice("price_cents must be a positive integer")
    if price_cents > MAX_PRICE_CENTS:
        raise InvalidPrice(
            f"price_cents exceeds platform limit {MAX_PRICE_CENTS}"
        )


def _truncate_message(message: str | None) -> str:
    """Brief 5.1 chose truncation over validation: don't reject a verbose
    user, just trim. UI mobile is the authoritative input length anyway."""
    if message is None:
        return ""
    return message[:MAX_MESSAGE_LENGTH]


async def _load_match_locked(
    db: AsyncSession, match_id: str
) -> Match:
    match = await db.scalar(
        select(Match).where(Match.id == match_id).with_for_update()
    )
    if match is None:
        raise MatchNotFoundForNegotiation(f"match {match_id!r} not found")
    return match


async def _load_negotiation_locked(
    db: AsyncSession, negotiation_id: str
) -> Negotiation:
    nego = await db.scalar(
        select(Negotiation)
        .where(Negotiation.id == negotiation_id)
        .with_for_update()
    )
    if nego is None:
        raise NegotiationNotFound(
            f"negotiation {negotiation_id!r} not found"
        )
    return nego


async def _verify_agent_ownership(
    db: AsyncSession, *, agent_id: str, user_id: str, accept_pending: bool
) -> Agent:
    """Return Agent if owned + in usable state; raise otherwise.

    `accept_pending=True` allows tier-1 agents (`status='pending_mandate'`)
    in addition to active ones — used by start/continue/reject.
    `accept_pending=False` requires `active` — used by accept (deal hand-off).
    """
    agent = await db.get(Agent, agent_id)
    if agent is None or agent.user_id != user_id:
        raise AgentNotOwned(f"agent {agent_id!r} not owned by caller")
    allowed = ("active",) if not accept_pending else ("active", "pending_mandate")
    if agent.status not in allowed:
        raise AgentNotInUsableState(
            f"agent in status {agent.status!r}, expected one of {allowed}"
        )
    return agent


async def _verify_user_party_to_match(
    db: AsyncSession, *, user_id: str, match: Match
) -> tuple[Intent, Intent]:
    buy_intent = await db.get(Intent, match.buy_intent_id)
    sell_intent = await db.get(Intent, match.sell_intent_id)
    if buy_intent is None or sell_intent is None:
        # FK constraint should make this unreachable; defensive guard.
        raise MatchNotFoundForNegotiation(
            "match references missing intent"
        )
    if user_id not in (buy_intent.user_id, sell_intent.user_id):
        raise AgentNotPartyToMatch(
            "caller does not own either side of the match"
        )
    return buy_intent, sell_intent


def _append_turn(nego: Negotiation, turn: dict[str, Any]) -> None:
    """Append a turn to the JSONB state, with reassignment for SA tracking.

    SQLAlchemy doesn't deep-track mutations inside JSONB columns by default.
    We always reassign `nego.state` to a new dict so the change is flushed.
    """
    state = dict(nego.state or {})
    turns = list(state.get("turns") or [])
    turns.append(turn)
    state["turns"] = turns
    nego.state = state


def _set_state_keys(nego: Negotiation, **kwargs: Any) -> None:
    state = dict(nego.state or {})
    state.update(kwargs)
    nego.state = state


# ---------------------------------------------------------------------------
# Public API: start_or_continue
# ---------------------------------------------------------------------------


async def start_or_continue(
    db: AsyncSession,
    *,
    user_id: str,
    agent_id: str,
    match_id: str,
    price_cents: int,
    message: str | None = None,
) -> TurnResult:
    """First offer or subsequent counter-offer on `match_id`. Tier ≥ 1."""
    _validate_price(price_cents)

    # 1. Auth — agent owned, in usable state.
    await _verify_agent_ownership(
        db, agent_id=agent_id, user_id=user_id, accept_pending=True
    )

    # 2. Lock the match + verify acceptable status.
    match = await _load_match_locked(db, match_id)
    if match.status not in ("discovered", "negotiating"):
        raise InvalidMatchState(
            f"match in status {match.status!r}, cannot negotiate"
        )

    # 3. Caller's user must own one side of the match.
    await _verify_user_party_to_match(db, user_id=user_id, match=match)

    # 4. Lock existing negotiation if any; otherwise create fresh.
    nego = await db.scalar(
        select(Negotiation)
        .where(Negotiation.match_id == match_id)
        .with_for_update()
    )
    created_new = nego is None
    if nego is None:
        nego = Negotiation(
            match_id=match_id,
            state={
                "turns": [],
                "is_final_round": False,
                "final_status": None,
                "agreed_price_cents": None,
            },
            rounds_used=0,
            max_rounds=MAX_ROUNDS,
            current_price_cents=None,
            status="active",
            started_at=_utcnow(),
        )
        db.add(nego)
        await db.flush()
        # Match lifecycle transition: discovered → negotiating.
        if match.status == "discovered":
            match.status = "negotiating"

    if nego.status != "active":
        raise NegotiationNotActive(
            f"negotiation in status {nego.status!r}, cannot continue"
        )
    if nego.rounds_used >= nego.max_rounds:
        raise MaxRoundsReached(
            f"max_rounds={nego.max_rounds} reached; only accept/reject allowed"
        )

    # 5. Build + append turn.
    turn_type = "offer" if not (nego.state or {}).get("turns") else "counter_offer"
    nego.rounds_used = (nego.rounds_used or 0) + 1
    new_turn = {
        "turn_number": nego.rounds_used,
        "agent_id": agent_id,
        "type": turn_type,
        "price_cents": price_cents,
        "message": _truncate_message(message),
        "timestamp": _utc_iso_z(),
    }
    _append_turn(nego, new_turn)
    _set_state_keys(
        nego, is_final_round=(nego.rounds_used >= nego.max_rounds - 1)
    )
    nego.current_price_cents = price_cents

    await db.flush()

    # 6. Audit.
    action = (
        audit_service.NegotiationActions.SEND_OFFER
        if turn_type == "offer"
        else audit_service.NegotiationActions.SEND_COUNTER_OFFER
    )
    await audit_service.log_intent_event(
        db,
        user_id=user_id,
        action=action,
        params={
            "negotiation_id": nego.id,
            "match_id": match_id,
            "agent_id": agent_id,
            "turn_number": nego.rounds_used,
            "price_cents": price_cents,
        },
        result={"created_new": created_new, "turn_type": turn_type},
        success=True,
        agent_id=agent_id,
    )

    await db.commit()
    await db.refresh(nego)

    return TurnResult(
        negotiation_id=nego.id,
        rounds_used=nego.rounds_used,
        max_rounds=nego.max_rounds,
        is_final_round=bool((nego.state or {}).get("is_final_round")),
        last_turn=new_turn,
        status=nego.status,
        created_new=created_new,
    )


# ---------------------------------------------------------------------------
# Public API: accept_offer
# ---------------------------------------------------------------------------


async def accept_offer(
    db: AsyncSession,
    *,
    user_id: str,
    agent_id: str,
    negotiation_id: str,
) -> AcceptResult:
    """Accept the counterparty's last turn. Tier ≥ 2 (deal hand-off in 5.3)."""
    # 1. Auth — agent active (not pending; accept implies imminent deal).
    await _verify_agent_ownership(
        db, agent_id=agent_id, user_id=user_id, accept_pending=False
    )

    # 2. Lock the negotiation, verify state.
    nego = await _load_negotiation_locked(db, negotiation_id)
    if nego.status != "active":
        raise NegotiationNotActive(
            f"negotiation in status {nego.status!r}"
        )
    turns = (nego.state or {}).get("turns") or []
    if not turns:
        raise NoOfferToAccept("no offers in this negotiation yet")
    last_turn = turns[-1]
    if last_turn["agent_id"] == agent_id:
        raise CannotActOnOwnOffer(
            "cannot accept your own last offer; counterparty must do it"
        )

    # 3. Verify match still acceptable + caller is party.
    match = await _load_match_locked(db, nego.match_id)
    await _verify_user_party_to_match(db, user_id=user_id, match=match)

    agreed_price = int(last_turn["price_cents"])

    # 4. Append accept turn.
    accept_turn = {
        "turn_number": (nego.rounds_used or 0) + 1,
        "agent_id": agent_id,
        "type": "accept",
        "price_cents": agreed_price,
        "message": "",
        "timestamp": _utc_iso_z(),
    }
    _append_turn(nego, accept_turn)
    _set_state_keys(
        nego, final_status="agreed", agreed_price_cents=agreed_price
    )
    nego.status = "agreed"
    nego.closed_at = _utcnow()
    match.status = "agreed"

    await db.flush()

    # 5. Audit.
    await audit_service.log_intent_event(
        db,
        user_id=user_id,
        action=audit_service.NegotiationActions.ACCEPT_OFFER,
        params={
            "negotiation_id": nego.id,
            "match_id": nego.match_id,
            "agent_id": agent_id,
            "agreed_price_cents": agreed_price,
        },
        result={"status": "agreed"},
        success=True,
        agent_id=agent_id,
    )

    await db.commit()

    return AcceptResult(
        negotiation_id=nego.id,
        match_id=nego.match_id,
        agreed_price_cents=agreed_price,
        next_step="create_deal_in_5_3",
    )


# ---------------------------------------------------------------------------
# Public API: reject_offer
# ---------------------------------------------------------------------------


async def reject_offer(
    db: AsyncSession,
    *,
    user_id: str,
    agent_id: str,
    negotiation_id: str,
    reason: str | None = None,
) -> RejectResult:
    """Reject the counterparty's last turn. Tier ≥ 1."""
    await _verify_agent_ownership(
        db, agent_id=agent_id, user_id=user_id, accept_pending=True
    )

    nego = await _load_negotiation_locked(db, negotiation_id)
    if nego.status != "active":
        raise NegotiationNotActive(
            f"negotiation in status {nego.status!r}"
        )
    turns = (nego.state or {}).get("turns") or []
    if not turns:
        raise NoOfferToAccept("no offers in this negotiation yet")
    last_turn = turns[-1]
    if last_turn["agent_id"] == agent_id:
        raise CannotActOnOwnOffer(
            "cannot reject your own last offer; counterparty must do it"
        )

    match = await _load_match_locked(db, nego.match_id)
    await _verify_user_party_to_match(db, user_id=user_id, match=match)

    reject_turn = {
        "turn_number": (nego.rounds_used or 0) + 1,
        "agent_id": agent_id,
        "type": "reject",
        "price_cents": int(last_turn["price_cents"]),
        "message": _truncate_message(reason),
        "timestamp": _utc_iso_z(),
    }
    _append_turn(nego, reject_turn)
    _set_state_keys(nego, final_status="rejected")
    nego.status = "rejected"
    nego.closed_at = _utcnow()
    match.status = "rejected"

    await db.flush()

    await audit_service.log_intent_event(
        db,
        user_id=user_id,
        action=audit_service.NegotiationActions.REJECT_OFFER,
        params={
            "negotiation_id": nego.id,
            "match_id": nego.match_id,
            "agent_id": agent_id,
        },
        result={"status": "rejected"},
        success=True,
        agent_id=agent_id,
    )

    await db.commit()

    return RejectResult(
        negotiation_id=nego.id,
        match_id=nego.match_id,
        reason=reason,
    )


# ---------------------------------------------------------------------------
# Public API: get_negotiation_state + listing
# ---------------------------------------------------------------------------


async def get_negotiation_state(
    db: AsyncSession, *, user_id: str, negotiation_id: str
) -> Negotiation:
    """Read-only fetch. Caller must be party to the underlying match."""
    nego = await db.get(Negotiation, negotiation_id)
    if nego is None:
        raise NegotiationNotFound(
            f"negotiation {negotiation_id!r} not found"
        )
    match = await db.get(Match, nego.match_id)
    if match is None:  # pragma: no cover — FK invariant
        raise NegotiationNotFound("negotiation references missing match")
    buy_intent = await db.get(Intent, match.buy_intent_id)
    sell_intent = await db.get(Intent, match.sell_intent_id)
    if buy_intent is None or sell_intent is None:  # pragma: no cover — FK
        raise NegotiationNotFound("match references missing intent")
    if user_id not in (buy_intent.user_id, sell_intent.user_id):
        # 403 not 404: the caller already has a negotiation_id, which they
        # could only have obtained legitimately if they're authorized.
        # 404 here would suggest the row doesn't exist; 403 says "exists,
        # but not yours".
        raise NegotiationNotForUser(
            "negotiation exists but caller is not party to its match"
        )
    return nego


async def list_negotiations_for_user(
    db: AsyncSession,
    *,
    user_id: str,
    status: str | None = None,
    limit: int = DEFAULT_LIST_LIMIT,
    offset: int = 0,
) -> NegotiationListPage:
    """List negotiations on matches the user is a party to, paginated."""
    limit = max(1, min(MAX_LIST_LIMIT, limit))
    offset = max(0, offset)

    user_intent_ids = (
        select(Intent.id).where(Intent.user_id == user_id)
    ).scalar_subquery()

    base_filters = [
        Negotiation.match_id.in_(
            select(Match.id).where(
                or_(
                    Match.buy_intent_id.in_(user_intent_ids),
                    Match.sell_intent_id.in_(user_intent_ids),
                )
            )
        )
    ]
    if status is not None:
        base_filters.append(Negotiation.status == status)

    total = int(
        await db.scalar(
            select(func.count())
            .select_from(Negotiation)
            .where(and_(*base_filters))
        )
        or 0
    )

    rows = list(
        await db.scalars(
            select(Negotiation)
            .where(and_(*base_filters))
            .order_by(Negotiation.started_at.desc())
            .limit(limit)
            .offset(offset)
        )
    )

    return NegotiationListPage(rows=rows, total=total, limit=limit, offset=offset)


# ---------------------------------------------------------------------------
# Cascade hooks (called from intent_service.cancel_intent)
# ---------------------------------------------------------------------------


async def cancel_negotiations_for_intent(
    db: AsyncSession, *, intent_id: str
) -> int:
    """Mark all active negotiations on matches involving `intent_id` as cancelled.

    Returns rowcount. Caller (intent_service.cancel_intent) holds the
    transaction; this helper does not commit. Audit emission per cancelled
    negotiation is deferred to V0.5+ — for V0 we accept that the cascade
    is an aggregate event captured by the parent `cancel_intent` audit row.
    """
    result = await db.execute(
        update(Negotiation)
        .where(
            Negotiation.match_id.in_(
                select(Match.id).where(
                    or_(
                        Match.buy_intent_id == intent_id,
                        Match.sell_intent_id == intent_id,
                    )
                )
            )
        )
        .where(Negotiation.status == "active")
        .values(status="cancelled", closed_at=_utcnow())
    )
    return int(result.rowcount or 0)
