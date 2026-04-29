"""View models for agent state reload (brief task 6.2).

These are deliberately separate from `schema.py`: that file owns the
SQLAlchemy ORM (persistence shape), this one owns the Pydantic v2
(serialization shape Claude reads). Keeping them apart prevents the ORM
shape from leaking into the prompt — privacy invariants like
"counterparty's `ideal_price_eur` is never exposed" (DQ-31) are enforced
here, in the view-builder layer.

All view models are JSON-friendly so `model_dump()` produces a payload
the orchestrator can pass to Claude's tool_use input. Verbose field
names + computed helpers (`days_until_expiry`, `awaiting_my_response`)
keep the prompt short — Claude doesn't have to derive what we already
know.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


# Truncation budget for free-text fields embedded in prompt context.
# 300 chars ≈ 75 tokens — small enough to keep prompts fast, large enough
# to convey the gist of an intent description.
DESCRIPTION_TRUNCATE_CHARS = 300


def _truncate(text: str | None, *, limit: int = DESCRIPTION_TRUNCATE_CHARS) -> str:
    if not text:
        return ""
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


# ---------------------------------------------------------------------------
# Identity / mandate / limits
# ---------------------------------------------------------------------------


class MandateView(BaseModel):
    mandate_id: str
    issued_at: datetime
    expires_at: datetime
    days_until_expiry: int

    allowed_actions: list[str]
    forbidden_actions: list[str]
    limits: dict[str, Any]
    step_up_required_for: list[dict[str, Any]]
    constraints: dict[str, Any]


class LimitsRemaining(BaseModel):
    """Effective remaining budget on the active mandate.

    Computed from `mandate.limits` minus the running counters
    (`spent_today_eur`, `spent_total_eur`, `deals_count`). Daily counters
    are reset by `mandate_verifier` lazily on read, but for the view we
    surface the *post-reset* values to keep the prompt accurate.
    """

    daily_volume_remaining_cents: int
    mandate_total_volume_remaining_cents: int
    deals_remaining_today: int

    daily_reset_at: datetime  # next UTC midnight

    is_at_daily_cap: bool
    is_near_mandate_cap: bool  # > 80% of mandate-lifetime cap


# ---------------------------------------------------------------------------
# Marketplace state (intents, matches, negotiations, deals)
# ---------------------------------------------------------------------------


class IntentView(BaseModel):
    intent_id: str
    side: str
    title: str
    description: str  # truncated
    category: str

    reservation_price_eur: float
    ideal_price_eur: float
    currency: str

    status: str
    expires_at: datetime
    days_until_expiry: int

    match_count_active: int
    has_active_negotiation: bool


class OtherIntentView(BaseModel):
    """Privacy-aware view of the counterparty's intent (DQ-31).

    Surfaces `reservation_price_eur` (already implicit from price-overlap)
    but NOT `ideal_price_eur` (the strategic target stays private).
    `description` is truncated to keep prompt size predictable.
    """

    intent_id: str
    side: str
    title: str
    description: str  # truncated
    category: str
    reservation_price_eur: float
    currency: str


class MatchView(BaseModel):
    match_id: str
    other_intent: OtherIntentView

    similarity_score: float
    price_proximity_score: float
    combined_score: float

    status: str
    discovered_at: datetime

    has_negotiation: bool


class OfferView(BaseModel):
    """Single turn within a negotiation. Mirrors the JSONB turn shape."""

    turn_number: int
    from_agent_id: str
    is_from_me: bool
    type: str  # 'offer' | 'counter_offer' | 'accept' | 'reject'
    price_cents: int
    message: str
    timestamp: str


class NegotiationView(BaseModel):
    negotiation_id: str
    match_id: str
    other_intent_summary: OtherIntentView

    status: str
    rounds_used: int
    max_rounds: int
    is_final_round: bool

    last_offer: OfferView | None
    awaiting_my_response: bool
    started_at: datetime


class DealView(BaseModel):
    deal_id: str
    negotiation_id: str
    agreed_price_cents: int
    currency: str

    status: str
    my_role: str  # 'buyer' | 'seller'
    i_have_signed: bool
    other_has_signed: bool

    created_at: datetime
    expires_at: datetime
    minutes_until_expiry: int


class StepUpView(BaseModel):
    step_up_id: str
    action: str
    action_params: dict[str, Any]
    reason: str
    expires_at: datetime
    minutes_until_expiry: int
    status: str  # 'pending' | 'approved' | 'rejected'


# ---------------------------------------------------------------------------
# Inbox (delta since last tick)
# ---------------------------------------------------------------------------


class AgentInbox(BaseModel):
    """Events relevant to the agent since `since` (the agent's
    `last_tick_at` or epoch-0 on first tick).
    """

    new_offers_received: list[OfferView] = Field(default_factory=list)
    counter_offers_received: list[OfferView] = Field(default_factory=list)
    deals_awaiting_my_signature: list[DealView] = Field(default_factory=list)
    other_party_signed_recently: list[DealView] = Field(default_factory=list)
    approved_step_ups: list[StepUpView] = Field(default_factory=list)
    rejected_step_ups: list[StepUpView] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Top-level full state
# ---------------------------------------------------------------------------


class AgentFullState(BaseModel):
    # Identity
    agent_id: str
    user_id: str
    agent_status: str
    nullifier_pseudonym: str | None  # truncated; None if tier-0 (no nullifier)

    # Mandate & limits (None when status != 'active')
    mandate: MandateView | None
    limits_remaining: LimitsRemaining | None

    # World
    active_intents: list[IntentView] = Field(default_factory=list)
    discovered_matches: list[MatchView] = Field(default_factory=list)
    active_negotiations: list[NegotiationView] = Field(default_factory=list)
    pending_deals: list[DealView] = Field(default_factory=list)

    # Inbox
    inbox: AgentInbox

    # Open questions
    pending_step_ups: list[StepUpView] = Field(default_factory=list)

    # Meta
    snapshot_at: datetime
    last_tick_at: datetime | None
    next_action_required: bool


# ---------------------------------------------------------------------------
# Helpers (used by the service layer)
# ---------------------------------------------------------------------------


def truncate_description(text: str | None) -> str:
    return _truncate(text)
