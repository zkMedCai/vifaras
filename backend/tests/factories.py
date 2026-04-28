"""Test fixture factories — shared between revocation, step-up, refresh tests.

Both sync and async versions exist so tests against the §5 sync scaffold
(`tool_layer.py`, `MandateVerifier`) and tests against new async services
can use the same shape of seed data.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta
from typing import Any

from app.core import canonicalization
from app.models.schema import Agent, Mandate, User
from app.services.auth_service import _b64url


def fake_credential_id_bytes() -> bytes:
    return b"factory-credential-id-bytes"


def fake_pubkey_bytes() -> bytes:
    return b"factory-cose-encoded-pubkey"


def default_user_kwargs(*, tier: int, email: str) -> dict[str, Any]:
    """Field values needed for a User row at any tier ≥ 1.

    Tier-0 users would also pass these (the placeholder values get
    overwritten at tier 1, but they're sentinels per DQ-8). Tier ≥ 1
    means we have a real Self verification flag set."""
    now = datetime.utcnow()
    return {
        "tier": tier,
        "nullifier_hash": f"nullifier-{email}",
        "passkey_credential_id": _b64url(fake_credential_id_bytes()),
        "passkey_pubkey": _b64url(fake_pubkey_bytes()),
        "passkey_sign_count": 0,
        "notification_email": email,
        "status": "active",
        "created_at": now,
        "last_active_at": now,
        "attributes_proven": {
            "isAdult": True,
            "issuingState": "IT",
            "documentValid": True,
            "documentExpiry": "2030-04-15",
        },
        "attributes_verified_at": now,
        "attributes_expires_at": now + timedelta(days=365 * 5),
    }


def build_mandate_payload_dict(
    *,
    mandate_id: str,
    user: User,
    agent: Agent,
    issued_at: datetime,
    expires_at: datetime,
    step_up_rules: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a mandate JSON payload for fixtures.

    Mirrors the shape `mandate_service._build_payload` produces, so tests
    don't have to special-case format. `step_up_rules` defaults to V0.
    """
    from app.core import platform_limits as pl

    return {
        "version": pl.MANDATE_SPEC_VERSION,
        "mandate_id": mandate_id,
        "principal": {
            "user_id": user.id,
            "nullifier_hash": user.nullifier_hash,
            "tier": user.tier,
        },
        "agent": {"agent_id": agent.id, "pubkey": agent.pubkey},
        "issued_at": issued_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "expires_at": expires_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "scope": {
            "allowed_actions": list(pl.V0_DEFAULT_ALLOWED_ACTIONS),
            "forbidden_actions": list(pl.V0_DEFAULT_FORBIDDEN_ACTIONS),
        },
        "limits": {
            "max_price_per_deal_eur": 100,
            "max_total_volume_eur_per_mandate": 500,
            "max_total_volume_eur_per_day": 200,
            "max_deals_per_day": 3,
            "max_active_intents": 10,
            "max_concurrent_negotiations": 5,
        },
        "step_up_required_for": (
            step_up_rules
            if step_up_rules is not None
            else [dict(s) for s in pl.V0_DEFAULT_STEP_UP_REQUIRED_FOR]
        ),
        "constraints": {
            "geo_scope": ["IT"],
            "categories_allowed": ["*"],
            "categories_forbidden": list(pl.HARD_FORBIDDEN_CATEGORIES),
            "operating_hours": "24/7",
        },
        "revocation": dict(pl.REVOCATION_POLICY_V0),
        "challenge": "0" * 64,
    }


# ---------------------------------------------------------------------------
# Sync helpers (for tool_layer / MandateVerifier tests)
# ---------------------------------------------------------------------------


def setup_active_mandate_sync(
    db_session,
    *,
    email: str,
    step_up_rules: list[dict[str, Any]] | None = None,
    user_id: str | None = None,
    agent_id: str | None = None,
    mandate_id: str | None = None,
) -> tuple[str, str, str]:
    """Insert a tier-2 User + active Agent + active Mandate. Returns IDs.

    Sync session — used by tool_layer.ToolHandler tests.
    """
    user_id = user_id or str(uuid.uuid4())
    agent_id = agent_id or str(uuid.uuid4())
    mandate_id = mandate_id or str(uuid.uuid4())

    user = User(id=user_id, **default_user_kwargs(tier=2, email=email))
    db_session.add(user)
    db_session.flush()

    agent = Agent(
        id=agent_id,
        user_id=user_id,
        pubkey="factory-agent-pubkey",
        privkey_kms_ref="file:.secrets/agent_keys/factory.json",
        status="active",
        created_at=datetime.utcnow(),
    )
    db_session.add(agent)
    db_session.flush()

    issued_at = datetime.utcnow()
    expires_at = issued_at + timedelta(days=30)
    payload = build_mandate_payload_dict(
        mandate_id=mandate_id,
        user=user,
        agent=agent,
        issued_at=issued_at,
        expires_at=expires_at,
        step_up_rules=step_up_rules,
    )
    canonical = canonicalization.canonicalize(payload)
    mandate = Mandate(
        id=mandate_id,
        agent_id=agent_id,
        user_id=user_id,
        version="1.0",
        scope=payload["scope"],
        limits=payload["limits"],
        step_up_required_for=payload["step_up_required_for"],
        constraints=payload["constraints"],
        spent_total_eur=0,
        deals_count=0,
        spent_today_eur=0,
        last_reset_date=issued_at,
        issued_at=issued_at,
        expires_at=expires_at,
        signature={"algorithm": "factory", "credential_id": "factory"},
        canonical_payload=canonical.decode("utf-8"),
    )
    db_session.add(mandate)
    db_session.commit()
    return user_id, agent_id, mandate_id


# ---------------------------------------------------------------------------
# Async helpers (for revocation, step-up, refresh tests)
# ---------------------------------------------------------------------------


async def setup_active_mandate_async(
    db,  # AsyncSession
    *,
    email: str,
    step_up_rules: list[dict[str, Any]] | None = None,
    user_id: str | None = None,
    agent_id: str | None = None,
    mandate_id: str | None = None,
) -> tuple[str, str, str]:
    user_id = user_id or str(uuid.uuid4())
    agent_id = agent_id or str(uuid.uuid4())
    mandate_id = mandate_id or str(uuid.uuid4())

    user = User(id=user_id, **default_user_kwargs(tier=2, email=email))
    db.add(user)
    await db.flush()

    agent = Agent(
        id=agent_id,
        user_id=user_id,
        pubkey="factory-agent-pubkey",
        privkey_kms_ref="file:.secrets/agent_keys/factory.json",
        status="active",
        created_at=datetime.utcnow(),
    )
    db.add(agent)
    await db.flush()

    issued_at = datetime.utcnow()
    expires_at = issued_at + timedelta(days=30)
    payload = build_mandate_payload_dict(
        mandate_id=mandate_id,
        user=user,
        agent=agent,
        issued_at=issued_at,
        expires_at=expires_at,
        step_up_rules=step_up_rules,
    )
    canonical = canonicalization.canonicalize(payload)
    mandate = Mandate(
        id=mandate_id,
        agent_id=agent_id,
        user_id=user_id,
        version="1.0",
        scope=payload["scope"],
        limits=payload["limits"],
        step_up_required_for=payload["step_up_required_for"],
        constraints=payload["constraints"],
        spent_total_eur=0,
        deals_count=0,
        spent_today_eur=0,
        last_reset_date=issued_at,
        issued_at=issued_at,
        expires_at=expires_at,
        signature={"algorithm": "factory", "credential_id": "factory"},
        canonical_payload=canonical.decode("utf-8"),
    )
    db.add(mandate)
    await db.commit()
    return user_id, agent_id, mandate_id


def fake_assertion_payload() -> dict[str, Any]:
    return {
        "id": _b64url(fake_credential_id_bytes()),
        "rawId": _b64url(fake_credential_id_bytes()),
        "type": "public-key",
        "response": {
            "authenticatorData": "factory-auth-data",
            "clientDataJSON": "factory-client-data",
            "signature": "factory-signature",
            "userHandle": "factory-user-handle",
        },
    }


# ---------------------------------------------------------------------------
# Granular sync factories (for `mandate_verifier` tests — task 2.6)
# ---------------------------------------------------------------------------
#
# Pattern: one factory per entity, with surgical override kwargs. Test
# bodies compose them — no all-or-nothing setup that obscures intent.


from decimal import Decimal


def make_user_sync(
    db,
    *,
    tier: int = 2,
    status: str = "active",
    email: str | None = None,
    label: str | None = None,
) -> User:
    """Insert a User row at the requested tier. Sync session."""
    user_id = str(uuid.uuid4())
    label = label or user_id[:8]
    email = email or f"factory-{label}@example.com"

    kwargs = default_user_kwargs(tier=tier, email=email)
    kwargs["status"] = status
    user = User(id=user_id, **kwargs)
    db.add(user)
    db.flush()
    return user


def make_agent_sync(
    db,
    *,
    user: User,
    status: str = "active",
    label: str | None = None,
) -> Agent:
    """Insert an Agent row owned by `user`. Sync session."""
    agent_id = str(uuid.uuid4())
    label = label or agent_id[:8]
    agent = Agent(
        id=agent_id,
        user_id=user.id,
        name=f"Factory {label}",
        pubkey=f"factory-pubkey-{label}",
        privkey_kms_ref=f"file:.secrets/agent_keys/{agent_id}.json",
        status=status,
        created_at=datetime.utcnow(),
    )
    db.add(agent)
    db.flush()
    return agent


def make_mandate_sync(
    db,
    *,
    user: User,
    agent: Agent,
    scope_overrides: dict[str, Any] | None = None,
    limits_overrides: dict[str, Any] | None = None,
    step_up_overrides: list[dict[str, Any]] | None = None,
    constraints_overrides: dict[str, Any] | None = None,
    expires_in_days: int = 30,
    revoked: bool = False,
    expired: bool = False,
    issued_offset_days: int = 0,
    spent_today_eur: float = 0,
    spent_total_eur: float = 0,
    deals_count: int = 0,
    last_reset_date: datetime | None = None,
) -> Mandate:
    """Insert a Mandate row with surgical overrides for verifier tests.

    Defaults mirror V0_DEFAULT_*. Pass `expired=True` for an already-expired
    mandate, `revoked=True` for a revoked one. `issued_offset_days` shifts
    `issued_at` (negative = past) to test ordering / most-recent semantics.
    Sync session.
    """
    from app.core import platform_limits as pl

    scope = {
        "allowed_actions": list(pl.V0_DEFAULT_ALLOWED_ACTIONS),
        "forbidden_actions": list(pl.V0_DEFAULT_FORBIDDEN_ACTIONS),
    }
    if scope_overrides:
        scope.update(scope_overrides)

    limits = {
        "max_price_per_deal_eur": 100,
        "max_total_volume_eur_per_day": 200,
        "max_total_volume_eur_per_mandate": 500,
        "max_deals_per_day": 3,
        "max_active_intents": 10,
        "max_concurrent_negotiations": 5,
    }
    if limits_overrides:
        limits.update(limits_overrides)

    constraints = {
        "geo_scope": ["IT"],
        "categories_allowed": ["*"],
        "categories_forbidden": list(pl.HARD_FORBIDDEN_CATEGORIES),
        "operating_hours": "24/7",
    }
    if constraints_overrides:
        constraints.update(constraints_overrides)

    issued_at = datetime.utcnow() + timedelta(days=issued_offset_days)
    if expired:
        expires_at = issued_at - timedelta(days=1)
    else:
        expires_at = issued_at + timedelta(days=expires_in_days)

    mandate = Mandate(
        id=str(uuid.uuid4()),
        agent_id=agent.id,
        user_id=user.id,
        version="1.0",
        scope=scope,
        limits=limits,
        step_up_required_for=(
            step_up_overrides if step_up_overrides is not None else []
        ),
        constraints=constraints,
        spent_total_eur=Decimal(str(spent_total_eur)),
        deals_count=deals_count,
        spent_today_eur=Decimal(str(spent_today_eur)),
        last_reset_date=last_reset_date or issued_at,
        issued_at=issued_at,
        expires_at=expires_at,
        revoked_at=datetime.utcnow() if revoked else None,
        revocation_reason="test_revocation" if revoked else None,
        signature={"algorithm": "factory"},
        canonical_payload='{"test":true}',
    )
    db.add(mandate)
    db.flush()
    return mandate
