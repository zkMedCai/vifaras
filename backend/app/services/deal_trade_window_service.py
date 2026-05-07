"""Trade Window workflow for confirmed deals (FASE 10.1.4.x).

V0 does not manage escrow, payment, shipping labels, or carrier APIs yet.
The Trade Window exposes a small durable logistics state machine after
both passkey signatures land. It gives the frontend a controlled place
for shipping/delivery/completion instead of turning post-deal chat into
an unbounded workflow container.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.schema import Deal, Intent
from app.services import audit_service, deal_service

TRADE_WINDOW_STATUS_OPEN: Final[str] = "trade_window_open"
TRADE_WINDOW_STATUS_COMPLETED: Final[str] = "trade_window_completed"
SHIPPING_STATUS_PENDING: Final[str] = "shipping_pending"
SHIPPING_STATUS_SHIPPED: Final[str] = "shipped"
SHIPPING_STATUS_DELIVERED: Final[str] = "delivered"
SHIPPING_STATUS_COMPLETED: Final[str] = "completed"

ACTION_MARK_SHIPPED: Final[str] = "mark_shipped"
ACTION_MARK_DELIVERED: Final[str] = "mark_delivered"
ACTION_MARK_COMPLETED: Final[str] = "mark_completed"

POST_CONFIRM_DEAL_STATUSES: Final[set[str]] = {"confirmed", "completed"}


class TradeWindowActionForbidden(deal_service.DealError):
    code = "trade_window_action_forbidden"
    http_status = 403


class InvalidTradeWindowTransition(deal_service.DealError):
    code = "invalid_trade_window_transition"
    http_status = 409


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
    tracking_reference: str | None
    shipped_at: datetime | None
    delivered_at: datetime | None
    completed_at: datetime | None


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _role_for_user(*, deal: Deal, user_id: str) -> str:
    if deal.buyer_user_id == user_id:
        return "buyer"
    if deal.seller_user_id == user_id:
        return "seller"
    raise deal_service.NotPartyToDeal(
        f"user {user_id!r} is not a party to deal {deal.id!r}"
    )


def _shipping_status(deal: Deal) -> str:
    return deal.shipping_status or SHIPPING_STATUS_PENDING


def _trade_window_status(deal: Deal) -> str:
    if deal.status == "completed" or _shipping_status(deal) == SHIPPING_STATUS_COMPLETED:
        return TRADE_WINDOW_STATUS_COMPLETED
    return TRADE_WINDOW_STATUS_OPEN


def _next_required_action(*, deal: Deal, user_id: str) -> str:
    status = _shipping_status(deal)
    role = _role_for_user(deal=deal, user_id=user_id)
    if status == SHIPPING_STATUS_PENDING:
        return (
            "seller_prepare_shipping"
            if role == "seller"
            else "wait_for_seller_shipping"
        )
    if status == SHIPPING_STATUS_SHIPPED:
        return (
            "buyer_confirm_delivery"
            if role == "buyer"
            else "wait_for_buyer_delivery"
        )
    if status == SHIPPING_STATUS_DELIVERED:
        return "complete_trade"
    if status == SHIPPING_STATUS_COMPLETED:
        return "trade_completed"
    return "review_trade_window"


def _ensure_trade_window_visible(deal: Deal) -> None:
    if deal.status not in POST_CONFIRM_DEAL_STATUSES:
        raise deal_service.DealNotConfirmed(
            f"deal {deal.id!r} is in status {deal.status!r}; trade window "
            f"opens only after both signatures land"
        )
    if deal.confirmed_at is None:
        raise deal_service.DealNotConfirmed(
            f"deal {deal.id!r} has no confirmed_at timestamp"
        )


def _ensure_trade_window_mutable(deal: Deal) -> None:
    _ensure_trade_window_visible(deal)
    if deal.status == "completed" or _shipping_status(deal) == SHIPPING_STATUS_COMPLETED:
        raise InvalidTradeWindowTransition(
            f"deal {deal.id!r} trade window is already completed"
        )


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


async def _state_from_deal(
    db: AsyncSession,
    *,
    deal: Deal,
    user_id: str,
) -> TradeWindowState:
    buy_intent = await db.get(Intent, deal.buy_intent_id)
    sell_intent = await db.get(Intent, deal.sell_intent_id)

    return TradeWindowState(
        deal_id=deal.id,
        status=_trade_window_status(deal),
        buyer_user_id=deal.buyer_user_id,
        seller_user_id=deal.seller_user_id,
        terms_summary=_build_terms_summary(
            deal=deal, buy_intent=buy_intent, sell_intent=sell_intent
        ),
        confirmed_at=deal.confirmed_at,
        expires_at=None,
        shipping_status=_shipping_status(deal),
        next_required_action=_next_required_action(deal=deal, user_id=user_id),
        tracking_reference=deal.tracking_reference,
        shipped_at=deal.shipped_at,
        delivered_at=deal.delivered_at,
        completed_at=deal.completed_at,
    )


async def get_trade_window_for_user(
    db: AsyncSession,
    *,
    user_id: str,
    deal_id: str,
) -> TradeWindowState:
    """Return the Trade Window read model for a post-confirm deal."""
    deal = await deal_service.get_deal_for_user(
        db, user_id=user_id, deal_id=deal_id
    )
    _ensure_trade_window_visible(deal)
    return await _state_from_deal(db, deal=deal, user_id=user_id)


async def apply_trade_window_action(
    db: AsyncSession,
    *,
    user_id: str,
    deal_id: str,
    action: str,
    tracking_reference: str | None = None,
) -> TradeWindowState:
    """Apply a V0 Trade Window action and return the updated read model."""
    deal = await db.scalar(
        select(Deal).where(Deal.id == deal_id).with_for_update()
    )
    if deal is None:
        raise deal_service.DealNotFound(f"deal {deal_id!r} not found")

    role = _role_for_user(deal=deal, user_id=user_id)
    _ensure_trade_window_mutable(deal)

    current = _shipping_status(deal)
    now = _utcnow()

    if action == ACTION_MARK_SHIPPED:
        if role != "seller":
            raise TradeWindowActionForbidden(
                "only the seller can mark the deal as shipped"
            )
        if current != SHIPPING_STATUS_PENDING:
            raise InvalidTradeWindowTransition(
                f"cannot mark shipped from shipping_status={current!r}"
            )
        deal.shipping_status = SHIPPING_STATUS_SHIPPED
        deal.shipped_at = now
        tracking = tracking_reference.strip() if tracking_reference else ""
        deal.tracking_reference = tracking or None
        audit_action = audit_service.DealActions.TRADE_SHIPPING_MARKED
        audit_result = {
            "shipping_status": SHIPPING_STATUS_SHIPPED,
            "tracking_reference_present": bool(deal.tracking_reference),
        }
    elif action == ACTION_MARK_DELIVERED:
        if role != "buyer":
            raise TradeWindowActionForbidden(
                "only the buyer can mark the deal as delivered"
            )
        if current != SHIPPING_STATUS_SHIPPED:
            raise InvalidTradeWindowTransition(
                f"cannot mark delivered from shipping_status={current!r}"
            )
        deal.shipping_status = SHIPPING_STATUS_DELIVERED
        deal.delivered_at = now
        audit_action = audit_service.DealActions.TRADE_DELIVERED
        audit_result = {"shipping_status": SHIPPING_STATUS_DELIVERED}
    elif action == ACTION_MARK_COMPLETED:
        if current != SHIPPING_STATUS_DELIVERED:
            raise InvalidTradeWindowTransition(
                f"cannot complete trade from shipping_status={current!r}"
            )
        deal.shipping_status = SHIPPING_STATUS_COMPLETED
        deal.completed_at = now
        deal.status = "completed"
        audit_action = audit_service.DealActions.TRADE_COMPLETED
        audit_result = {
            "deal_status": "completed",
            "shipping_status": SHIPPING_STATUS_COMPLETED,
        }
    else:
        raise InvalidTradeWindowTransition(f"unknown trade window action {action!r}")

    await audit_service.log_intent_event(
        db,
        user_id=user_id,
        action=audit_action,
        params={"deal_id": deal.id, "role": role, "action": action},
        result=audit_result,
        success=True,
    )
    await db.commit()
    await db.refresh(deal)

    return await _state_from_deal(db, deal=deal, user_id=user_id)
