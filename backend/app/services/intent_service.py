"""Intent service — BUY/SELL CRUD for the marketplace (brief task 4.1).

Public surface:
  - CreateIntentInput / UpdateIntentInput  — Pydantic v2 input models
  - IntentError (+ subclasses)             — typed errors with code+http_status
  - create_intent(db, user_id, input)      → Intent
  - list_user_intents(db, user_id, ...)    → (rows, total)
  - get_intent_for_user(db, user_id, id)   → Intent | None
  - update_intent(db, user_id, id, input)  → Intent
  - cancel_intent(db, user_id, id)         → Intent

Design notes:

- Async-only. The §5 scaffold's tool_layer call site (`intent_service.
  create_intent`) is sync and currently disconnected — re-wiring tool_layer
  to async is a FASE 5/6 task (see DESIGN_QUESTIONS DQ-28). For 4.1, this
  service is exclusively driven by the FastAPI endpoints in `api/intents.py`.

- Tier-based active-intent caps: tier 0 → 5, tier 1 → 10, tier 2 → reads
  from the user's active mandate (`limits.max_active_intents`, defaulting
  to V0_DEFAULT). The cap is checked on *active* intents only —
  matched/closed/cancelled/expired/paused don't count.

- Embedding generation is sync-inline via `embedding_service`. Failure is
  terminal for `create_intent`: an intent without an embedding is invisible
  to the matcher (4.3), so we'd rather 503 than persist a ghost row.

- `side='trade'` is rejected operationally with 422
  (`trade_not_yet_available`) per PROJECT_BRIEF §2.9. Schema accepts it
  (column widened to String(5) in migration 8df1d6891fd9) so V1 FASE 8
  can flip the switch without further migration.

- Price update on an intent with active negotiations is blocked (409). The
  brief's reasoning: changing floor/cap mid-negotiation can desynchronize
  the agent's strategy. To change price, cancel + recreate.

- Step-up signature on price updates is part of the desired UX but the
  cryptographic challenge-binding for arbitrary user-initiated actions
  doesn't exist yet. V0 gates by `tier=2` (which already requires a
  passkey); the full webauthn-bound update flow is deferred (DQ-29).
"""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Final, Literal

from pydantic import BaseModel, Field, model_validator
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import categories, platform_limits as pl
from app.models.schema import Intent, Mandate, Match, Negotiation, User
from app.services import audit_service, embedding_service


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


MAX_PRICE_PER_INTENT_EUR: Final[int] = 10_000
MAX_DURATION_DAYS: Final[int] = 30
MIN_DURATION_DAYS: Final[int] = 1
DEFAULT_DURATION_DAYS: Final[int] = 14

MAX_TITLE_LEN: Final[int] = 200
MAX_DESCRIPTION_LEN: Final[int] = 2000

# Tier-based caps for user.tier ∈ {0, 1}. Tier 2 reads the mandate.
TIER_0_MAX_ACTIVE_INTENTS: Final[int] = 5
TIER_1_MAX_ACTIVE_INTENTS: Final[int] = 10

ALLOWED_CURRENCIES: Final[tuple[str, ...]] = ("EUR",)

_HTML_TAG_RE = re.compile(r"<[^>]+>")
_URL_RE = re.compile(r"https?://", re.IGNORECASE)
_LOCATION_RE = re.compile(r"^[^,]+,\s*[A-Z]{2}$")


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class IntentError(Exception):
    code: str = "intent_error"
    http_status: int = 400


class TradeNotYetAvailable(IntentError):
    code = "trade_not_yet_available"
    http_status = 422


class InvalidSide(IntentError):
    code = "invalid_side"
    http_status = 422


class InvalidTitle(IntentError):
    code = "invalid_title"
    http_status = 422


class InvalidDescription(IntentError):
    code = "invalid_description"
    http_status = 422


class CategoryForbidden(IntentError):
    code = "category_forbidden"
    http_status = 422


class CategoryNotAllowed(IntentError):
    code = "category_not_allowed"
    http_status = 422


class CategoryNotModifiable(IntentError):
    code = "category_not_modifiable"
    http_status = 422


class SideNotModifiable(IntentError):
    code = "side_not_modifiable"
    http_status = 422


class InvalidPrice(IntentError):
    code = "invalid_price"
    http_status = 422


class PriceExceedsPlatformLimit(IntentError):
    code = "price_exceeds_platform_limit"
    http_status = 422


class InvalidPriceRelationship(IntentError):
    code = "invalid_price_relationship"
    http_status = 422


class InvalidDurationDays(IntentError):
    code = "invalid_duration_days"
    http_status = 422


class InvalidLocation(IntentError):
    code = "invalid_location"
    http_status = 422


class CurrencyNotSupported(IntentError):
    code = "currency_not_supported"
    http_status = 422


class TooManyActiveIntents(IntentError):
    """Tier-based cap exceeded → 402 with next-step payload."""

    code = "too_many_active_intents"
    http_status = 402


class IntentNotFound(IntentError):
    code = "intent_not_found"
    http_status = 404


class IntentNotEditable(IntentError):
    code = "intent_not_editable"
    http_status = 409


class IntentInActiveNegotiation(IntentError):
    code = "intent_in_active_negotiation"
    http_status = 409


class UserNotFound(IntentError):
    code = "user_not_found"
    http_status = 404


class TierTooLowForPriceUpdate(IntentError):
    """Reservation/ideal price update requires tier ≥ 2."""

    code = "tier_too_low_for_price_update"
    http_status = 402


class EmbeddingUnavailable(IntentError):
    code = "embedding_service_unavailable"
    http_status = 503


# ---------------------------------------------------------------------------
# Input models
# ---------------------------------------------------------------------------


class CreateIntentInput(BaseModel):
    side: Literal["buy", "sell", "trade"]
    title: str
    description: str | None = None
    category: str
    reservation_price_eur: float = Field(gt=0)
    ideal_price_eur: float = Field(gt=0)
    duration_days: int = Field(default=DEFAULT_DURATION_DAYS)
    hard_constraints: dict[str, Any] = Field(default_factory=dict)
    soft_preferences: dict[str, Any] = Field(default_factory=dict)
    currency: str = "EUR"


class UpdateIntentInput(BaseModel):
    """All fields optional — service applies only those that are set.

    `category` and `side` are accepted at the schema level but rejected
    explicitly by the service (with distinct error codes) so we can tell
    the user *why* the update failed instead of a generic "invalid request".
    """

    title: str | None = None
    description: str | None = None
    reservation_price_eur: float | None = Field(default=None, gt=0)
    ideal_price_eur: float | None = Field(default=None, gt=0)
    duration_days: int | None = None
    soft_preferences: dict[str, Any] | None = None
    category: str | None = None
    side: str | None = None

    @model_validator(mode="after")
    def _at_least_one_field(self) -> "UpdateIntentInput":
        if all(getattr(self, f) is None for f in type(self).model_fields):
            raise ValueError("at least one field must be provided")
        return self


# ---------------------------------------------------------------------------
# Output dataclasses
# ---------------------------------------------------------------------------


@dataclass
class IntentListPage:
    rows: list[Intent]
    total: int
    limit: int
    offset: int


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _eur_to_cents(eur: float) -> int:
    return round(eur * 100)


def _cents_to_eur(cents: int) -> float:
    return cents / 100


def _next_step_for_tier(tier: int) -> dict[str, str]:
    if tier == 0:
        return {
            "path": "/api/identity/verify-self",
            "description": (
                "Verifica la tua identità per aumentare il limite di "
                "intent attivi."
            ),
        }
    if tier == 1:
        return {
            "path": "/api/mandates/draft",
            "description": (
                "Autorizza il tuo agente per sbloccare il limite più alto "
                "configurato nel tuo mandate."
            ),
        }
    return {}


# ---------------------------------------------------------------------------
# Validation (pure functions; raise typed IntentError)
# ---------------------------------------------------------------------------


def _validate_side_for_create(side: str) -> None:
    if side == "trade":
        raise TradeNotYetAvailable(
            "TRADE not yet available, coming in V1 (FASE 8)"
        )
    if side not in ("buy", "sell"):
        raise InvalidSide(f"side must be 'buy' or 'sell', got {side!r}")


def _validate_title(title: str) -> None:
    if not title or not title.strip():
        raise InvalidTitle("title must not be empty")
    if len(title) > MAX_TITLE_LEN:
        raise InvalidTitle(f"title exceeds {MAX_TITLE_LEN} chars")
    if _HTML_TAG_RE.search(title):
        raise InvalidTitle("title must not contain HTML tags")
    if _URL_RE.search(title):
        raise InvalidTitle("title must not contain URLs")


def _validate_description(description: str | None) -> None:
    if description is None:
        return
    if len(description) > MAX_DESCRIPTION_LEN:
        raise InvalidDescription(
            f"description exceeds {MAX_DESCRIPTION_LEN} chars"
        )


def _validate_category(category: str) -> None:
    if categories.is_forbidden(category):
        raise CategoryForbidden(
            f"category {category!r} is hard-forbidden on the platform"
        )
    if not categories.is_allowed(category):
        raise CategoryNotAllowed(
            f"category {category!r} is not in the V0 vocabulary"
        )


def _validate_price(price_eur: float, *, field: str) -> None:
    if price_eur <= 0:
        raise InvalidPrice(f"{field} must be > 0")
    if price_eur > MAX_PRICE_PER_INTENT_EUR:
        raise PriceExceedsPlatformLimit(
            f"{field} exceeds platform limit €{MAX_PRICE_PER_INTENT_EUR}"
        )


def _validate_price_relationship(
    *, side: str, reservation_eur: float, ideal_eur: float
) -> None:
    """For SELL ideal >= reservation (floor); for BUY ideal <= reservation (cap)."""
    if side == "sell" and ideal_eur < reservation_eur:
        raise InvalidPriceRelationship(
            "for SELL, ideal_price_eur must be >= reservation_price_eur "
            "(reservation is the floor, ideal is the target above it)"
        )
    if side == "buy" and ideal_eur > reservation_eur:
        raise InvalidPriceRelationship(
            "for BUY, ideal_price_eur must be <= reservation_price_eur "
            "(reservation is the cap, ideal is the target below it)"
        )


def _validate_duration_days(days: int) -> None:
    if days < MIN_DURATION_DAYS or days > MAX_DURATION_DAYS:
        raise InvalidDurationDays(
            f"duration_days must be {MIN_DURATION_DAYS}-{MAX_DURATION_DAYS}"
        )


def _validate_hard_constraints(constraints: dict[str, Any]) -> None:
    """V0: only `location` is structurally validated. Other keys pass through.

    Location format: '<city>, <2-letter ISO uppercase>'. We don't enforce
    that the country is in geo_scope here — that's a mandate concern for
    when the agent acts on the intent.
    """
    location = constraints.get("location")
    if location is None:
        return
    if not isinstance(location, str) or not _LOCATION_RE.match(location):
        raise InvalidLocation(
            "hard_constraints.location must look like 'City, IT'"
        )


def _validate_currency(currency: str) -> None:
    if currency not in ALLOWED_CURRENCIES:
        raise CurrencyNotSupported(
            f"currency {currency!r} not supported in V0 "
            f"(allowed: {ALLOWED_CURRENCIES})"
        )


# ---------------------------------------------------------------------------
# Tier limit resolution
# ---------------------------------------------------------------------------


async def _max_active_intents_for_user(
    db: AsyncSession, user: User
) -> int:
    if user.tier == 0:
        return TIER_0_MAX_ACTIVE_INTENTS
    if user.tier == 1:
        return TIER_1_MAX_ACTIVE_INTENTS
    # tier >= 2: read mandate. Defensive default if no active mandate found.
    mandate = await db.scalar(
        select(Mandate)
        .where(Mandate.user_id == user.id)
        .where(Mandate.revoked_at.is_(None))
        .order_by(Mandate.issued_at.desc())
    )
    if mandate is None:
        return pl.DEFAULT_MAX_ACTIVE_INTENTS
    limits = mandate.limits or {}
    return int(
        limits.get("max_active_intents", pl.DEFAULT_MAX_ACTIVE_INTENTS)
    )


async def _count_active_intents(db: AsyncSession, user_id: str) -> int:
    return int(
        await db.scalar(
            select(func.count())
            .select_from(Intent)
            .where(Intent.user_id == user_id)
            .where(Intent.status == "active")
        )
        or 0
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def create_intent(
    db: AsyncSession,
    *,
    user_id: str,
    input: CreateIntentInput,
) -> Intent:
    """Create + persist a new intent. Generates the embedding inline.

    Mutates `db` (add + commit). Returns the persisted Intent row.
    """
    # 1. Field validation — fast-fail before any DB work or network call.
    _validate_side_for_create(input.side)
    _validate_title(input.title)
    _validate_description(input.description)
    _validate_category(input.category)
    _validate_price(input.reservation_price_eur, field="reservation_price_eur")
    _validate_price(input.ideal_price_eur, field="ideal_price_eur")
    _validate_price_relationship(
        side=input.side,
        reservation_eur=input.reservation_price_eur,
        ideal_eur=input.ideal_price_eur,
    )
    _validate_duration_days(input.duration_days)
    _validate_hard_constraints(input.hard_constraints)
    _validate_currency(input.currency)

    # 2. Tier-based active-intent cap.
    user = await db.get(User, user_id)
    if user is None:
        raise UserNotFound(f"user {user_id!r} not found")

    limit = await _max_active_intents_for_user(db, user)
    current = await _count_active_intents(db, user_id)
    if current >= limit:
        raise TooManyActiveIntents(
            f"tier {user.tier} allows max {limit} active intents "
            f"(currently {current})"
        )

    # 3. Embedding (sync inline). Terminal failure on unavailable.
    text_to_embed = embedding_service.build_embedding_text(
        title=input.title, description=input.description
    )
    try:
        embedding = await embedding_service.generate_embedding(text_to_embed)
    except embedding_service.EmbeddingServiceUnavailable as exc:
        raise EmbeddingUnavailable(str(exc)) from exc

    # 4. Persist.
    now = _utcnow()
    intent_id = str(uuid.uuid4())
    intent = Intent(
        id=intent_id,
        user_id=user_id,
        agent_id=None,  # tier-0 has no agent; tier-1+ binding is FASE 5/6 work
        side=input.side,
        title=input.title.strip(),
        description=input.description,
        category=input.category,
        description_embedding=embedding,
        reservation_price_cents=_eur_to_cents(input.reservation_price_eur),
        ideal_price_cents=_eur_to_cents(input.ideal_price_eur),
        currency=input.currency,
        hard_constraints=input.hard_constraints,
        soft_preferences=input.soft_preferences,
        status="active",
        expires_at=now + timedelta(days=input.duration_days),
        created_at=now,
    )
    db.add(intent)
    await db.flush()

    # 5. Audit (uses the same session; will commit together with the row).
    await audit_service.log_intent_event(
        db,
        user_id=user_id,
        action=audit_service.IntentActions.CREATE,
        params={
            "intent_id": intent_id,
            "side": input.side,
            "category": input.category,
            "reservation_price_eur": input.reservation_price_eur,
            "ideal_price_eur": input.ideal_price_eur,
        },
        result={"intent_id": intent_id, "status": "active"},
        success=True,
    )

    await db.commit()
    await db.refresh(intent)
    return intent


async def list_user_intents(
    db: AsyncSession,
    *,
    user_id: str,
    status: str | None = None,
    side: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> IntentListPage:
    """List intents owned by the user, paginated. No filtering on agent_id.

    `limit` is clamped to [1, 100], `offset` to [0, ∞).
    """
    limit = max(1, min(100, limit))
    offset = max(0, offset)

    base_filters = [Intent.user_id == user_id]
    if status is not None:
        base_filters.append(Intent.status == status)
    if side is not None:
        base_filters.append(Intent.side == side)

    total = int(
        await db.scalar(
            select(func.count()).select_from(Intent).where(*base_filters)
        )
        or 0
    )

    rows = list(
        await db.scalars(
            select(Intent)
            .where(*base_filters)
            .order_by(Intent.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
    )

    return IntentListPage(rows=rows, total=total, limit=limit, offset=offset)


async def get_intent_for_user(
    db: AsyncSession, *, user_id: str, intent_id: str
) -> Intent | None:
    """Return the intent if it belongs to the user, else None.

    Returns None for not-found AND for not-owned — the API maps both to 404
    so we don't leak existence info on intents owned by other users.
    """
    return await db.scalar(
        select(Intent)
        .where(Intent.id == intent_id)
        .where(Intent.user_id == user_id)
    )


async def update_intent(
    db: AsyncSession,
    *,
    user_id: str,
    user_tier: int,
    intent_id: str,
    input: UpdateIntentInput,
) -> Intent:
    """Apply selective updates to an intent.

    Field-level gating:
      - `category` / `side`        → never modifiable (422)
      - `reservation_price_eur`    → tier ≥ 2
      - `ideal_price_eur`          → tier ≥ 2 (financially relevant)
      - `duration_days`            → any tier (extends `expires_at`)
      - `title` / `description` /  → any tier
        `soft_preferences`

    Modifying the price on an intent that has any active negotiation
    raises `IntentInActiveNegotiation` (409). Same on a non-active intent.
    """
    # 1. Reject non-modifiable fields up front so we don't waste a row lock.
    if input.category is not None:
        raise CategoryNotModifiable(
            "category is not modifiable; cancel and create a new intent"
        )
    if input.side is not None:
        raise SideNotModifiable(
            "side is not modifiable; cancel and create a new intent"
        )

    # 2. Tier gate on price changes.
    price_change = (
        input.reservation_price_eur is not None
        or input.ideal_price_eur is not None
    )
    if price_change and user_tier < 2:
        raise TierTooLowForPriceUpdate(
            "reservation/ideal price changes require tier 2"
        )

    # 3. Lock the row + verify ownership + status.
    intent = await db.scalar(
        select(Intent)
        .where(Intent.id == intent_id)
        .where(Intent.user_id == user_id)
        .with_for_update()
    )
    if intent is None:
        raise IntentNotFound(f"intent {intent_id!r} not found")
    if intent.status != "active":
        raise IntentNotEditable(
            f"intent is in status {intent.status!r}, not editable"
        )

    # 4. Active-negotiation guard for price changes.
    if price_change:
        active_neg = await db.scalar(
            select(func.count())
            .select_from(Negotiation)
            .join(Match, Match.id == Negotiation.match_id)
            .where(
                (Match.buy_intent_id == intent_id)
                | (Match.sell_intent_id == intent_id)
            )
            .where(Negotiation.status == "active")
        )
        if active_neg and int(active_neg) > 0:
            raise IntentInActiveNegotiation(
                "intent has an active negotiation; cancel and recreate "
                "to change price"
            )

    # 5. Apply scalar field updates.
    changed: dict[str, Any] = {}

    if input.title is not None:
        _validate_title(input.title)
        intent.title = input.title.strip()
        changed["title"] = intent.title

    if input.description is not None:
        _validate_description(input.description)
        intent.description = input.description
        changed["description"] = "<changed>"

    if input.soft_preferences is not None:
        intent.soft_preferences = input.soft_preferences
        changed["soft_preferences"] = "<changed>"

    if input.duration_days is not None:
        _validate_duration_days(input.duration_days)
        # Extend expires_at relative to NOW, not to the original created_at,
        # so a "renew" semantics emerges naturally.
        intent.expires_at = _utcnow() + timedelta(days=input.duration_days)
        changed["expires_at"] = intent.expires_at.isoformat()

    if input.reservation_price_eur is not None:
        _validate_price(
            input.reservation_price_eur, field="reservation_price_eur"
        )
        intent.reservation_price_cents = _eur_to_cents(
            input.reservation_price_eur
        )
        changed["reservation_price_eur"] = input.reservation_price_eur

    if input.ideal_price_eur is not None:
        _validate_price(input.ideal_price_eur, field="ideal_price_eur")
        intent.ideal_price_cents = _eur_to_cents(input.ideal_price_eur)
        changed["ideal_price_eur"] = input.ideal_price_eur

    # Re-validate price relationship if either price changed.
    if price_change:
        _validate_price_relationship(
            side=intent.side,
            reservation_eur=_cents_to_eur(intent.reservation_price_cents),
            ideal_eur=_cents_to_eur(intent.ideal_price_cents),
        )

    await db.flush()

    await audit_service.log_intent_event(
        db,
        user_id=user_id,
        action=audit_service.IntentActions.UPDATE,
        params={"intent_id": intent_id, "fields": list(changed.keys())},
        result={"changed": changed},
        success=True,
    )

    await db.commit()
    await db.refresh(intent)
    return intent


@dataclass
class CancelResult:
    intent: Intent
    negotiations_cancelled: int
    matches_expired: int
    already_cancelled: bool


async def cancel_intent(
    db: AsyncSession, *, user_id: str, intent_id: str
) -> CancelResult:
    """Mark `cancelled`. Cascade-cancel active negotiations + expire matches.

    Idempotent: cancelling an already-cancelled intent is a no-op (returns
    `already_cancelled=True`) instead of an error. Behavior matches the
    revoke pattern in `mandate_revocation_service`.
    """
    intent = await db.scalar(
        select(Intent)
        .where(Intent.id == intent_id)
        .where(Intent.user_id == user_id)
        .with_for_update()
    )
    if intent is None:
        raise IntentNotFound(f"intent {intent_id!r} not found")

    if intent.status == "cancelled":
        return CancelResult(
            intent=intent,
            negotiations_cancelled=0,
            matches_expired=0,
            already_cancelled=True,
        )

    now = _utcnow()
    intent.status = "cancelled"
    intent.closed_at = now

    # Cascade: active negotiations on matches involving this intent → cancelled.
    neg_result = await db.execute(
        update(Negotiation)
        .where(
            Negotiation.match_id.in_(
                select(Match.id).where(
                    (Match.buy_intent_id == intent_id)
                    | (Match.sell_intent_id == intent_id)
                )
            )
        )
        .where(Negotiation.status == "active")
        .values(status="cancelled", closed_at=now)
    )
    neg_count = neg_result.rowcount or 0

    # Cascade: existing matches involving this intent → expired.
    match_result = await db.execute(
        update(Match)
        .where(
            (Match.buy_intent_id == intent_id)
            | (Match.sell_intent_id == intent_id)
        )
        .where(Match.status.in_(("discovered", "negotiating")))
        .values(status="expired")
    )
    match_count = match_result.rowcount or 0

    await db.flush()

    await audit_service.log_intent_event(
        db,
        user_id=user_id,
        action=audit_service.IntentActions.CANCEL,
        params={"intent_id": intent_id},
        result={
            "negotiations_cancelled": neg_count,
            "matches_expired": match_count,
        },
        success=True,
    )

    await db.commit()
    await db.refresh(intent)

    return CancelResult(
        intent=intent,
        negotiations_cancelled=neg_count,
        matches_expired=match_count,
        already_cancelled=False,
    )
