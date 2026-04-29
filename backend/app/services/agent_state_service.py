"""Agent state full-reload (brief task 6.2).

The orchestrator (6.3) runs each agent on a 60s scheduler. The PROJECT_BRIEF
§2 architectural principle says agents have **no conversational memory
between ticks** — they reload their entire world state from the DB each
time. `get_full_state` is that reload.

Returned `AgentFullState` is JSON-serializable (Pydantic v2). The 6.3
orchestrator will pass `state.model_dump(mode='json')` into Claude's
prompt context. Privacy invariants from earlier phases (DQ-31:
counterparty `ideal_price_eur` not exposed) are enforced by the view
models, not at the prompt-construction layer — defense in depth.

Read-only: no commits, no mutations. Multiple round-trips to the DB
(identity, mandate, intents, matches, negotiations, deals, step-ups,
inbox) — each query uses an existing index. V0 acceptable cost; V1+
may consolidate via a single REPEATABLE READ snapshot if perf bites.

Performance target V0 (single-process, no cache):
  p50: < 100 ms
  p99: < 500 ms

`get_full_state` does NOT update `agents.last_tick_at` — that's the
orchestrator's responsibility post-tick (so a failed tick doesn't move
the cursor and miss inbox events).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Final

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.datetime_helpers import days_until, is_near_cap, minutes_until
from app.models.schema import (
    Agent,
    Deal,
    Intent,
    Mandate,
    Match,
    Negotiation,
    StepUpRequest,
    User,
)
from app.models.views import (
    AgentFullState,
    DealView,
    IntentView,
    LimitsRemaining,
    MandateView,
    MatchView,
    NegotiationView,
    OfferView,
    OtherIntentView,
    StepUpView,
    truncate_description,
)
from app.services import inbox_service


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class AgentStateError(Exception):
    code: str = "agent_state_error"
    http_status: int = 400


class AgentNotFound(AgentStateError):
    code = "agent_not_found"
    http_status = 404


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def get_full_state(
    db: AsyncSession,
    *,
    agent_id: str,
) -> AgentFullState:
    """Return the agent's complete world snapshot.

    Raises `AgentNotFound` if `agent_id` doesn't exist; otherwise always
    returns — even for revoked or pending-mandate agents (the prompt
    needs to know about them too, just with reduced fields).
    """
    agent = await db.get(Agent, agent_id)
    if agent is None:
        raise AgentNotFound(f"agent {agent_id!r} not found")

    user = await db.get(User, agent.user_id)
    if user is None:  # pragma: no cover — FK invariant
        raise AgentNotFound(f"agent {agent_id!r} references missing user")

    snapshot_at = _utcnow()

    mandate_row = await _load_active_mandate(db, agent.id)
    mandate_view = _mandate_view(mandate_row, snapshot_at) if mandate_row else None
    limits_view = _limits_remaining_view(mandate_row, snapshot_at) if mandate_row else None

    active_intents = await _build_intents_view(db, user_id=agent.user_id)
    discovered_matches = await _build_matches_view(db, user_id=agent.user_id)
    active_negotiations = await _build_negotiations_view(
        db, user_id=agent.user_id, agent_id=agent.id
    )
    pending_deals = await _build_pending_deals_view(
        db, user_id=agent.user_id
    )
    pending_step_ups = await _build_pending_step_ups_view(
        db, agent_id=agent.id
    )

    inbox = await inbox_service.get_inbox_for_agent(
        db,
        agent_id=agent.id,
        user_id=agent.user_id,
        since=agent.last_tick_at,
    )

    next_action_required = _has_pending_work(
        active_negotiations=active_negotiations,
        pending_deals=pending_deals,
        inbox=inbox,
    )

    return AgentFullState(
        agent_id=agent.id,
        user_id=agent.user_id,
        agent_status=agent.status,
        nullifier_pseudonym=_nullifier_pseudonym(user.nullifier_hash),
        mandate=mandate_view,
        limits_remaining=limits_view,
        active_intents=active_intents,
        discovered_matches=discovered_matches,
        active_negotiations=active_negotiations,
        pending_deals=pending_deals,
        inbox=inbox,
        pending_step_ups=pending_step_ups,
        snapshot_at=snapshot_at,
        last_tick_at=agent.last_tick_at,
        next_action_required=next_action_required,
    )


# ---------------------------------------------------------------------------
# Mandate + limits
# ---------------------------------------------------------------------------


async def _load_active_mandate(db: AsyncSession, agent_id: str) -> Mandate | None:
    """Most recent non-revoked mandate for the agent. None if none active."""
    return await db.scalar(
        select(Mandate)
        .where(Mandate.agent_id == agent_id)
        .where(Mandate.revoked_at.is_(None))
        .order_by(Mandate.issued_at.desc())
    )


def _mandate_view(mandate: Mandate, snapshot_at: datetime) -> MandateView:
    return MandateView(
        mandate_id=mandate.id,
        issued_at=mandate.issued_at,
        expires_at=mandate.expires_at,
        days_until_expiry=days_until(mandate.expires_at, now=snapshot_at),
        allowed_actions=list((mandate.scope or {}).get("allowed_actions") or []),
        forbidden_actions=list((mandate.scope or {}).get("forbidden_actions") or []),
        limits=dict(mandate.limits or {}),
        step_up_required_for=list(mandate.step_up_required_for or []),
        constraints=dict(mandate.constraints or {}),
    )


def _limits_remaining_view(
    mandate: Mandate, snapshot_at: datetime
) -> LimitsRemaining:
    """Compute remaining budget. Daily counters reset at UTC midnight.

    The mandate's `last_reset_date` may lag the actual UTC day; we
    surface remaining as if the reset already happened (the verifier
    does the lazy reset on its next call). This makes the prompt
    honest about post-reset capacity instead of stale "0 remaining".
    """
    limits = mandate.limits or {}
    daily_cap_eur = float(limits.get("max_total_volume_eur_per_day") or 0)
    mandate_cap_eur = float(limits.get("max_total_volume_eur_per_mandate") or 0)
    deals_cap = int(limits.get("max_deals_per_day") or 0)

    spent_today_eur = float(mandate.spent_today_eur or 0)
    spent_total_eur = float(mandate.spent_total_eur or 0)
    deals_today = int(mandate.deals_count or 0)

    # If daily counter is stale (last_reset_date < UTC today), behave
    # as if reset.
    last_reset = mandate.last_reset_date
    if last_reset is not None and last_reset.date() < snapshot_at.date():
        spent_today_eur = 0.0
        deals_today = 0

    return LimitsRemaining(
        daily_volume_remaining_cents=max(
            0, int((daily_cap_eur - spent_today_eur) * 100)
        ),
        mandate_total_volume_remaining_cents=max(
            0, int((mandate_cap_eur - spent_total_eur) * 100)
        ),
        deals_remaining_today=max(0, deals_cap - deals_today),
        daily_reset_at=_next_utc_midnight(snapshot_at),
        is_at_daily_cap=daily_cap_eur > 0
        and spent_today_eur >= daily_cap_eur,
        is_near_mandate_cap=is_near_cap(spent_total_eur, mandate_cap_eur),
    )


def _next_utc_midnight(now: datetime) -> datetime:
    """Naive-UTC midnight at the start of `now`'s next day."""
    tomorrow = (now + timedelta(days=1)).date()
    return datetime(tomorrow.year, tomorrow.month, tomorrow.day)


# ---------------------------------------------------------------------------
# Intents
# ---------------------------------------------------------------------------


async def _build_intents_view(
    db: AsyncSession, *, user_id: str
) -> list[IntentView]:
    """Active intents owned by the user, with computed match/negotiation flags."""
    intents = list(
        await db.scalars(
            select(Intent)
            .where(Intent.user_id == user_id)
            .where(Intent.status == "active")
            .order_by(Intent.created_at.desc())
        )
    )
    if not intents:
        return []

    intent_ids = [i.id for i in intents]
    # Match counts per intent (one query, grouped).
    match_count_rows = list(
        await db.execute(
            select(
                Match.id,
                Match.buy_intent_id,
                Match.sell_intent_id,
                Match.status,
            ).where(
                or_(
                    Match.buy_intent_id.in_(intent_ids),
                    Match.sell_intent_id.in_(intent_ids),
                )
            )
        )
    )
    matches_by_intent: dict[str, int] = {iid: 0 for iid in intent_ids}
    matches_in_negotiation: set[str] = set()
    for _mid, buy_id, sell_id, status in match_count_rows:
        if status == "discovered":
            for iid in (buy_id, sell_id):
                if iid in matches_by_intent:
                    matches_by_intent[iid] += 1
        elif status == "negotiating":
            for iid in (buy_id, sell_id):
                if iid in matches_by_intent:
                    matches_in_negotiation.add(iid)
                    matches_by_intent[iid] += 1

    snapshot_at = _utcnow()
    return [
        IntentView(
            intent_id=i.id,
            side=i.side,
            title=i.title,
            description=truncate_description(i.description),
            category=i.category,
            reservation_price_eur=i.reservation_price_cents / 100,
            ideal_price_eur=i.ideal_price_cents / 100,
            currency=i.currency,
            status=i.status,
            expires_at=i.expires_at,
            days_until_expiry=days_until(i.expires_at, now=snapshot_at),
            match_count_active=matches_by_intent.get(i.id, 0),
            has_active_negotiation=i.id in matches_in_negotiation,
        )
        for i in intents
    ]


# ---------------------------------------------------------------------------
# Matches
# ---------------------------------------------------------------------------


async def _build_matches_view(
    db: AsyncSession, *, user_id: str
) -> list[MatchView]:
    """Discovered matches whose intents the user owns.

    Sorted by combined_score DESC (prompt prioritization).
    """
    user_intent_ids_subq = (
        select(Intent.id).where(Intent.user_id == user_id)
    ).scalar_subquery()

    matches = list(
        await db.scalars(
            select(Match)
            .where(
                or_(
                    Match.buy_intent_id.in_(user_intent_ids_subq),
                    Match.sell_intent_id.in_(user_intent_ids_subq),
                )
            )
            .where(Match.status.in_(("discovered", "negotiating")))
            .order_by(Match.combined_score.desc())
        )
    )
    if not matches:
        return []

    # Bulk-load both intents per match.
    all_intent_ids: set[str] = set()
    for m in matches:
        all_intent_ids.add(m.buy_intent_id)
        all_intent_ids.add(m.sell_intent_id)
    intent_rows = list(
        await db.scalars(select(Intent).where(Intent.id.in_(all_intent_ids)))
    )
    intents_by_id: dict[str, Intent] = {i.id: i for i in intent_rows}

    # Negotiation existence flag.
    nego_match_ids = set(
        await db.scalars(
            select(Negotiation.match_id).where(
                Negotiation.match_id.in_([m.id for m in matches])
            )
        )
    )

    out: list[MatchView] = []
    for m in matches:
        # The user owns ONE side; the OTHER intent is the counterparty.
        buy_intent = intents_by_id.get(m.buy_intent_id)
        sell_intent = intents_by_id.get(m.sell_intent_id)
        if buy_intent is None or sell_intent is None:  # pragma: no cover
            continue
        other = (
            sell_intent if buy_intent.user_id == user_id else buy_intent
        )
        out.append(
            MatchView(
                match_id=m.id,
                other_intent=OtherIntentView(
                    intent_id=other.id,
                    side=other.side,
                    title=other.title,
                    description=truncate_description(other.description),
                    category=other.category,
                    reservation_price_eur=other.reservation_price_cents / 100,
                    currency=other.currency,
                ),
                similarity_score=float(m.similarity_score or 0),
                price_proximity_score=float(m.price_proximity_score or 0),
                combined_score=float(m.combined_score or 0),
                status=m.status,
                discovered_at=m.created_at,
                has_negotiation=m.id in nego_match_ids,
            )
        )
    return out


# ---------------------------------------------------------------------------
# Negotiations
# ---------------------------------------------------------------------------


async def _build_negotiations_view(
    db: AsyncSession, *, user_id: str, agent_id: str
) -> list[NegotiationView]:
    """Active negotiations whose matches the user is party to."""
    user_intent_ids = (
        select(Intent.id).where(Intent.user_id == user_id)
    ).scalar_subquery()

    rows = list(
        await db.scalars(
            select(Negotiation)
            .join(Match, Match.id == Negotiation.match_id)
            .where(
                or_(
                    Match.buy_intent_id.in_(user_intent_ids),
                    Match.sell_intent_id.in_(user_intent_ids),
                )
            )
            .where(Negotiation.status == "active")
            .order_by(Negotiation.started_at.desc())
        )
    )
    if not rows:
        return []

    match_ids = [n.match_id for n in rows]
    matches = list(
        await db.scalars(select(Match).where(Match.id.in_(match_ids)))
    )
    matches_by_id: dict[str, Match] = {m.id: m for m in matches}
    intent_ids: set[str] = set()
    for m in matches:
        intent_ids.add(m.buy_intent_id)
        intent_ids.add(m.sell_intent_id)
    intents = list(
        await db.scalars(select(Intent).where(Intent.id.in_(intent_ids)))
    )
    intents_by_id: dict[str, Intent] = {i.id: i for i in intents}

    out: list[NegotiationView] = []
    for nego in rows:
        match = matches_by_id.get(nego.match_id)
        if match is None:  # pragma: no cover
            continue
        buy_intent = intents_by_id.get(match.buy_intent_id)
        sell_intent = intents_by_id.get(match.sell_intent_id)
        if buy_intent is None or sell_intent is None:  # pragma: no cover
            continue
        other = (
            sell_intent if buy_intent.user_id == user_id else buy_intent
        )

        turns = (nego.state or {}).get("turns") or []
        last_turn = turns[-1] if turns else None
        last_offer_view: OfferView | None = None
        awaiting_my_response = False
        if last_turn:
            last_offer_view = OfferView(
                turn_number=last_turn.get("turn_number") or 0,
                from_agent_id=last_turn.get("agent_id") or "",
                is_from_me=last_turn.get("agent_id") == agent_id,
                type=last_turn.get("type") or "",
                price_cents=int(last_turn.get("price_cents") or 0),
                message=last_turn.get("message") or "",
                timestamp=last_turn.get("timestamp") or "",
            )
            awaiting_my_response = (
                last_turn.get("agent_id") != agent_id
                and last_turn.get("type") in ("offer", "counter_offer")
            )

        out.append(
            NegotiationView(
                negotiation_id=nego.id,
                match_id=nego.match_id,
                other_intent_summary=OtherIntentView(
                    intent_id=other.id,
                    side=other.side,
                    title=other.title,
                    description=truncate_description(other.description),
                    category=other.category,
                    reservation_price_eur=other.reservation_price_cents / 100,
                    currency=other.currency,
                ),
                status=nego.status,
                rounds_used=nego.rounds_used or 0,
                max_rounds=nego.max_rounds or 6,
                is_final_round=bool((nego.state or {}).get("is_final_round")),
                last_offer=last_offer_view,
                awaiting_my_response=awaiting_my_response,
                started_at=nego.started_at,
            )
        )
    return out


# ---------------------------------------------------------------------------
# Deals
# ---------------------------------------------------------------------------


async def _build_pending_deals_view(
    db: AsyncSession, *, user_id: str
) -> list[DealView]:
    rows = list(
        await db.scalars(
            select(Deal)
            .where(
                or_(
                    Deal.buyer_user_id == user_id,
                    Deal.seller_user_id == user_id,
                )
            )
            .where(Deal.status == "pending_signatures")
            .order_by(Deal.created_at.desc())
        )
    )
    return [_deal_view(d, user_id=user_id) for d in rows]


def _deal_view(deal: Deal, *, user_id: str) -> DealView:
    is_buyer = user_id == deal.buyer_user_id
    return DealView(
        deal_id=deal.id,
        negotiation_id=deal.negotiation_id,
        agreed_price_cents=deal.agreed_price_cents,
        currency=deal.currency,
        status=deal.status,
        my_role="buyer" if is_buyer else "seller",
        i_have_signed=(
            deal.buyer_signature is not None
            if is_buyer
            else deal.seller_signature is not None
        ),
        other_has_signed=(
            deal.seller_signature is not None
            if is_buyer
            else deal.buyer_signature is not None
        ),
        created_at=deal.created_at,
        expires_at=deal.expires_at,
        minutes_until_expiry=minutes_until(deal.expires_at),
    )


# ---------------------------------------------------------------------------
# Step-ups (pending only — resolved ones are in inbox)
# ---------------------------------------------------------------------------


async def _build_pending_step_ups_view(
    db: AsyncSession, *, agent_id: str
) -> list[StepUpView]:
    rows = list(
        await db.scalars(
            select(StepUpRequest)
            .where(StepUpRequest.agent_id == agent_id)
            .where(StepUpRequest.status == "pending")
            .order_by(StepUpRequest.created_at.desc())
        )
    )
    return [
        StepUpView(
            step_up_id=r.id,
            action=r.action,
            action_params=r.action_params or {},
            reason=r.reason,
            expires_at=r.expires_at,
            minutes_until_expiry=minutes_until(r.expires_at),
            status=r.status,
        )
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Misc helpers
# ---------------------------------------------------------------------------


def _nullifier_pseudonym(nullifier_hash: str | None) -> str | None:
    """First 12 chars of the hash — enough for log correlation, no PII risk."""
    if not nullifier_hash:
        return None
    return nullifier_hash[:12]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _has_pending_work(
    *,
    active_negotiations: list[NegotiationView],
    pending_deals: list[DealView],
    inbox,
) -> bool:
    """Heuristic: would the orchestrator do anything this tick?

    True if any of:
      - a negotiation is awaiting my response,
      - a deal awaits my signature,
      - the inbox has any new offers / counter-offers / signed-by-other.
    """
    if any(n.awaiting_my_response for n in active_negotiations):
        return True
    if any(not d.i_have_signed for d in pending_deals):
        return True
    if (
        inbox.new_offers_received
        or inbox.counter_offers_received
        or inbox.other_party_signed_recently
    ):
        return True
    return False
