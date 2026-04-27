"""MandateVerifier smoke tests (brief task 1.3).

Two minimal cases that validate the test infrastructure (Postgres+pgvector
container, db_session rollback, factories) and that MandateVerifier wires up
end-to-end against a real DB. Full coverage of the verifier (scope KO,
constraints, step-up, expired, revoked, daily reset) lands in task 2.6.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models.schema import Agent, AuditLog, Mandate, User
from app.services.mandate_verifier import LimitExceeded, MandateVerifier


def _now() -> datetime:
    return datetime.utcnow()


def _make_user(db, label: str) -> User:
    user = User(
        nullifier_hash=f"test-nullifier-{label}",
        attributes_proven={"adult": True, "country": "IT", "valid": True},
        attributes_verified_at=_now(),
        attributes_expires_at=_now() + timedelta(days=365),
        passkey_credential_id=f"test-cred-{label}",
        passkey_pubkey=f"test-pk-{label}",
        passkey_sign_count=0,
        status="active",
        created_at=_now(),
        last_active_at=_now(),
    )
    db.add(user)
    db.flush()
    return user


def _make_agent(db, user_id: str, label: str) -> Agent:
    agent = Agent(
        user_id=user_id,
        name=f"Test {label}",
        pubkey=f"test-agent-pk-{label}",
        privkey_kms_ref=f"test-kms-{label}",
        status="active",
        created_at=_now(),
    )
    db.add(agent)
    db.flush()
    return agent


def _make_mandate(
    db,
    *,
    agent_id: str,
    user_id: str,
    max_price_per_deal_eur: int,
    allowed_actions: list[str] | None = None,
) -> Mandate:
    mandate = Mandate(
        agent_id=agent_id,
        user_id=user_id,
        version="1.0",
        scope={
            "allowed_actions": allowed_actions
            or ["send_offer", "send_counter_offer", "accept_offer"],
            "forbidden_actions": [],
        },
        limits={
            "max_price_per_deal_eur": max_price_per_deal_eur,
            "max_total_volume_eur_per_day": 5000,
            "max_total_volume_eur_per_mandate": 100_000,
            "max_deals_per_day": 10,
        },
        step_up_required_for=[],
        constraints={
            "geo_scope": ["IT"],
            "categories_allowed": ["*"],
            "categories_forbidden": [],
        },
        spent_total_eur=Decimal("0"),
        deals_count=0,
        spent_today_eur=Decimal("0"),
        last_reset_date=_now(),
        issued_at=_now(),
        expires_at=_now() + timedelta(days=30),
        signature={"webauthn": "test-signature"},
        canonical_payload='{"test":true}',
    )
    db.add(mandate)
    db.flush()
    return mandate


@pytest.mark.db
def test_mandate_verifier_happy_path(db_session) -> None:
    """authorize() returns the active mandate when scope and limits permit."""
    user = _make_user(db_session, "happy")
    agent = _make_agent(db_session, user.id, "happy")
    mandate = _make_mandate(
        db_session,
        agent_id=agent.id,
        user_id=user.id,
        max_price_per_deal_eur=100,  # €100 per-deal cap
    )

    verifier = MandateVerifier(db_session)
    returned = verifier.authorize(
        agent.id,
        "send_offer",
        {"price_cents": 5_000},  # €50 — under the €100 cap
    )

    assert returned.id == mandate.id


@pytest.mark.db
def test_mandate_verifier_limit_exceeded(db_session) -> None:
    """A price above the per-deal cap raises LimitExceeded.

    Asserts the rejection path does not invoke record_usage(): the daily-spend
    counter must not move and no `success=True` audit row must exist.
    """
    user = _make_user(db_session, "over")
    agent = _make_agent(db_session, user.id, "over")
    mandate = _make_mandate(
        db_session,
        agent_id=agent.id,
        user_id=user.id,
        max_price_per_deal_eur=200,  # €200 per-deal cap
    )
    spent_before = mandate.spent_today_eur or Decimal("0")

    verifier = MandateVerifier(db_session)
    with pytest.raises(LimitExceeded):
        verifier.authorize(
            agent.id,
            "accept_offer",
            {"price_cents": 200_000},  # €2000 — well above the cap
        )

    db_session.refresh(mandate)
    assert (mandate.spent_today_eur or Decimal("0")) == spent_before

    success_rows = db_session.scalars(
        select(AuditLog)
        .where(AuditLog.agent_id == agent.id)
        .where(AuditLog.success.is_(True))
    ).all()
    assert success_rows == []
