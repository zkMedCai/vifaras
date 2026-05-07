"""Autonomous Capital Mandate V0.

This is an operational mandate layered on top of the base Mandate. It is
signed with passkey, but it does not custody or move real money. V0 stores
policy, consent, an operational ledger, and inventory/position skeletons.
"""
from __future__ import annotations

import json
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Final

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from webauthn import verify_authentication_response

from app.core import canonicalization, platform_limits as pl
from app.core.config import settings
from app.core.logging import log
from app.models.schema import (
    Agent,
    CapitalLedgerEntry,
    CapitalMandate,
    CapitalMandateDraft,
    CapitalPosition,
    Mandate,
    User,
)
from app.services import audit_service, capital_ledger_service
from app.services.auth_service import _b64url, _b64url_decode
from app.services.mandate_service import WebAuthnAssertionPayload


DEFAULT_CAPITAL_BUDGET_CENTS: Final[int] = 50_000
MAX_CAPITAL_BUDGET_CENTS: Final[int] = 50_000
DEFAULT_DURATION_DAYS: Final[int] = 30
MAX_DURATION_DAYS: Final[int] = 30
DEFAULT_MAX_SINGLE_PURCHASE_CENTS: Final[int] = 10_000
MAX_OPEN_POSITIONS: Final[int] = 20
DEFAULT_MAX_OPEN_POSITIONS: Final[int] = 5
DRAFT_TTL_SECONDS: Final[int] = 300
RISK_LEVELS: Final[set[str]] = {"low", "medium", "high"}
STATUS_ACTIVE: Final[str] = "active"
STATUS_PAUSED: Final[str] = "paused"
STATUS_EXPIRED: Final[str] = "expired"
STATUS_REVOKED: Final[str] = "revoked"
STATUS_SETTLED: Final[str] = "settled"


class CapitalMandateError(Exception):
    code: str = "capital_mandate_error"
    http_status: int = 400


class UserNotFound(CapitalMandateError):
    code = "user_not_found"
    http_status = 404


class AgentNotOwned(CapitalMandateError):
    code = "agent_not_owned"
    http_status = 403


class BaseMandateRequired(CapitalMandateError):
    code = "base_mandate_required"
    http_status = 409


class ActiveCapitalMandateExists(CapitalMandateError):
    code = "active_capital_mandate_exists"
    http_status = 409


class CapitalMandateNotFound(CapitalMandateError):
    code = "capital_mandate_not_found"
    http_status = 404


class CapitalMandateDraftNotFound(CapitalMandateError):
    code = "capital_mandate_draft_not_found"
    http_status = 404


class CapitalMandateDraftExpired(CapitalMandateError):
    code = "capital_mandate_draft_expired"
    http_status = 410


class CapitalMandateDraftConsumed(CapitalMandateError):
    code = "capital_mandate_draft_consumed"
    http_status = 409


class CapitalMandateInvalidLimits(CapitalMandateError):
    code = "capital_mandate_invalid_limits"
    http_status = 422


class CapitalMandateWebAuthnFailed(CapitalMandateError):
    code = "capital_mandate_webauthn_failed"
    http_status = 422


class CapitalMandateInvalidState(CapitalMandateError):
    code = "capital_mandate_invalid_state"
    http_status = 409


class CapitalMandateDraftInput(BaseModel):
    agent_id: str
    budget_total_cents: int = Field(default=DEFAULT_CAPITAL_BUDGET_CENTS, ge=1)
    duration_days: int = Field(default=DEFAULT_DURATION_DAYS, ge=1)
    max_single_purchase_cents: int = Field(
        default=DEFAULT_MAX_SINGLE_PURCHASE_CENTS, ge=1
    )
    max_open_positions: int = Field(default=DEFAULT_MAX_OPEN_POSITIONS, ge=1)
    max_daily_deals: int | None = Field(default=None, ge=1)
    min_expected_margin_bps: int = Field(default=0, ge=0)
    max_total_loss_cents: int | None = Field(default=None, ge=0)
    risk_level: str = "medium"
    allowed_categories: list[str] = Field(default_factory=list)
    forbidden_categories: list[str] = Field(default_factory=list)
    geo_scope: list[str] = Field(default_factory=lambda: ["IT"])
    constraints: dict[str, Any] = Field(default_factory=dict)
    auto_buy: bool = True
    auto_sell: bool = True
    auto_relist: bool = True


@dataclass
class CapitalMandateDraftCreated:
    draft_id: str
    payload: dict[str, Any]
    payload_summary: dict[str, Any]
    challenge_b64url: str
    expires_at_utc: datetime


@dataclass
class CapitalMandateSubmitResult:
    capital_mandate_id: str
    status: str
    budget_state: dict[str, int]
    expires_at: datetime


@dataclass
class ActiveCapitalMandateResult:
    mandate: CapitalMandate | None
    budget_state: dict[str, int] | None
    positions_summary: dict[str, int] | None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _utcnow_aware() -> datetime:
    return datetime.now(timezone.utc)


def _iso_z(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso_z(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")


def _signature_blob(assertion: WebAuthnAssertionPayload) -> dict[str, Any]:
    return {
        "algorithm": "webauthn",
        "credential_id": assertion.id,
        "raw_id": assertion.raw_id,
        "response": dict(assertion.response),
    }


def _clean_categories(values: list[str]) -> list[str]:
    return sorted({str(v).strip() for v in values if str(v).strip()})


def _validate_input(input_obj: CapitalMandateDraftInput) -> None:
    if input_obj.budget_total_cents > MAX_CAPITAL_BUDGET_CENTS:
        raise CapitalMandateInvalidLimits(
            f"budget_total_cents exceeds V0 cap {MAX_CAPITAL_BUDGET_CENTS}"
        )
    if input_obj.duration_days > MAX_DURATION_DAYS:
        raise CapitalMandateInvalidLimits(
            f"duration_days exceeds V0 cap {MAX_DURATION_DAYS}"
        )
    if input_obj.max_single_purchase_cents > input_obj.budget_total_cents:
        raise CapitalMandateInvalidLimits(
            "max_single_purchase_cents cannot exceed budget_total_cents"
        )
    if input_obj.max_open_positions > MAX_OPEN_POSITIONS:
        raise CapitalMandateInvalidLimits(
            f"max_open_positions exceeds V0 cap {MAX_OPEN_POSITIONS}"
        )
    if input_obj.risk_level not in RISK_LEVELS:
        raise CapitalMandateInvalidLimits(
            f"risk_level must be one of {sorted(RISK_LEVELS)}"
        )
    forbidden_hard = set(input_obj.allowed_categories) & set(
        pl.HARD_FORBIDDEN_CATEGORIES
    )
    if forbidden_hard:
        raise CapitalMandateInvalidLimits(
            f"allowed_categories contains hard-forbidden values: {sorted(forbidden_hard)}"
        )
    invalid_geo = set(input_obj.geo_scope) - set(pl.GEO_SCOPE_V0)
    if invalid_geo:
        raise CapitalMandateInvalidLimits(
            f"geo_scope contains values outside V0 set: {sorted(invalid_geo)}"
        )


async def _load_user_agent_base_mandate(
    db: AsyncSession, *, user_id: str, agent_id: str
) -> tuple[User, Agent, Mandate]:
    user = await db.get(User, user_id)
    if user is None:
        raise UserNotFound(user_id)
    if user.tier < 2:
        raise BaseMandateRequired("capital mandate requires tier 2")

    agent = await db.get(Agent, agent_id)
    if agent is None or agent.user_id != user_id:
        raise AgentNotOwned(agent_id)
    if agent.status != "active":
        raise BaseMandateRequired(
            f"agent.status={agent.status!r}; base mandate must be active"
        )

    base_mandate = await db.scalar(
        select(Mandate)
        .where(Mandate.agent_id == agent_id)
        .where(Mandate.user_id == user_id)
        .where(Mandate.revoked_at.is_(None))
        .order_by(Mandate.issued_at.desc())
    )
    if base_mandate is None or base_mandate.expires_at < _utcnow():
        raise BaseMandateRequired("no active base mandate for this agent")
    return user, agent, base_mandate


async def _ensure_no_active_capital_mandate(
    db: AsyncSession, *, user_id: str, agent_id: str
) -> None:
    existing = await db.scalar(
        select(CapitalMandate)
        .where(CapitalMandate.user_id == user_id)
        .where(CapitalMandate.agent_id == agent_id)
        .where(CapitalMandate.status.in_([STATUS_ACTIVE, STATUS_PAUSED]))
        .order_by(CapitalMandate.created_at.desc())
    )
    if existing is not None and existing.expires_at >= _utcnow():
        raise ActiveCapitalMandateExists(
            f"agent {agent_id!r} already has an active capital mandate"
        )


def _build_payload(
    *,
    capital_mandate_id: str,
    user: User,
    agent: Agent,
    base_mandate: Mandate,
    input_obj: CapitalMandateDraftInput,
    starts_at: datetime,
    expires_at: datetime,
    challenge_hex: str,
) -> dict[str, Any]:
    allowed_categories = _clean_categories(input_obj.allowed_categories)
    forbidden_categories = _clean_categories(input_obj.forbidden_categories)
    return {
        "schema_version": "1.0",
        "kind": "autonomous_capital_mandate",
        "mandate_id": capital_mandate_id,
        "user_id": user.id,
        "agent_id": agent.id,
        "base_mandate_id": base_mandate.id,
        "budget_total_cents": int(input_obj.budget_total_cents),
        "currency": "EUR",
        "starts_at": _iso_z(starts_at),
        "expires_at": _iso_z(expires_at),
        "duration_days": int(input_obj.duration_days),
        "max_single_purchase_cents": int(input_obj.max_single_purchase_cents),
        "max_open_positions": int(input_obj.max_open_positions),
        "max_daily_deals": input_obj.max_daily_deals,
        "min_expected_margin_bps": int(input_obj.min_expected_margin_bps),
        "max_total_loss_cents": input_obj.max_total_loss_cents,
        "risk_level": input_obj.risk_level,
        "allowed_categories": allowed_categories,
        "forbidden_categories": forbidden_categories,
        "geo_scope": list(input_obj.geo_scope),
        "constraints": dict(input_obj.constraints),
        "auto_buy": bool(input_obj.auto_buy),
        "auto_sell": bool(input_obj.auto_sell),
        "auto_relist": bool(input_obj.auto_relist),
        "requires_manual_approval": False,
        "revocation": {
            "revocable_anytime_by_principal": True,
            "pausable_anytime_by_principal": True,
        },
        "disclaimer": {
            "no_real_money_moved_v0": True,
            "profits_not_guaranteed": True,
        },
        "challenge": challenge_hex,
    }


def _build_payload_summary(payload: dict[str, Any]) -> dict[str, Any]:
    budget = int(payload["budget_total_cents"]) / 100
    single = int(payload["max_single_purchase_cents"]) / 100
    categories = payload["allowed_categories"] or ["tutte le categorie consentite"]
    margin = int(payload["min_expected_margin_bps"]) / 100
    return {
        "human_readable": (
            f"Autorizzi il tuo agente a operare per "
            f"{payload['duration_days']} giorni con budget massimo "
            f"{budget:.0f}€, massimo {single:.0f}€ per acquisto, "
            f"categorie consentite {', '.join(categories)}, margine minimo "
            f"{margin:.2f}%. Puoi sospendere o revocare il mandato in "
            f"qualsiasi momento. I profitti non sono garantiti."
        ),
        "key_fields": [
            {"label": "Budget di compravendita", "value": f"{budget:.0f}€"},
            {"label": "Durata", "value": f"{payload['duration_days']} giorni"},
            {"label": "Massimo per acquisto", "value": f"{single:.0f}€"},
            {"label": "Posizioni aperte", "value": str(payload["max_open_positions"])},
            {"label": "Rischio", "value": payload["risk_level"]},
            {"label": "Profitto", "value": "Non garantito"},
        ],
        "v0_notice": (
            "V0 non muove denaro reale: prepara policy, autorizzazioni e "
            "ledger operativo."
        ),
    }


async def create_capital_mandate_draft(
    db: AsyncSession,
    *,
    user_id: str,
    input_obj: CapitalMandateDraftInput,
) -> CapitalMandateDraftCreated:
    _validate_input(input_obj)
    user, agent, base_mandate = await _load_user_agent_base_mandate(
        db, user_id=user_id, agent_id=input_obj.agent_id
    )
    await _ensure_no_active_capital_mandate(
        db, user_id=user_id, agent_id=input_obj.agent_id
    )

    now = _utcnow_aware()
    expires_at = now + timedelta(days=input_obj.duration_days)
    challenge = secrets.token_bytes(32)
    capital_mandate_id = str(uuid.uuid4())
    payload = _build_payload(
        capital_mandate_id=capital_mandate_id,
        user=user,
        agent=agent,
        base_mandate=base_mandate,
        input_obj=input_obj,
        starts_at=now,
        expires_at=expires_at,
        challenge_hex=challenge.hex(),
    )
    canonical_payload = canonicalization.canonicalize(payload)
    draft_expires_at = _utcnow() + timedelta(seconds=DRAFT_TTL_SECONDS)
    draft = CapitalMandateDraft(
        id=str(uuid.uuid4()),
        user_id=user_id,
        agent_id=input_obj.agent_id,
        base_mandate_id=base_mandate.id,
        canonical_payload=canonical_payload,
        challenge=challenge,
        expires_at=draft_expires_at,
        consumed=False,
        created_at=_utcnow(),
    )
    db.add(draft)
    await db.commit()

    return CapitalMandateDraftCreated(
        draft_id=draft.id,
        payload=payload,
        payload_summary=_build_payload_summary(payload),
        challenge_b64url=_b64url(challenge),
        expires_at_utc=draft_expires_at,
    )


async def submit_signed_capital_mandate(
    db: AsyncSession,
    *,
    user_id: str,
    draft_id: str,
    assertion: WebAuthnAssertionPayload,
) -> CapitalMandateSubmitResult:
    draft = await db.scalar(
        select(CapitalMandateDraft)
        .where(CapitalMandateDraft.id == draft_id)
        .where(CapitalMandateDraft.user_id == user_id)
        .with_for_update()
    )
    if draft is None:
        raise CapitalMandateDraftNotFound(draft_id)
    if draft.consumed:
        raise CapitalMandateDraftConsumed(draft_id)
    if draft.expires_at < _utcnow():
        raise CapitalMandateDraftExpired(draft_id)

    user, agent, base_mandate = await _load_user_agent_base_mandate(
        db, user_id=user_id, agent_id=draft.agent_id
    )
    if base_mandate.id != draft.base_mandate_id:
        raise BaseMandateRequired("capital mandate draft is bound to another base mandate")
    await _ensure_no_active_capital_mandate(
        db, user_id=user_id, agent_id=draft.agent_id
    )

    try:
        verified = verify_authentication_response(
            credential=assertion.model_dump(by_alias=True),
            expected_challenge=bytes(draft.challenge),
            expected_rp_id=settings.webauthn_rp_id,
            expected_origin=settings.webauthn_origin,
            credential_public_key=_b64url_decode(user.passkey_pubkey),
            credential_current_sign_count=user.passkey_sign_count or 0,
            require_user_verification=False,
        )
    except Exception as exc:
        log.info(
            "capital_mandate.webauthn_failed",
            user_id=user_id,
            draft_id=draft_id,
            error=type(exc).__name__,
        )
        raise CapitalMandateWebAuthnFailed(str(exc)) from exc

    user.passkey_sign_count = verified.new_sign_count
    user.last_active_at = _utcnow()
    payload = json.loads(bytes(draft.canonical_payload).decode("utf-8"))
    starts_at = _parse_iso_z(payload["starts_at"])
    expires_at = _parse_iso_z(payload["expires_at"])
    now = _utcnow()
    capital_mandate = CapitalMandate(
        id=payload["mandate_id"],
        user_id=user.id,
        agent_id=agent.id,
        base_mandate_id=base_mandate.id,
        status=STATUS_ACTIVE,
        budget_total_cents=payload["budget_total_cents"],
        currency=payload["currency"],
        starts_at=starts_at,
        expires_at=expires_at,
        duration_days=payload["duration_days"],
        max_single_purchase_cents=payload["max_single_purchase_cents"],
        max_open_positions=payload["max_open_positions"],
        max_daily_deals=payload.get("max_daily_deals"),
        min_expected_margin_bps=payload["min_expected_margin_bps"],
        max_total_loss_cents=payload.get("max_total_loss_cents"),
        risk_level=payload["risk_level"],
        auto_buy=payload["auto_buy"],
        auto_sell=payload["auto_sell"],
        auto_relist=payload["auto_relist"],
        requires_manual_approval=False,
        allowed_categories=payload["allowed_categories"],
        forbidden_categories=payload["forbidden_categories"],
        geo_scope=payload["geo_scope"],
        constraints=payload["constraints"],
        signature=_signature_blob(assertion),
        canonical_payload=bytes(draft.canonical_payload).decode("utf-8"),
        created_at=now,
        activated_at=now,
    )
    db.add(capital_mandate)
    draft.consumed = True

    await audit_service.log_intent_event(
        db,
        user_id=user.id,
        agent_id=agent.id,
        mandate_id=base_mandate.id,
        action="capital_mandate_activated",
        params={"capital_mandate_id": capital_mandate.id},
        result={
            "status": STATUS_ACTIVE,
            "budget_total_cents": capital_mandate.budget_total_cents,
            "expires_at": _iso_z(expires_at),
        },
        success=True,
    )
    await db.commit()

    budget_state = await capital_ledger_service.compute_budget_state(
        db, capital_mandate_id=capital_mandate.id
    )
    return CapitalMandateSubmitResult(
        capital_mandate_id=capital_mandate.id,
        status=capital_mandate.status,
        budget_state=budget_state.to_dict(),
        expires_at=capital_mandate.expires_at,
    )


async def expire_old_capital_mandates(db: AsyncSession) -> int:
    now = _utcnow()
    rows = list(
        await db.scalars(
            select(CapitalMandate)
            .where(CapitalMandate.status.in_([STATUS_ACTIVE, STATUS_PAUSED]))
            .where(CapitalMandate.expires_at < now)
        )
    )
    for row in rows:
        row.status = STATUS_EXPIRED
    if rows:
        await db.commit()
    return len(rows)


async def get_active_capital_mandate(
    db: AsyncSession, *, user_id: str, agent_id: str | None = None
) -> ActiveCapitalMandateResult:
    await expire_old_capital_mandates(db)
    filters = [
        CapitalMandate.user_id == user_id,
        CapitalMandate.status.in_([STATUS_ACTIVE, STATUS_PAUSED]),
    ]
    if agent_id is not None:
        filters.append(CapitalMandate.agent_id == agent_id)
    mandate = await db.scalar(
        select(CapitalMandate)
        .where(*filters)
        .order_by(CapitalMandate.activated_at.desc())
    )
    if mandate is None:
        return ActiveCapitalMandateResult(None, None, None)

    budget_state = await capital_ledger_service.compute_budget_state(
        db, capital_mandate_id=mandate.id
    )
    positions = list(
        await db.scalars(
            select(CapitalPosition).where(
                CapitalPosition.capital_mandate_id == mandate.id
            )
        )
    )
    open_count = sum(
        1 for p in positions if p.status not in {"sold", "cancelled", "closed_loss"}
    )
    return ActiveCapitalMandateResult(
        mandate=mandate,
        budget_state=budget_state.to_dict(),
        positions_summary={
            "total": len(positions),
            "open": open_count,
            "closed": len(positions) - open_count,
        },
    )


async def get_capital_mandate_for_user(
    db: AsyncSession, *, user_id: str, capital_mandate_id: str
) -> CapitalMandate:
    mandate = await db.get(CapitalMandate, capital_mandate_id)
    if mandate is None or mandate.user_id != user_id:
        raise CapitalMandateNotFound(capital_mandate_id)
    return mandate


async def pause_capital_mandate(
    db: AsyncSession, *, user_id: str, capital_mandate_id: str
) -> CapitalMandate:
    mandate = await get_capital_mandate_for_user(
        db, user_id=user_id, capital_mandate_id=capital_mandate_id
    )
    if mandate.status != STATUS_ACTIVE:
        raise CapitalMandateInvalidState(
            f"cannot pause capital mandate in status {mandate.status!r}"
        )
    mandate.status = STATUS_PAUSED
    mandate.paused_at = _utcnow()
    await audit_service.log_intent_event(
        db,
        user_id=user_id,
        agent_id=mandate.agent_id,
        mandate_id=mandate.base_mandate_id,
        action="capital_mandate_paused",
        params={"capital_mandate_id": mandate.id},
        result={"status": STATUS_PAUSED},
        success=True,
    )
    await db.commit()
    return mandate


async def resume_capital_mandate(
    db: AsyncSession, *, user_id: str, capital_mandate_id: str
) -> CapitalMandate:
    mandate = await get_capital_mandate_for_user(
        db, user_id=user_id, capital_mandate_id=capital_mandate_id
    )
    if mandate.status != STATUS_PAUSED:
        raise CapitalMandateInvalidState(
            f"cannot resume capital mandate in status {mandate.status!r}"
        )
    if mandate.expires_at < _utcnow():
        mandate.status = STATUS_EXPIRED
        await db.commit()
        raise CapitalMandateInvalidState("capital mandate is expired")
    mandate.status = STATUS_ACTIVE
    mandate.paused_at = None
    await audit_service.log_intent_event(
        db,
        user_id=user_id,
        agent_id=mandate.agent_id,
        mandate_id=mandate.base_mandate_id,
        action="capital_mandate_resumed",
        params={"capital_mandate_id": mandate.id},
        result={"status": STATUS_ACTIVE},
        success=True,
    )
    await db.commit()
    return mandate


async def revoke_capital_mandate(
    db: AsyncSession,
    *,
    user_id: str,
    capital_mandate_id: str,
    reason: str | None = None,
) -> CapitalMandate:
    mandate = await get_capital_mandate_for_user(
        db, user_id=user_id, capital_mandate_id=capital_mandate_id
    )
    if mandate.status in (STATUS_REVOKED, STATUS_SETTLED):
        return mandate
    mandate.status = STATUS_REVOKED
    mandate.revoked_at = _utcnow()
    mandate.revocation_reason = reason or "user_revoked"
    await audit_service.log_intent_event(
        db,
        user_id=user_id,
        agent_id=mandate.agent_id,
        mandate_id=mandate.base_mandate_id,
        action="capital_mandate_revoked",
        params={"capital_mandate_id": mandate.id},
        result={
            "status": STATUS_REVOKED,
            "revocation_reason": mandate.revocation_reason,
        },
        success=True,
    )
    await db.commit()
    return mandate


async def list_ledger_entries_for_user(
    db: AsyncSession, *, user_id: str, capital_mandate_id: str
) -> list[CapitalLedgerEntry]:
    await get_capital_mandate_for_user(
        db, user_id=user_id, capital_mandate_id=capital_mandate_id
    )
    return list(
        await db.scalars(
            select(CapitalLedgerEntry)
            .where(CapitalLedgerEntry.capital_mandate_id == capital_mandate_id)
            .order_by(CapitalLedgerEntry.created_at.desc())
        )
    )


async def list_positions_for_user(
    db: AsyncSession, *, user_id: str, capital_mandate_id: str
) -> list[CapitalPosition]:
    await get_capital_mandate_for_user(
        db, user_id=user_id, capital_mandate_id=capital_mandate_id
    )
    return list(
        await db.scalars(
            select(CapitalPosition)
            .where(CapitalPosition.capital_mandate_id == capital_mandate_id)
            .order_by(CapitalPosition.created_at.desc())
        )
    )
