"""Deterministic V0 shipping policy for Trade Window.

The policy is intentionally hard-coded and fake-priced. It gives the
Trade Window structured shipping choices before any payment, escrow,
carrier, or tracking integration exists.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any, Final, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.schema import Deal, DealShippingSelection, Intent
from app.services import audit_service, deal_service

PAID_BY_VALUES: Final[set[str]] = {"buyer", "seller", "included"}

SMALL_CATEGORY_TOKENS: Final[tuple[str, ...]] = (
    "pokemon",
    "card",
    "cards",
    "collectible",
    "collectibles",
)
ELECTRONICS_CATEGORY_TOKENS: Final[tuple[str, ...]] = (
    "electronics",
    "gaming",
)
FRAGILE_CATEGORY_TOKENS: Final[tuple[str, ...]] = (
    "fragile",
    "home",
    "art",
)


class ShippingMethodNotAllowed(deal_service.DealError):
    code = "shipping_method_not_allowed"
    http_status = 422


class ShippingMethodSelectionLocked(deal_service.DealError):
    code = "shipping_method_selection_locked"
    http_status = 409


@dataclass(frozen=True)
class ShippingOption:
    code: str
    label: str
    description: str
    price_cents: int
    currency: str
    tracking_required: bool
    insurance_available: bool
    insurance_required: bool
    recommended: bool
    allowed: bool
    disabled_reason: str | None
    risk_level: Literal["low", "medium", "high"]


@dataclass(frozen=True)
class SelectedShippingMethod:
    method_code: str
    method_label: str
    method_description: str
    price_cents: int
    currency: str
    paid_by: str
    tracking_required: bool
    insurance_available: bool
    insurance_required: bool
    recommended: bool
    risk_level: str
    selected_by_user_id: str
    selected_at: datetime


@dataclass(frozen=True)
class ShippingOptionsState:
    deal_id: str
    agreed_price_cents: int
    currency: str
    selected_method: SelectedShippingMethod | None
    options: list[ShippingOption]


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def selection_to_method(row: DealShippingSelection | None) -> SelectedShippingMethod | None:
    if row is None:
        return None
    return SelectedShippingMethod(
        method_code=row.method_code,
        method_label=row.method_label,
        method_description=row.method_description,
        price_cents=row.price_cents,
        currency=row.currency,
        paid_by=row.paid_by,
        tracking_required=row.tracking_required,
        insurance_available=row.insurance_available,
        insurance_required=row.insurance_required,
        recommended=row.recommended,
        risk_level=row.risk_level,
        selected_by_user_id=row.selected_by_user_id,
        selected_at=row.selected_at,
    )


def option_to_snapshot(option: ShippingOption) -> dict[str, Any]:
    return asdict(option)


def _role_for_user(*, deal: Deal, user_id: str) -> str:
    if deal.buyer_user_id == user_id:
        return "buyer"
    if deal.seller_user_id == user_id:
        return "seller"
    raise deal_service.NotPartyToDeal(
        f"user {user_id!r} is not a party to deal {deal.id!r}"
    )


def _ensure_options_visible(deal: Deal) -> None:
    if deal.status not in ("confirmed", "completed") or deal.confirmed_at is None:
        raise deal_service.DealNotConfirmed(
            f"deal {deal.id!r} is in status {deal.status!r}; shipping options "
            f"open only after both signatures land"
        )


def _ensure_selection_mutable(deal: Deal) -> None:
    if deal.status != "confirmed" or deal.confirmed_at is None:
        raise deal_service.DealNotConfirmed(
            f"deal {deal.id!r} is in status {deal.status!r}; shipping method "
            f"can be selected only after confirmation"
        )
    if (deal.shipping_status or "shipping_pending") != "shipping_pending":
        raise ShippingMethodSelectionLocked(
            f"deal {deal.id!r} shipping method is locked after shipping starts"
        )


def _contains_token(value: Any, token: str) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return token in value.lower()
    if isinstance(value, list | tuple | set):
        return any(_contains_token(item, token) for item in value)
    if isinstance(value, dict):
        return any(_contains_token(item, token) for item in value.values())
    return False


def _pickup_available(*, buy_intent: Intent | None, sell_intent: Intent | None) -> bool:
    for intent in (sell_intent, buy_intent):
        constraints = intent.hard_constraints if intent is not None else None
        if not isinstance(constraints, dict):
            continue
        delivery = constraints.get("delivery") or constraints.get("shipping")
        if _contains_token(delivery, "pickup"):
            return True
        if constraints.get("location") or constraints.get("city") or constraints.get("pickup"):
            return True
    return False


def _category_from_intents(*, buy_intent: Intent | None, sell_intent: Intent | None) -> str:
    for intent in (sell_intent, buy_intent):
        if intent is not None and intent.category:
            return str(intent.category).lower()
    return "unknown"


def _category_kind(category: str) -> Literal["small", "electronics", "fragile", "unknown"]:
    if any(token in category for token in SMALL_CATEGORY_TOKENS):
        return "small"
    if any(token in category for token in ELECTRONICS_CATEGORY_TOKENS):
        return "electronics"
    if any(token in category for token in FRAGILE_CATEGORY_TOKENS):
        return "fragile"
    return "unknown"


def _risk_level(*, category_kind: str, agreed_price_cents: int) -> Literal["low", "medium", "high"]:
    if agreed_price_cents >= 50_000 or category_kind == "fragile":
        return "high"
    if agreed_price_cents >= 10_000 or category_kind in ("electronics", "unknown"):
        return "medium"
    return "low"


def calculate_shipping_options(
    *,
    deal: Deal,
    buy_intent: Intent | None,
    sell_intent: Intent | None,
) -> list[ShippingOption]:
    currency = deal.currency
    price = int(deal.agreed_price_cents)
    category = _category_from_intents(buy_intent=buy_intent, sell_intent=sell_intent)
    category_kind = _category_kind(category)
    risk = _risk_level(category_kind=category_kind, agreed_price_cents=price)
    pickup_available = _pickup_available(buy_intent=buy_intent, sell_intent=sell_intent)
    small = category_kind == "small"
    tracking_required = price >= 2_500
    high_value_requires_insurance = price >= 50_000
    insurance_available = price >= 10_000 or category_kind in ("electronics", "fragile")
    insurance_recommended = high_value_requires_insurance or category_kind == "fragile"

    untracked_allowed = small and price < 2_500
    registered_allowed = small or (category_kind == "unknown" and price < 2_500)
    tracked_allowed = not high_value_requires_insurance

    recommended_code = "tracked_parcel"
    if pickup_available:
        recommended_code = "pickup"
    elif high_value_requires_insurance:
        recommended_code = "insured_tracked_parcel"
    elif insurance_recommended:
        recommended_code = "insured_tracked_parcel"
    elif price < 2_500 and small:
        recommended_code = "untracked_letter"
    elif price < 2_500 and category_kind == "unknown":
        recommended_code = "registered_letter"

    return [
        ShippingOption(
            code="pickup",
            label="Ritiro a mano",
            description="Scambio coordinato tra le parti senza spedizione.",
            price_cents=0,
            currency=currency,
            tracking_required=False,
            insurance_available=False,
            insurance_required=False,
            recommended=recommended_code == "pickup",
            allowed=pickup_available,
            disabled_reason=None if pickup_available else "pickup_not_available",
            risk_level="low",
        ),
        ShippingOption(
            code="untracked_letter",
            label="Lettera non tracciata",
            description="Opzione economica per oggetti piccoli e basso valore.",
            price_cents=250,
            currency=currency,
            tracking_required=False,
            insurance_available=False,
            insurance_required=False,
            recommended=recommended_code == "untracked_letter",
            allowed=untracked_allowed,
            disabled_reason=None
            if untracked_allowed
            else (
                "tracking_required_over_25_eur"
                if tracking_required
                else "category_not_compatible"
            ),
            risk_level="low" if untracked_allowed else risk,
        ),
        ShippingOption(
            code="registered_letter",
            label="Raccomandata",
            description="Lettera tracciata per oggetti piccoli.",
            price_cents=650,
            currency=currency,
            tracking_required=True,
            insurance_available=False,
            insurance_required=False,
            recommended=recommended_code == "registered_letter",
            allowed=registered_allowed,
            disabled_reason=None if registered_allowed else "category_not_compatible",
            risk_level="low" if price < 2_500 else "medium",
        ),
        ShippingOption(
            code="tracked_parcel",
            label="Pacco tracciato",
            description="Spedizione standard tracciata per la maggior parte dei deal.",
            price_cents=990,
            currency=currency,
            tracking_required=True,
            insurance_available=insurance_available,
            insurance_required=False,
            recommended=recommended_code == "tracked_parcel",
            allowed=tracked_allowed,
            disabled_reason=None
            if tracked_allowed
            else "insurance_required_for_high_value",
            risk_level="medium" if risk == "low" else risk,
        ),
        ShippingOption(
            code="insured_tracked_parcel",
            label="Pacco tracciato assicurato",
            description="Spedizione tracciata con assicurazione per deal ad alto rischio.",
            price_cents=1490,
            currency=currency,
            tracking_required=True,
            insurance_available=True,
            insurance_required=high_value_requires_insurance,
            recommended=recommended_code == "insured_tracked_parcel",
            allowed=True,
            disabled_reason=None,
            risk_level="high" if insurance_recommended else "medium",
        ),
    ]


async def _load_intents(db: AsyncSession, deal: Deal) -> tuple[Intent | None, Intent | None]:
    buy_intent = await db.get(Intent, deal.buy_intent_id)
    sell_intent = await db.get(Intent, deal.sell_intent_id)
    return buy_intent, sell_intent


async def _selected_for_deal(
    db: AsyncSession, *, deal_id: str
) -> DealShippingSelection | None:
    return await db.scalar(
        select(DealShippingSelection).where(DealShippingSelection.deal_id == deal_id)
    )


async def get_shipping_options_for_user(
    db: AsyncSession,
    *,
    user_id: str,
    deal_id: str,
) -> ShippingOptionsState:
    deal = await deal_service.get_deal_for_user(
        db, user_id=user_id, deal_id=deal_id
    )
    _ensure_options_visible(deal)
    buy_intent, sell_intent = await _load_intents(db, deal)
    selected = await _selected_for_deal(db, deal_id=deal.id)
    return ShippingOptionsState(
        deal_id=deal.id,
        agreed_price_cents=deal.agreed_price_cents,
        currency=deal.currency,
        selected_method=selection_to_method(selected),
        options=calculate_shipping_options(
            deal=deal,
            buy_intent=buy_intent,
            sell_intent=sell_intent,
        ),
    )


async def select_shipping_method_for_user(
    db: AsyncSession,
    *,
    user_id: str,
    deal_id: str,
    method_code: str,
    paid_by: str,
) -> ShippingOptionsState:
    if paid_by not in PAID_BY_VALUES:
        raise ShippingMethodNotAllowed(f"paid_by {paid_by!r} is not supported")

    deal = await db.scalar(
        select(Deal).where(Deal.id == deal_id).with_for_update()
    )
    if deal is None:
        raise deal_service.DealNotFound(f"deal {deal_id!r} not found")
    _role_for_user(deal=deal, user_id=user_id)
    _ensure_selection_mutable(deal)

    buy_intent, sell_intent = await _load_intents(db, deal)
    options = calculate_shipping_options(
        deal=deal,
        buy_intent=buy_intent,
        sell_intent=sell_intent,
    )
    option_by_code = {option.code: option for option in options}
    option = option_by_code.get(method_code)
    if option is None or not option.allowed:
        reason = option.disabled_reason if option is not None else "unknown_method"
        raise ShippingMethodNotAllowed(
            f"shipping method {method_code!r} is not allowed: {reason}"
        )

    selected = await _selected_for_deal(db, deal_id=deal.id)
    if (
        selected is not None
        and selected.method_code == method_code
        and selected.paid_by == paid_by
    ):
        return ShippingOptionsState(
            deal_id=deal.id,
            agreed_price_cents=deal.agreed_price_cents,
            currency=deal.currency,
            selected_method=selection_to_method(selected),
            options=options,
        )

    now = _utcnow()
    changed = (
        selected is None
        or selected.method_code != method_code
        or selected.paid_by != paid_by
    )
    if selected is None:
        selected = DealShippingSelection(
            deal_id=deal.id,
            created_at=now,
        )
        db.add(selected)

    selected.method_code = option.code
    selected.method_label = option.label
    selected.method_description = option.description
    selected.price_cents = option.price_cents
    selected.currency = option.currency
    selected.paid_by = paid_by
    selected.tracking_required = option.tracking_required
    selected.insurance_available = option.insurance_available
    selected.insurance_required = option.insurance_required
    selected.recommended = option.recommended
    selected.risk_level = option.risk_level
    selected.selected_by_user_id = user_id
    selected.selected_at = now
    selected.updated_at = now
    selected.policy_snapshot = option_to_snapshot(option)

    if changed:
        await audit_service.log_intent_event(
            db,
            user_id=user_id,
            action=audit_service.DealActions.SHIPPING_METHOD_SELECTED,
            params={
                "deal_id": deal.id,
                "method_code": option.code,
                "paid_by": paid_by,
            },
            result={
                "price_cents": option.price_cents,
                "tracking_required": option.tracking_required,
                "insurance_required": option.insurance_required,
            },
            success=True,
        )

    await db.commit()
    await db.refresh(selected)

    return ShippingOptionsState(
        deal_id=deal.id,
        agreed_price_cents=deal.agreed_price_cents,
        currency=deal.currency,
        selected_method=selection_to_method(selected),
        options=options,
    )
