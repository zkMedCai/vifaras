"""Operational ledger for Autonomous Capital Mandate V0.

Append-only by design. V0 does not move real money; ledger entries model
budget reservation/commitment so policy can reason about available budget.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.schema import CapitalLedgerEntry, CapitalMandate

BUDGET_RESERVED: Final[str] = "budget_reserved"
BUDGET_RELEASED: Final[str] = "budget_released"
BUDGET_COMMITTED: Final[str] = "budget_committed"
PURCHASE_RECORDED: Final[str] = "purchase_recorded"
SALE_RECORDED: Final[str] = "sale_recorded"
PNL_REALIZED: Final[str] = "pnl_realized"
FEE_RECORDED: Final[str] = "fee_recorded"
LOSS_RECORDED: Final[str] = "loss_recorded"


class CapitalLedgerError(Exception):
    code: str = "capital_ledger_error"
    http_status: int = 400


class CapitalMandateLedgerNotFound(CapitalLedgerError):
    code = "capital_mandate_not_found"
    http_status = 404


@dataclass
class BudgetState:
    budget_total_cents: int
    reserved_cents: int
    committed_cents: int
    available_cents: int
    realized_pnl_cents: int

    def to_dict(self) -> dict[str, int]:
        return {
            "budget_total_cents": self.budget_total_cents,
            "reserved_cents": self.reserved_cents,
            "committed_cents": self.committed_cents,
            "available_cents": self.available_cents,
            "realized_pnl_cents": self.realized_pnl_cents,
        }


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def compute_budget_state(
    db: AsyncSession, *, capital_mandate_id: str
) -> BudgetState:
    mandate = await db.get(CapitalMandate, capital_mandate_id)
    if mandate is None:
        raise CapitalMandateLedgerNotFound(capital_mandate_id)

    rows = list(
        await db.scalars(
            select(CapitalLedgerEntry).where(
                CapitalLedgerEntry.capital_mandate_id == capital_mandate_id
            )
        )
    )
    reserved = 0
    committed = 0
    pnl = 0
    for row in rows:
        amount = int(row.amount_cents)
        if row.type == BUDGET_RESERVED:
            reserved += amount
        elif row.type == BUDGET_RELEASED:
            reserved -= amount
        elif row.type == BUDGET_COMMITTED:
            reserved -= amount
            committed += amount
        elif row.type == PURCHASE_RECORDED:
            committed += amount
        elif row.type == SALE_RECORDED:
            committed -= amount
        elif row.type == PNL_REALIZED:
            pnl += amount
        elif row.type in (FEE_RECORDED, LOSS_RECORDED):
            pnl -= amount

    reserved = max(0, reserved)
    committed = max(0, committed)
    available = int(mandate.budget_total_cents) - reserved - committed
    return BudgetState(
        budget_total_cents=int(mandate.budget_total_cents),
        reserved_cents=reserved,
        committed_cents=committed,
        available_cents=max(0, available),
        realized_pnl_cents=pnl,
    )


async def _insert_or_get_entry(
    db: AsyncSession,
    *,
    capital_mandate_id: str,
    user_id: str,
    agent_id: str,
    entry_type: str,
    amount_cents: int,
    currency: str,
    reason: str,
    idempotency_key: str,
    deal_id: str | None = None,
    position_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> CapitalLedgerEntry:
    existing = await db.scalar(
        select(CapitalLedgerEntry).where(
            CapitalLedgerEntry.idempotency_key == idempotency_key
        )
    )
    if existing is not None:
        return existing

    entry = CapitalLedgerEntry(
        id=str(uuid.uuid4()),
        capital_mandate_id=capital_mandate_id,
        user_id=user_id,
        agent_id=agent_id,
        deal_id=deal_id,
        position_id=position_id,
        type=entry_type,
        amount_cents=int(amount_cents),
        currency=currency,
        reason=reason,
        entry_metadata=metadata or {},
        idempotency_key=idempotency_key,
        created_at=_utcnow(),
    )
    db.add(entry)
    await db.flush()
    return entry


async def reserve_budget(
    db: AsyncSession,
    *,
    capital_mandate_id: str,
    amount_cents: int,
    deal_id: str | None,
    idempotency_key: str,
    reason: str = "auto_buy_budget_reservation",
    metadata: dict[str, Any] | None = None,
) -> CapitalLedgerEntry:
    mandate = await db.get(CapitalMandate, capital_mandate_id)
    if mandate is None:
        raise CapitalMandateLedgerNotFound(capital_mandate_id)
    return await _insert_or_get_entry(
        db,
        capital_mandate_id=mandate.id,
        user_id=mandate.user_id,
        agent_id=mandate.agent_id,
        entry_type=BUDGET_RESERVED,
        amount_cents=amount_cents,
        currency=mandate.currency,
        reason=reason,
        idempotency_key=idempotency_key,
        deal_id=deal_id,
        metadata=metadata,
    )


async def release_budget(
    db: AsyncSession,
    *,
    capital_mandate_id: str,
    amount_cents: int,
    deal_id: str | None,
    idempotency_key: str,
    reason: str = "budget_released",
    metadata: dict[str, Any] | None = None,
) -> CapitalLedgerEntry:
    mandate = await db.get(CapitalMandate, capital_mandate_id)
    if mandate is None:
        raise CapitalMandateLedgerNotFound(capital_mandate_id)
    return await _insert_or_get_entry(
        db,
        capital_mandate_id=mandate.id,
        user_id=mandate.user_id,
        agent_id=mandate.agent_id,
        entry_type=BUDGET_RELEASED,
        amount_cents=amount_cents,
        currency=mandate.currency,
        reason=reason,
        idempotency_key=idempotency_key,
        deal_id=deal_id,
        metadata=metadata,
    )


async def commit_budget(
    db: AsyncSession,
    *,
    capital_mandate_id: str,
    amount_cents: int,
    deal_id: str | None,
    position_id: str | None,
    idempotency_key: str,
    reason: str = "budget_committed",
    metadata: dict[str, Any] | None = None,
) -> CapitalLedgerEntry:
    mandate = await db.get(CapitalMandate, capital_mandate_id)
    if mandate is None:
        raise CapitalMandateLedgerNotFound(capital_mandate_id)
    return await _insert_or_get_entry(
        db,
        capital_mandate_id=mandate.id,
        user_id=mandate.user_id,
        agent_id=mandate.agent_id,
        entry_type=BUDGET_COMMITTED,
        amount_cents=amount_cents,
        currency=mandate.currency,
        reason=reason,
        idempotency_key=idempotency_key,
        deal_id=deal_id,
        position_id=position_id,
        metadata=metadata,
    )


async def record_sale(
    db: AsyncSession,
    *,
    capital_mandate_id: str,
    amount_cents: int,
    deal_id: str | None,
    position_id: str | None,
    idempotency_key: str,
    reason: str = "sale_recorded",
    metadata: dict[str, Any] | None = None,
) -> CapitalLedgerEntry:
    mandate = await db.get(CapitalMandate, capital_mandate_id)
    if mandate is None:
        raise CapitalMandateLedgerNotFound(capital_mandate_id)
    return await _insert_or_get_entry(
        db,
        capital_mandate_id=mandate.id,
        user_id=mandate.user_id,
        agent_id=mandate.agent_id,
        entry_type=SALE_RECORDED,
        amount_cents=amount_cents,
        currency=mandate.currency,
        reason=reason,
        idempotency_key=idempotency_key,
        deal_id=deal_id,
        position_id=position_id,
        metadata=metadata,
    )
