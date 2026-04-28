"""Mandate service — Tier 2 upgrade via WebAuthn-signed mandate (brief task 2.4).

Two stages with a stateless seam between them, mirroring the auth-service
pattern from 2.1/2.2 but with a draft-row instead of a JWT:

  /draft  ⇒ create_draft(...)            → MandateDraft row + canonical_payload
  /submit ⇒ submit_signed_mandate(...)   → Mandate row, agent active, tier=2

The bytes the user's passkey signs are JCS-canonicalized (RFC 8785) so a
re-canonicalization at any future point yields the same digest. The
draft's `challenge` doubles as the WebAuthn assertion challenge.

Public surface:
  - DraftCreated, MandateSubmitResult       — return models
  - MandateError (+ subclasses)             — typed errors with code+http_status
  - WebAuthnAssertionPayload                — assertion shape from the mobile SDK
  - create_draft(db, user_id, ...)          → DraftCreated
  - submit_signed_mandate(db, user_id, ...) → MandateSubmitResult

V0 simplifying assumptions (DESIGN_QUESTIONS DQ-17, DQ-18):
- One active mandate per agent. Re-mandating requires revocation (2.5).
- The mandate `version` is fixed to "1.0" in V0; bumping is a code change.
- `scope.allowed_actions` / `forbidden_actions` / `step_up_required_for`
  / forbidden categories are server-fixed (V0_DEFAULT_*). The user only
  picks limits, geo, expiry, and the optional categories whitelist.
"""
from __future__ import annotations

import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from webauthn import verify_authentication_response

from app.core import canonicalization, platform_limits as pl
from app.core.config import settings
from app.core.logging import log
from app.models.schema import Agent, Mandate, MandateDraft, User
from app.services import audit_service
from app.services.auth_service import _b64url, _b64url_decode


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class MandateError(Exception):
    code: str = "mandate_error"
    http_status: int = 400


class UserNotFound(MandateError):
    code = "user_not_found"
    http_status = 404


class AgentNotOwned(MandateError):
    code = "agent_not_owned"
    http_status = 404


class AgentInWrongState(MandateError):
    code = "agent_in_wrong_state"
    http_status = 409


class LimitsExceedPlatformCap(MandateError):
    code = "limits_exceed_platform_cap"
    http_status = 422


class InvalidGeoScope(MandateError):
    code = "invalid_geo_scope"
    http_status = 422


class InvalidExpiryWindow(MandateError):
    code = "invalid_expiry_window"
    http_status = 422


class InvalidTierTransition(MandateError):
    code = "invalid_tier_transition"
    http_status = 409


class DraftNotFound(MandateError):
    code = "draft_not_found"
    http_status = 404


class DraftExpired(MandateError):
    code = "draft_expired"
    http_status = 410


class DraftAlreadyConsumed(MandateError):
    code = "draft_already_consumed"
    http_status = 409


class WebAuthnVerificationFailed(MandateError):
    code = "webauthn_verification_failed"
    http_status = 422


# ---------------------------------------------------------------------------
# Input / output models
# ---------------------------------------------------------------------------


class DraftLimitsInput(BaseModel):
    """User-modifiable subset of mandate limits.

    All fields optional → omit to take the V0 default. Server validates
    each against the platform hard cap and raises 422 if exceeded.
    """

    max_price_per_deal_eur: int | None = Field(default=None, ge=1)
    max_total_volume_eur_per_mandate: int | None = Field(default=None, ge=1)
    max_total_volume_eur_per_day: int | None = Field(default=None, ge=1)
    max_deals_per_day: int | None = Field(default=None, ge=1)
    max_active_intents: int | None = Field(default=None, ge=1)
    max_concurrent_negotiations: int | None = Field(default=None, ge=1)


class DraftConstraintsInput(BaseModel):
    """User-modifiable subset of mandate constraints."""

    geo_scope: list[str] | None = Field(default=None)


class WebAuthnAssertionPayload(BaseModel):
    """py-webauthn assertion shape — passed verbatim to the verifier."""

    id: str
    raw_id: str = Field(alias="rawId")
    type: str = "public-key"
    response: dict[str, Any]

    model_config = {"populate_by_name": True}


@dataclass
class DraftCreated:
    draft_id: str
    payload: dict[str, Any]
    payload_summary: dict[str, Any]
    challenge_b64url: str
    expires_at_utc: datetime


@dataclass
class MandateSubmitResult:
    mandate_id: str
    agent_id: str
    agent_status: str
    expires_at: datetime
    new_access_token: str
    next_step: dict[str, Any]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_ITALIAN_MONTHS = (
    "gennaio",
    "febbraio",
    "marzo",
    "aprile",
    "maggio",
    "giugno",
    "luglio",
    "agosto",
    "settembre",
    "ottobre",
    "novembre",
    "dicembre",
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _utcnow_naive() -> datetime:
    """Naive UTC for the legacy DateTime columns on mandates / agents / users."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _format_italian_date(dt: datetime) -> str:
    return f"{dt.day} {_ITALIAN_MONTHS[dt.month - 1]} {dt.year}"


def _iso_z(dt: datetime) -> str:
    """ISO-8601 with Z suffix; deterministic for canonicalization."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _resolve_limits(user_limits: DraftLimitsInput) -> dict[str, int]:
    """Apply V0 defaults then check against platform hard caps."""
    candidates = {
        "max_price_per_deal_eur": (
            user_limits.max_price_per_deal_eur
            or pl.DEFAULT_MAX_PRICE_PER_DEAL_EUR,
            pl.MAX_PRICE_PER_DEAL_EUR,
        ),
        "max_total_volume_eur_per_mandate": (
            user_limits.max_total_volume_eur_per_mandate
            or pl.DEFAULT_MAX_TOTAL_VOLUME_EUR_PER_MANDATE,
            pl.MAX_TOTAL_VOLUME_EUR_PER_MANDATE,
        ),
        "max_total_volume_eur_per_day": (
            user_limits.max_total_volume_eur_per_day
            or pl.DEFAULT_MAX_TOTAL_VOLUME_EUR_PER_DAY,
            pl.MAX_TOTAL_VOLUME_EUR_PER_DAY,
        ),
        "max_deals_per_day": (
            user_limits.max_deals_per_day or pl.DEFAULT_MAX_DEALS_PER_DAY,
            pl.MAX_DEALS_PER_DAY,
        ),
        "max_active_intents": (
            user_limits.max_active_intents or pl.DEFAULT_MAX_ACTIVE_INTENTS,
            pl.MAX_ACTIVE_INTENTS,
        ),
        "max_concurrent_negotiations": (
            user_limits.max_concurrent_negotiations
            or pl.DEFAULT_MAX_CONCURRENT_NEGOTIATIONS,
            pl.MAX_CONCURRENT_NEGOTIATIONS,
        ),
    }
    resolved: dict[str, int] = {}
    for name, (value, cap) in candidates.items():
        if value > cap:
            raise LimitsExceedPlatformCap(
                f"{name}={value} exceeds platform cap {cap}"
            )
        resolved[name] = value
    return resolved


def _resolve_constraints(
    user_constraints: DraftConstraintsInput,
) -> dict[str, Any]:
    geo = user_constraints.geo_scope or list(pl.GEO_SCOPE_V0)
    invalid = set(geo) - set(pl.GEO_SCOPE_V0)
    if invalid:
        raise InvalidGeoScope(
            f"geo_scope contains values outside V0 set: {sorted(invalid)}"
        )
    return {
        "geo_scope": list(geo),
        "categories_allowed": list(pl.V0_DEFAULT_CATEGORIES_ALLOWED),
        "categories_forbidden": list(pl.HARD_FORBIDDEN_CATEGORIES),
        "operating_hours": pl.V0_DEFAULT_OPERATING_HOURS,
    }


def _resolve_expires_at(expires_in_days: int | None) -> datetime:
    days = expires_in_days or pl.DEFAULT_MANDATE_DURATION_DAYS
    if days < 1 or days > pl.MAX_MANDATE_DURATION_DAYS:
        raise InvalidExpiryWindow(
            f"expires_in_days={days} outside [1, {pl.MAX_MANDATE_DURATION_DAYS}]"
        )
    return _utcnow() + timedelta(days=days)


def _build_payload(
    *,
    mandate_id: str,
    user: User,
    agent: Agent,
    issued_at: datetime,
    expires_at: datetime,
    limits: dict[str, int],
    constraints: dict[str, Any],
    challenge_hex: str,
) -> dict[str, Any]:
    """Assemble the canonical payload dict (pre-canonicalization).

    Field order in this dict doesn't matter — JCS will sort keys
    lexicographically when serializing.
    """
    return {
        "version": pl.MANDATE_SPEC_VERSION,
        "mandate_id": mandate_id,
        "principal": {
            "user_id": user.id,
            "nullifier_hash": user.nullifier_hash,
            "tier": user.tier,
        },
        "agent": {
            "agent_id": agent.id,
            "pubkey": agent.pubkey,
        },
        "issued_at": _iso_z(issued_at),
        "expires_at": _iso_z(expires_at),
        "scope": {
            "allowed_actions": list(pl.V0_DEFAULT_ALLOWED_ACTIONS),
            "forbidden_actions": list(pl.V0_DEFAULT_FORBIDDEN_ACTIONS),
        },
        "limits": limits,
        "step_up_required_for": [dict(s) for s in pl.V0_DEFAULT_STEP_UP_REQUIRED_FOR],
        "constraints": constraints,
        "revocation": dict(pl.REVOCATION_POLICY_V0),
        "challenge": challenge_hex,
    }


def _build_payload_summary(payload: dict[str, Any]) -> dict[str, Any]:
    """Italian, mobile-friendly recap of the mandate the user is about to sign."""
    limits = payload["limits"]
    expires_at = datetime.strptime(payload["expires_at"], "%Y-%m-%dT%H:%M:%SZ")
    geo = payload["constraints"]["geo_scope"]
    geo_label = "Italia" if geo == ["IT"] else ", ".join(geo)
    return {
        "human_readable": (
            f"Il tuo agente potrà spendere fino a "
            f"€{limits['max_price_per_deal_eur']} per singolo deal, "
            f"massimo €{limits['max_total_volume_eur_per_mandate']} totali, "
            f"fino a {limits['max_deals_per_day']} deal al giorno. "
            f"Configurazione valida fino al "
            f"{_format_italian_date(expires_at)}."
        ),
        "key_fields": [
            {
                "label": "Spesa massima per deal",
                "value": f"€{limits['max_price_per_deal_eur']}",
            },
            {
                "label": "Spesa totale",
                "value": f"€{limits['max_total_volume_eur_per_mandate']}",
            },
            {
                "label": "Deal al giorno",
                "value": str(limits["max_deals_per_day"]),
            },
            {"label": "Geo", "value": geo_label},
            {
                "label": "Scadenza",
                "value": _format_italian_date(expires_at),
            },
        ],
    }


# ---------------------------------------------------------------------------
# create_draft
# ---------------------------------------------------------------------------


# Drafts live for 5 minutes — short enough that a stale token can't be
# replayed days later, long enough that a slow biometric flow on the
# device works.
DRAFT_TTL_SECONDS = 300


async def create_draft(
    db: AsyncSession,
    *,
    user_id: str,
    agent_id: str,
    user_limits: DraftLimitsInput,
    user_constraints: DraftConstraintsInput,
    expires_in_days: int | None,
) -> DraftCreated:
    """Create a pending mandate draft for `user_id` over `agent_id`.

    Validates ownership, agent state, limits-vs-platform-cap, geo-scope,
    expiry window. Persists `MandateDraft` row carrying the canonical
    bytes the user's passkey will sign.
    """
    user = await db.scalar(select(User).where(User.id == user_id))
    if user is None:
        raise UserNotFound(user_id)
    if user.tier >= 2:
        raise InvalidTierTransition(
            f"user already at tier {user.tier}; revoke before re-mandating"
        )

    agent = await db.scalar(select(Agent).where(Agent.id == agent_id))
    if agent is None or agent.user_id != user_id:
        raise AgentNotOwned(agent_id)
    if agent.status != "pending_mandate":
        raise AgentInWrongState(
            f"agent.status={agent.status!r}; expected 'pending_mandate'"
        )

    limits = _resolve_limits(user_limits)
    constraints = _resolve_constraints(user_constraints)
    expires_at = _resolve_expires_at(expires_in_days)
    issued_at = _utcnow()

    mandate_id = str(uuid.uuid4())
    challenge_bytes = secrets.token_bytes(32)
    payload = _build_payload(
        mandate_id=mandate_id,
        user=user,
        agent=agent,
        issued_at=issued_at,
        expires_at=expires_at,
        limits=limits,
        constraints=constraints,
        challenge_hex=challenge_bytes.hex(),
    )
    canonical_bytes = canonicalization.canonicalize(payload)

    draft_expires_at = _utcnow() + timedelta(seconds=DRAFT_TTL_SECONDS)
    draft = MandateDraft(
        user_id=user_id,
        agent_id=agent_id,
        canonical_payload=canonical_bytes,
        challenge=challenge_bytes,
        expires_at=draft_expires_at.replace(tzinfo=None),
        consumed=False,
        created_at=_utcnow_naive(),
    )
    db.add(draft)
    await db.flush()
    draft_id = draft.id
    await db.commit()

    return DraftCreated(
        draft_id=draft_id,
        payload=payload,
        payload_summary=_build_payload_summary(payload),
        challenge_b64url=_b64url(challenge_bytes),
        expires_at_utc=draft_expires_at,
    )


# ---------------------------------------------------------------------------
# submit_signed_mandate
# ---------------------------------------------------------------------------


def _signature_blob(assertion: WebAuthnAssertionPayload) -> dict[str, Any]:
    """Persist-friendly serialization of the user's WebAuthn assertion.

    Stored on `Mandate.signature` (JSONB) so an auditor can later replay
    the verification entirely from the DB row.
    """
    return {
        "algorithm": "webauthn",
        "credential_id": assertion.id,
        "raw_id": assertion.raw_id,
        "response": dict(assertion.response),
    }


async def submit_signed_mandate(
    db: AsyncSession,
    *,
    user_id: str,
    draft_id: str,
    assertion: WebAuthnAssertionPayload,
) -> MandateSubmitResult:
    """Verify assertion against the draft's challenge; persist mandate; tier up.

    Sequence:
      1. SELECT draft FOR UPDATE (lock vs concurrent submit)
      2. Reject if expired or already consumed
      3. Load user + agent (fresh state)
      4. Verify WebAuthn assertion (challenge=draft.challenge, pubkey=user.passkey)
      5. Bump passkey sign_count
      6. Build Mandate row from the canonical_payload bytes
      7. Activate agent (pending_mandate → active)
      8. Tier up user (1 → 2; defensive guard for 2 → 2 race)
      9. Mark draft consumed
      10. Commit
      11. Audit log + new access token
    """
    draft = await db.scalar(
        select(MandateDraft)
        .where(MandateDraft.id == draft_id)
        .where(MandateDraft.user_id == user_id)
        .with_for_update()
    )
    if draft is None:
        raise DraftNotFound(draft_id)
    if draft.consumed:
        raise DraftAlreadyConsumed(draft_id)
    if draft.expires_at < _utcnow_naive():
        raise DraftExpired(draft_id)

    user = await db.scalar(select(User).where(User.id == user_id))
    if user is None:
        raise UserNotFound(user_id)
    if user.tier >= 2:
        raise InvalidTierTransition(
            f"user already at tier {user.tier}; cannot re-sign mandate in V0"
        )

    agent = await db.scalar(select(Agent).where(Agent.id == draft.agent_id))
    if agent is None or agent.user_id != user_id:
        raise AgentNotOwned(draft.agent_id)
    if agent.status != "pending_mandate":
        raise AgentInWrongState(
            f"agent.status={agent.status!r}; expected 'pending_mandate'"
        )

    # WebAuthn verify — challenge is the draft's random 32 bytes.
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
            "mandate.webauthn_failed",
            user_id=user_id,
            draft_id=draft_id,
            error=type(exc).__name__,
        )
        raise WebAuthnVerificationFailed(str(exc)) from exc

    user.passkey_sign_count = verified.new_sign_count
    user.last_active_at = _utcnow_naive()

    # Re-canonicalize to a dict so we can read fields back out without
    # touching `jcs.deserialize` (which is just `json.loads` on UTF-8 bytes).
    import json

    payload = json.loads(bytes(draft.canonical_payload).decode("utf-8"))
    issued_at_dt = datetime.strptime(payload["issued_at"], "%Y-%m-%dT%H:%M:%SZ")
    expires_at_dt = datetime.strptime(payload["expires_at"], "%Y-%m-%dT%H:%M:%SZ")

    mandate = Mandate(
        id=payload["mandate_id"],
        agent_id=agent.id,
        user_id=user.id,
        version=payload["version"],
        scope=payload["scope"],
        limits=payload["limits"],
        step_up_required_for=payload["step_up_required_for"],
        constraints=payload["constraints"],
        spent_total_eur=0,
        deals_count=0,
        spent_today_eur=0,
        last_reset_date=_utcnow_naive(),
        issued_at=issued_at_dt,
        expires_at=expires_at_dt,
        signature=_signature_blob(assertion),
        canonical_payload=bytes(draft.canonical_payload).decode("utf-8"),
    )
    db.add(mandate)

    agent.status = "active"
    user.tier = 2
    draft.consumed = True

    try:
        await db.commit()
    except Exception:
        await db.rollback()
        raise

    await audit_service.log_mandate_signed(
        user_id=user.id,
        mandate_id=mandate.id,
        agent_id=agent.id,
    )

    from app.core.security import create_access_token

    new_access_token = create_access_token(user_id=user.id, tier=2)

    return MandateSubmitResult(
        mandate_id=mandate.id,
        agent_id=agent.id,
        agent_status=agent.status,
        expires_at=expires_at_dt,
        new_access_token=new_access_token,
        next_step={
            "action": "create_first_intent",
            "endpoint": "/api/intents",
        },
    )
