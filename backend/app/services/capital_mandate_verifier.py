"""Verifier for Autonomous Capital Mandate actions."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.schema import CapitalMandate, CapitalPosition
from app.services import capital_ledger_service, capital_position_service


class CapitalAuthorizationError(Exception):
    code: str = "capital_authorization_error"
    http_status: int = 403


class NoActiveCapitalMandate(CapitalAuthorizationError):
    code = "no_active_capital_mandate"
    http_status = 409


class CapitalMandateNotActive(CapitalAuthorizationError):
    code = "capital_mandate_not_active"
    http_status = 409


class CapitalBudgetExceeded(CapitalAuthorizationError):
    code = "capital_budget_exceeded"
    http_status = 422


class MaxSinglePurchaseExceeded(CapitalAuthorizationError):
    code = "max_single_purchase_exceeded"
    http_status = 422


class CapitalCategoryNotAllowed(CapitalAuthorizationError):
    code = "capital_category_not_allowed"
    http_status = 422


class MinMarginNotMet(CapitalAuthorizationError):
    code = "min_margin_not_met"
    http_status = 422


class MaxOpenPositionsExceeded(CapitalAuthorizationError):
    code = "max_open_positions_exceeded"
    http_status = 422


class CapitalGeoNotAllowed(CapitalAuthorizationError):
    code = "capital_geo_not_allowed"
    http_status = 422


class CapitalLossLimitExceeded(CapitalAuthorizationError):
    code = "capital_loss_limit_exceeded"
    http_status = 422


@dataclass
class CapitalAuthorizationResult:
    allowed: bool
    capital_mandate: CapitalMandate
    budget_state: capital_ledger_service.BudgetState
    blocking_reasons: list[str]
    expected_profit_cents: int | None = None
    expected_margin_bps: int | None = None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _category_matches(pattern: str, category: str) -> bool:
    if pattern == "*":
        return True
    if category == pattern:
        return True
    return category.startswith(f"{pattern}_")


async def _active_mandate_for_agent(
    db: AsyncSession, *, agent_id: str
) -> CapitalMandate:
    mandate = await db.scalar(
        select(CapitalMandate)
        .where(CapitalMandate.agent_id == agent_id)
        .where(CapitalMandate.status.in_(["active", "paused", "revoked", "expired"]))
        .order_by(CapitalMandate.activated_at.desc())
    )
    if mandate is None:
        raise NoActiveCapitalMandate(f"agent {agent_id!r} has no capital mandate")
    return mandate


async def check_duration_active(
    db: AsyncSession, *, capital_mandate: CapitalMandate
) -> None:
    if capital_mandate.status != "active":
        raise CapitalMandateNotActive(
            f"capital mandate status is {capital_mandate.status!r}"
        )
    now = _utcnow()
    if capital_mandate.starts_at > now or capital_mandate.expires_at < now:
        capital_mandate.status = "expired"
        await db.flush()
        raise CapitalMandateNotActive("capital mandate is outside its active window")


async def check_budget_available(
    db: AsyncSession, *, capital_mandate: CapitalMandate, amount_cents: int
) -> capital_ledger_service.BudgetState:
    budget = await capital_ledger_service.compute_budget_state(
        db, capital_mandate_id=capital_mandate.id
    )
    if int(amount_cents) > budget.available_cents:
        raise CapitalBudgetExceeded(
            f"amount_cents={amount_cents} exceeds available budget "
            f"{budget.available_cents}"
        )
    return budget


def check_category_allowed(
    *, capital_mandate: CapitalMandate, category: str | None
) -> None:
    category_value = (category or "unknown").strip() or "unknown"
    forbidden = list(capital_mandate.forbidden_categories or [])
    if any(_category_matches(str(pattern), category_value) for pattern in forbidden):
        raise CapitalCategoryNotAllowed(f"category {category_value!r} is forbidden")

    allowed = list(capital_mandate.allowed_categories or [])
    if allowed and "*" not in allowed:
        if not any(_category_matches(str(pattern), category_value) for pattern in allowed):
            raise CapitalCategoryNotAllowed(
                f"category {category_value!r} is outside allowed_categories"
            )


def check_geo_allowed(
    *, capital_mandate: CapitalMandate, geo: str | None
) -> None:
    if geo is None:
        return
    geo_scope = list(capital_mandate.geo_scope or [])
    if geo_scope and geo not in geo_scope:
        raise CapitalGeoNotAllowed(f"geo {geo!r} is outside geo_scope")


def check_max_single_purchase(
    *, capital_mandate: CapitalMandate, amount_cents: int
) -> None:
    if int(amount_cents) > int(capital_mandate.max_single_purchase_cents):
        raise MaxSinglePurchaseExceeded(
            f"amount_cents={amount_cents} exceeds max_single_purchase_cents="
            f"{capital_mandate.max_single_purchase_cents}"
        )


async def check_max_open_positions(
    db: AsyncSession, *, capital_mandate: CapitalMandate
) -> None:
    rows = list(
        await db.scalars(
            select(CapitalPosition).where(
                CapitalPosition.capital_mandate_id == capital_mandate.id
            )
        )
    )
    open_count = sum(
        1 for row in rows if row.status not in {"sold", "cancelled", "closed_loss"}
    )
    if open_count >= int(capital_mandate.max_open_positions):
        raise MaxOpenPositionsExceeded(
            f"open positions {open_count} >= max_open_positions "
            f"{capital_mandate.max_open_positions}"
        )


def check_min_expected_margin(
    *,
    capital_mandate: CapitalMandate,
    purchase_price_cents: int,
    expected_resale_price_cents: int | None,
) -> tuple[int | None, int | None]:
    pnl = capital_position_service.compute_expected_pnl(
        purchase_price_cents=purchase_price_cents,
        expected_resale_price_cents=expected_resale_price_cents,
    )
    required = int(capital_mandate.min_expected_margin_bps or 0)
    if required and pnl.expected_margin_bps is None:
        raise MinMarginNotMet("expected resale price is required by min margin policy")
    if expected_resale_price_cents is not None and required:
        if pnl.expected_margin_bps is None or pnl.expected_margin_bps < required:
            raise MinMarginNotMet(
                f"expected margin {pnl.expected_margin_bps} bps is below "
                f"required {required} bps"
            )
    return pnl.expected_profit_cents, pnl.expected_margin_bps


def check_loss_limit(
    *,
    capital_mandate: CapitalMandate,
    expected_profit_cents: int | None,
) -> None:
    if capital_mandate.max_total_loss_cents is None:
        return
    if expected_profit_cents is None or expected_profit_cents >= 0:
        return
    if abs(expected_profit_cents) > int(capital_mandate.max_total_loss_cents):
        raise CapitalLossLimitExceeded("expected loss exceeds max_total_loss_cents")


async def authorize_auto_buy(
    db: AsyncSession,
    *,
    agent_id: str,
    amount_cents: int,
    category: str | None,
    expected_resale_price_cents: int | None = None,
    geo: str | None = None,
) -> CapitalAuthorizationResult:
    mandate = await _active_mandate_for_agent(db, agent_id=agent_id)
    await check_duration_active(db, capital_mandate=mandate)
    if not mandate.auto_buy or mandate.requires_manual_approval:
        raise CapitalMandateNotActive("auto_buy is disabled for this mandate")
    check_max_single_purchase(
        capital_mandate=mandate, amount_cents=int(amount_cents)
    )
    check_category_allowed(capital_mandate=mandate, category=category)
    check_geo_allowed(capital_mandate=mandate, geo=geo)
    await check_max_open_positions(db, capital_mandate=mandate)
    expected_profit, expected_margin = check_min_expected_margin(
        capital_mandate=mandate,
        purchase_price_cents=int(amount_cents),
        expected_resale_price_cents=expected_resale_price_cents,
    )
    check_loss_limit(
        capital_mandate=mandate, expected_profit_cents=expected_profit
    )
    budget = await check_budget_available(
        db, capital_mandate=mandate, amount_cents=int(amount_cents)
    )
    return CapitalAuthorizationResult(
        allowed=True,
        capital_mandate=mandate,
        budget_state=budget,
        blocking_reasons=[],
        expected_profit_cents=expected_profit,
        expected_margin_bps=expected_margin,
    )


async def authorize_auto_sell(
    db: AsyncSession,
    *,
    agent_id: str,
    category: str | None,
    geo: str | None = None,
) -> CapitalAuthorizationResult:
    mandate = await _active_mandate_for_agent(db, agent_id=agent_id)
    await check_duration_active(db, capital_mandate=mandate)
    if not mandate.auto_sell or mandate.requires_manual_approval:
        raise CapitalMandateNotActive("auto_sell is disabled for this mandate")
    check_category_allowed(capital_mandate=mandate, category=category)
    check_geo_allowed(capital_mandate=mandate, geo=geo)
    budget = await capital_ledger_service.compute_budget_state(
        db, capital_mandate_id=mandate.id
    )
    return CapitalAuthorizationResult(
        allowed=True,
        capital_mandate=mandate,
        budget_state=budget,
        blocking_reasons=[],
    )


async def evaluate_buy_opportunity(
    db: AsyncSession,
    *,
    agent_id: str,
    expected_buy_price_cents: int,
    expected_resale_price_cents: int | None,
    category: str | None,
    geo: str | None = None,
) -> dict[str, Any]:
    try:
        result = await authorize_auto_buy(
            db,
            agent_id=agent_id,
            amount_cents=expected_buy_price_cents,
            expected_resale_price_cents=expected_resale_price_cents,
            category=category,
            geo=geo,
        )
    except CapitalAuthorizationError as exc:
        profit = None
        margin = None
        if expected_resale_price_cents is not None:
            pnl = capital_position_service.compute_expected_pnl(
                purchase_price_cents=expected_buy_price_cents,
                expected_resale_price_cents=expected_resale_price_cents,
            )
            profit = pnl.expected_profit_cents
            margin = pnl.expected_margin_bps
        return {
            "expected_profit_cents": profit,
            "expected_margin_bps": margin,
            "risk_level": "medium",
            "allowed_by_capital_mandate": False,
            "blocking_reasons": [exc.code],
        }
    return {
        "expected_profit_cents": result.expected_profit_cents,
        "expected_margin_bps": result.expected_margin_bps,
        "risk_level": result.capital_mandate.risk_level,
        "allowed_by_capital_mandate": True,
        "blocking_reasons": [],
        "available_budget_cents": result.budget_state.available_cents,
    }
