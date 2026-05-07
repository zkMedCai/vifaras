"""Capital position skeleton for Autonomous Capital Mandate V0."""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.schema import CapitalMandate, CapitalPosition, Deal, Intent


@dataclass
class ExpectedPnl:
    expected_profit_cents: int | None
    expected_margin_bps: int | None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def compute_expected_pnl(
    *, purchase_price_cents: int, expected_resale_price_cents: int | None
) -> ExpectedPnl:
    if expected_resale_price_cents is None or purchase_price_cents <= 0:
        return ExpectedPnl(expected_profit_cents=None, expected_margin_bps=None)
    profit = int(expected_resale_price_cents) - int(purchase_price_cents)
    margin_bps = int(round((profit / int(purchase_price_cents)) * 10_000))
    return ExpectedPnl(expected_profit_cents=profit, expected_margin_bps=margin_bps)


async def create_position_from_opportunity(
    db: AsyncSession,
    *,
    capital_mandate_id: str,
    item_snapshot: dict[str, Any],
    purchase_price_cents: int | None = None,
    expected_resale_price_cents: int | None = None,
    status: str = "opportunity_found",
) -> CapitalPosition:
    mandate = await db.get(CapitalMandate, capital_mandate_id)
    if mandate is None:
        raise ValueError(f"capital mandate {capital_mandate_id!r} not found")

    pnl = (
        compute_expected_pnl(
            purchase_price_cents=purchase_price_cents,
            expected_resale_price_cents=expected_resale_price_cents,
        )
        if purchase_price_cents is not None
        else ExpectedPnl(None, None)
    )
    position = CapitalPosition(
        id=str(uuid.uuid4()),
        capital_mandate_id=mandate.id,
        user_id=mandate.user_id,
        agent_id=mandate.agent_id,
        item_snapshot=item_snapshot,
        status=status,
        purchase_price_cents=purchase_price_cents,
        expected_resale_price_cents=expected_resale_price_cents,
        expected_profit_cents=pnl.expected_profit_cents,
        expected_margin_bps=pnl.expected_margin_bps,
        created_at=_utcnow(),
    )
    db.add(position)
    await db.flush()
    return position


async def create_position_from_buy_deal(
    db: AsyncSession,
    *,
    capital_mandate_id: str,
    deal_id: str,
    expected_resale_price_cents: int | None = None,
    category: str | None = None,
) -> CapitalPosition:
    existing = await db.scalar(
        select(CapitalPosition).where(CapitalPosition.source_buy_deal_id == deal_id)
    )
    if existing is not None:
        return existing

    mandate = await db.get(CapitalMandate, capital_mandate_id)
    deal = await db.get(Deal, deal_id)
    if mandate is None:
        raise ValueError(f"capital mandate {capital_mandate_id!r} not found")
    if deal is None:
        raise ValueError(f"deal {deal_id!r} not found")

    sell_intent = await db.get(Intent, deal.sell_intent_id)
    buy_intent = await db.get(Intent, deal.buy_intent_id)
    item_snapshot = {
        "deal_id": deal.id,
        "buy_intent_id": deal.buy_intent_id,
        "sell_intent_id": deal.sell_intent_id,
        "title": sell_intent.title if sell_intent is not None else None,
        "description": sell_intent.description if sell_intent is not None else None,
        "category": category
        or (sell_intent.category if sell_intent is not None else None)
        or (buy_intent.category if buy_intent is not None else None),
    }
    pnl = compute_expected_pnl(
        purchase_price_cents=int(deal.agreed_price_cents),
        expected_resale_price_cents=expected_resale_price_cents,
    )
    position = CapitalPosition(
        id=str(uuid.uuid4()),
        capital_mandate_id=mandate.id,
        user_id=mandate.user_id,
        agent_id=mandate.agent_id,
        source_buy_deal_id=deal.id,
        item_snapshot=item_snapshot,
        status="purchase_authorized",
        purchase_price_cents=int(deal.agreed_price_cents),
        expected_resale_price_cents=expected_resale_price_cents,
        expected_profit_cents=pnl.expected_profit_cents,
        expected_margin_bps=pnl.expected_margin_bps,
        created_at=_utcnow(),
    )
    db.add(position)
    await db.flush()
    return position


async def mark_position_in_inventory(
    db: AsyncSession, *, position_id: str
) -> CapitalPosition:
    position = await db.get(CapitalPosition, position_id)
    if position is None:
        raise ValueError(f"position {position_id!r} not found")
    position.status = "in_inventory"
    position.updated_at = _utcnow()
    await db.flush()
    return position


async def mark_position_listed_for_resale(
    db: AsyncSession, *, position_id: str
) -> CapitalPosition:
    position = await db.get(CapitalPosition, position_id)
    if position is None:
        raise ValueError(f"position {position_id!r} not found")
    position.status = "listed_for_resale"
    position.updated_at = _utcnow()
    await db.flush()
    return position


async def mark_position_sold(
    db: AsyncSession,
    *,
    position_id: str,
    realized_sale_price_cents: int,
) -> CapitalPosition:
    position = await db.get(CapitalPosition, position_id)
    if position is None:
        raise ValueError(f"position {position_id!r} not found")
    position.status = "sold"
    position.realized_sale_price_cents = realized_sale_price_cents
    if position.purchase_price_cents is not None:
        position.realized_profit_cents = (
            int(realized_sale_price_cents) - int(position.purchase_price_cents)
        )
    position.updated_at = _utcnow()
    position.closed_at = _utcnow()
    await db.flush()
    return position
