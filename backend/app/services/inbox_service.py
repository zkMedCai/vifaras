"""Inbox view: events the agent should react to (brief task 6.2).

Builds the `AgentInbox` view model: events relevant to a specific agent
since the cursor `since` (its previous `last_tick_at`). The orchestrator
in 6.3 reads this each tick to decide whether the agent has something to
respond to.

Categories surfaced:

  - **new_offers_received**: first-turn `offer` rows in negotiations on
    matches involving the agent's intents, when the offering agent is
    NOT this one. Bounded by `since`.
  - **counter_offers_received**: same, for `counter_offer` turn type.
  - **deals_awaiting_my_signature**: pending deals where this agent's
    role hasn't signed yet. Always included regardless of `since` —
    the agent must keep re-considering open deals every tick.
  - **other_party_signed_recently**: pending deals where the OTHER role
    has signed `> since`. Triggers "now your turn" reasoning.
  - **approved_step_ups** / **rejected_step_ups**: step-up resolutions
    since `since` — the agent needs to know which paused actions can
    now resume (or are dead).

Performance: each query uses existing indexes (FK indexes on intent_id /
deal_id + the unread index on step_up_requests). Negotiation turn
filtering is app-side — JSONB filtering by timestamp is awkward and
volumes are tiny per agent (≤ a handful of active negotiations).

Read-only — no commits, no mutations.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Final

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.datetime_helpers import minutes_until
from app.models.schema import Deal, Intent, Match, Negotiation, StepUpRequest
from app.models.views import (
    AgentInbox,
    DealView,
    OfferView,
    OtherIntentView,
    StepUpView,
    truncate_description,
)


# Earliest sentinel for "no previous tick" — anything > this is "since
# first tick", i.e. everything. Naive UTC.
_EPOCH_SENTINEL: Final[datetime] = datetime(2000, 1, 1)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _parse_iso_z(s: str) -> datetime:
    """Parse a `YYYY-MM-DDTHH:MM:SSZ` turn timestamp into a naive datetime.

    The negotiation_service writes turns with this exact format
    (see `_utc_iso_z`). Defensive on edge cases — returns _EPOCH_SENTINEL
    if parse fails so the turn still surfaces but with conservative time.
    """
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ")
    except (ValueError, TypeError):
        return _EPOCH_SENTINEL


def _other_intent_view(intent: Intent) -> OtherIntentView:
    return OtherIntentView(
        intent_id=intent.id,
        side=intent.side,
        title=intent.title,
        description=truncate_description(intent.description),
        category=intent.category,
        reservation_price_eur=intent.reservation_price_cents / 100,
        currency=intent.currency,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def get_inbox_for_agent(
    db: AsyncSession,
    *,
    agent_id: str,
    user_id: str,
    since: datetime | None = None,
) -> AgentInbox:
    """Compose the agent's inbox view.

    `user_id` is needed because the orchestrator in 6.3 will pass the
    agent's owning user — that's the side of each match/deal we identify
    with. Avoids re-loading the agent row inside this service.

    `since` is the cursor; pass the agent's previous `last_tick_at`. If
    `None`, defaults to `_EPOCH_SENTINEL` (everything is "new").
    """
    cursor = since if since is not None else _EPOCH_SENTINEL

    new_offers, counter_offers = await _query_offers_received(
        db, agent_id=agent_id, user_id=user_id, since=cursor
    )
    deals_awaiting = await _query_deals_awaiting_my_signature(
        db, user_id=user_id
    )
    other_signed = await _query_deals_other_party_signed_recently(
        db, user_id=user_id, since=cursor
    )
    approved = await _query_step_ups_resolved(
        db, agent_id=agent_id, since=cursor, status="approved"
    )
    rejected = await _query_step_ups_resolved(
        db, agent_id=agent_id, since=cursor, status="rejected"
    )

    return AgentInbox(
        new_offers_received=new_offers,
        counter_offers_received=counter_offers,
        deals_awaiting_my_signature=deals_awaiting,
        other_party_signed_recently=other_signed,
        approved_step_ups=approved,
        rejected_step_ups=rejected,
    )


# ---------------------------------------------------------------------------
# Query helpers
# ---------------------------------------------------------------------------


async def _query_offers_received(
    db: AsyncSession,
    *,
    agent_id: str,
    user_id: str,
    since: datetime,
) -> tuple[list[OfferView], list[OfferView]]:
    """Return (offers, counter_offers) received by this agent since `since`.

    "Received" = turn was authored by an agent other than this one, on a
    negotiation whose match touches an intent owned by `user_id`. We
    iterate negotiations + parse `state["turns"]` app-side: JSONB-filtered
    queries get hairy and per-agent volume is small.
    """
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
            .where(Negotiation.status.in_(("active", "agreed")))
        )
    )

    new_offers: list[OfferView] = []
    counter_offers: list[OfferView] = []
    for nego in rows:
        for turn in (nego.state or {}).get("turns") or []:
            if turn.get("agent_id") == agent_id:
                continue  # authored by us — not "received"
            if turn.get("type") not in ("offer", "counter_offer"):
                continue
            ts = _parse_iso_z(turn.get("timestamp", ""))
            if ts <= since:
                continue
            view = OfferView(
                schema_version=turn.get("schema_version"),
                turn_number=turn.get("turn_number") or 0,
                from_agent_id=turn.get("agent_id") or "",
                is_from_me=False,
                type=turn.get("type"),
                price_cents=int(turn.get("price_cents") or 0),
                message=turn.get("message") or "",
                public_message=turn.get("public_message"),
                terms_delta=turn.get("terms_delta"),
                canonical_terms_snapshot=turn.get("canonical_terms_snapshot"),
                proposal_hash=turn.get("proposal_hash"),
                accepted_proposal_hash=turn.get("accepted_proposal_hash"),
                policy_check=turn.get("policy_check"),
                timestamp=turn.get("timestamp") or "",
            )
            if turn["type"] == "offer":
                new_offers.append(view)
            else:
                counter_offers.append(view)

    return new_offers, counter_offers


async def _query_deals_awaiting_my_signature(
    db: AsyncSession, *, user_id: str
) -> list[DealView]:
    """All `pending_signatures` deals where MY signature is missing.

    Status-only filter (no `since` cursor): agents need to keep
    considering open deals every tick — they expire if both don't sign.
    """
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
        )
    )
    return [_deal_view(d, user_id=user_id) for d in rows if not _i_signed(d, user_id)]


async def _query_deals_other_party_signed_recently(
    db: AsyncSession, *, user_id: str, since: datetime
) -> list[DealView]:
    """Pending deals where the OTHER party signed `> since` and I haven't yet."""
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
        )
    )
    out: list[DealView] = []
    for d in rows:
        if _i_signed(d, user_id):
            continue
        other_signed_at = (
            d.seller_signed_at if user_id == d.buyer_user_id else d.buyer_signed_at
        )
        if other_signed_at is None or other_signed_at <= since:
            continue
        out.append(_deal_view(d, user_id=user_id))
    return out


async def _query_step_ups_resolved(
    db: AsyncSession,
    *,
    agent_id: str,
    since: datetime,
    status: str,
) -> list[StepUpView]:
    """Step-ups for this agent that resolved (approved/rejected) `> since`."""
    rows = list(
        await db.scalars(
            select(StepUpRequest)
            .where(StepUpRequest.agent_id == agent_id)
            .where(StepUpRequest.status == status)
            .where(StepUpRequest.resolved_at > since)
        )
    )
    return [_step_up_view(r) for r in rows]


# ---------------------------------------------------------------------------
# View-builders (helpers shared with agent_state_service)
# ---------------------------------------------------------------------------


def _i_signed(deal: Deal, user_id: str) -> bool:
    if user_id == deal.buyer_user_id:
        return deal.buyer_signature is not None
    return deal.seller_signature is not None


def _deal_view(deal: Deal, *, user_id: str) -> DealView:
    is_buyer = user_id == deal.buyer_user_id
    return DealView(
        deal_id=deal.id,
        negotiation_id=deal.negotiation_id,
        agreed_price_cents=deal.agreed_price_cents,
        currency=deal.currency,
        status=deal.status,
        my_role="buyer" if is_buyer else "seller",
        i_have_signed=_i_signed(deal, user_id),
        other_has_signed=(
            deal.seller_signature is not None
            if is_buyer
            else deal.buyer_signature is not None
        ),
        created_at=deal.created_at,
        expires_at=deal.expires_at,
        minutes_until_expiry=minutes_until(deal.expires_at),
    )


def _step_up_view(request: StepUpRequest) -> StepUpView:
    return StepUpView(
        step_up_id=request.id,
        action=request.action,
        action_params=request.action_params or {},
        reason=request.reason,
        expires_at=request.expires_at,
        minutes_until_expiry=minutes_until(request.expires_at),
        status=request.status,
    )
