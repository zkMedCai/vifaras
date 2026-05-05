"""Trade Window skeleton for confirmed deals (FASE 10.1.4.2).

V0 does not manage escrow, payment, shipping labels, or delivery
confirmation yet. The Trade Window is a derived read model exposed after
both passkey signatures land. It gives the frontend a controlled place
for logistics steps instead of turning post-deal chat into an unbounded
workflow container.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.schema import Intent
from app.services import deal_service


TRADE_WINDOW_STATUS_OPEN: Final[str] = "trade_window_open"
SHIPPING_STATUS_PENDING: Final[str] = "shipping_pending"


@dataclass
class TradeWindowState:
    deal_id: str
    status: str
    buyer_user_id: str
    seller_user_id: str
    terms_summary: dict[str, Any]
    confirmed_at: datetime
    expires_at: datetime | None
    shipping_status: str
    next_required_action: str


def _pick_delivery(*, buy_intent: Intent | None, sell_intent: Intent | None) -> Any:
    for intent in (sell_intent, buy_intent):
        constraints = intent.hard_constraints if intent is not None else None
        if isinstance(constraints, dict):
            delivery = constraints.get("delivery") or constraints.get("shipping")
            if delivery:
                return delivery
    return None


def _build_terms_summary(
    *,
    deal,
    buy_intent: Intent | None,
    sell_intent: Intent | None,
) -> dict[str, Any]:
    return {
        "agreed_price_cents": deal.agreed_price_cents,
        "currency": deal.currency,
        "buy_intent_id": deal.buy_intent_id,
        "sell_intent_id": deal.sell_intent_id,
        "buy_intent_title": buy_intent.title if buy_intent is not None else None,
        "sell_intent_title": sell_intent.title if sell_intent is not None else None,
        "category": (
            sell_intent.category
            if sell_intent is not None
            else buy_intent.category if buy_intent is not None else None
        ),
        "delivery": _pick_delivery(buy_intent=buy_intent, sell_intent=sell_intent),
    }


async def get_trade_window_for_user(
    db: AsyncSession,
    *,
    user_id: str,
    deal_id: str,
) -> TradeWindowState:
    """Return the derived Trade Window read model for a confirmed deal."""
    deal = await deal_service.get_deal_for_user(
        db, user_id=user_id, deal_id=deal_id
    )
    if deal.status != "confirmed":
        raise deal_service.DealNotConfirmed(
            f"deal {deal.id!r} is in status {deal.status!r}; trade window "
            f"opens only after both signatures land"
        )
    if deal.confirmed_at is None:
        raise deal_service.DealNotConfirmed(
            f"deal {deal.id!r} has no confirmed_at timestamp"
        )

    buy_intent = await db.get(Intent, deal.buy_intent_id)
    sell_intent = await db.get(Intent, deal.sell_intent_id)
    next_required_action = (
        "seller_prepare_shipping"
        if user_id == deal.seller_user_id
        else "wait_for_seller_shipping"
    )

    return TradeWindowState(
        deal_id=deal.id,
        status=TRADE_WINDOW_STATUS_OPEN,
        buyer_user_id=deal.buyer_user_id,
        seller_user_id=deal.seller_user_id,
        terms_summary=_build_terms_summary(
            deal=deal, buy_intent=buy_intent, sell_intent=sell_intent
        ),
        confirmed_at=deal.confirmed_at,
        expires_at=None,
        shipping_status=SHIPPING_STATUS_PENDING,
        next_required_action=next_required_action,
    )
