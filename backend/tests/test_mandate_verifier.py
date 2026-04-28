"""MandateVerifier full-coverage tests (brief task 2.6).

35+ tests organized by area of the verifier:

  1. _get_active_mandate (4)
  2. _check_scope (3)
  3. _check_constraints (6)
  4. _check_limits (8)
  5. _check_step_up (5)
  6. _reset_daily_counters_if_needed (2)
  7. record_usage (3)
  8. log_failed (2)
  9. helpers — _extract_price_eur + _extract_country (parametrized)

The verifier is sync (§5 scaffold). All tests use `db_session`.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select

from app.models.schema import AuditLog, Mandate
from app.services.mandate_verifier import (
    ActionNotAllowed,
    ConstraintViolation,
    LimitExceeded,
    MandateExpired,
    MandateVerifier,
    NoActiveMandate,
    StepUpRequired,
)
from .factories import make_agent_sync, make_mandate_sync, make_user_sync


# ---------------------------------------------------------------------------
# Fixture: a tier-2 user + active agent (mandate is per-test)
# ---------------------------------------------------------------------------


@pytest.fixture
def actor(db_session):
    """Pre-seeded actor: tier-2 user + active agent. No mandate yet."""
    user = make_user_sync(db_session, tier=2, label="actor")
    agent = make_agent_sync(db_session, user=user, label="actor")
    return user, agent


@pytest.fixture
def verifier(db_session):
    return MandateVerifier(db_session)


# ===========================================================================
# 1. _get_active_mandate (4 tests)
# ===========================================================================


@pytest.mark.db
def test_no_active_mandate_raises(db_session, verifier, actor) -> None:
    """No mandate row at all → NoActiveMandate."""
    _, agent = actor
    with pytest.raises(NoActiveMandate):
        verifier.authorize(agent.id, "send_offer", {"price_cents": 1_000})


@pytest.mark.db
def test_expired_mandate_raises(db_session, verifier, actor) -> None:
    """Mandate with `expires_at` in the past → MandateExpired."""
    user, agent = actor
    make_mandate_sync(db_session, user=user, agent=agent, expired=True)
    with pytest.raises(MandateExpired):
        verifier.authorize(agent.id, "send_offer", {"price_cents": 1_000})


@pytest.mark.db
def test_revoked_mandate_excluded_raises_no_active_mandate(
    db_session, verifier, actor
) -> None:
    """Revoked mandate is filtered out by the query → caller sees NoActiveMandate.

    The scaffold's `_get_active_mandate` filters `revoked_at IS NULL` in
    the SELECT, so a revoked-only state has no row to return. The
    user-visible failure mode is `NoActiveMandate`, not `MandateRevoked`
    (which is a defensive post-filter check, unreachable in practice).
    """
    user, agent = actor
    make_mandate_sync(db_session, user=user, agent=agent, revoked=True)
    with pytest.raises(NoActiveMandate):
        verifier.authorize(agent.id, "send_offer", {"price_cents": 1_000})


@pytest.mark.db
def test_returns_most_recent_active_mandate(
    db_session, verifier, actor
) -> None:
    """When several mandates exist, the most recent non-revoked is selected."""
    user, agent = actor
    # Old, revoked
    make_mandate_sync(
        db_session, user=user, agent=agent,
        issued_offset_days=-30, revoked=True,
    )
    # Mid, active (older than the newest)
    make_mandate_sync(
        db_session, user=user, agent=agent,
        issued_offset_days=-20,
    )
    # Newest, active — should be selected
    newest = make_mandate_sync(
        db_session, user=user, agent=agent,
        issued_offset_days=-10,
    )
    db_session.commit()

    selected = verifier.authorize(
        agent.id, "send_offer", {"price_cents": 1_000}
    )
    assert selected.id == newest.id


# ===========================================================================
# 2. _check_scope (3 tests)
# ===========================================================================


@pytest.mark.db
def test_action_in_forbidden_raises(db_session, verifier, actor) -> None:
    user, agent = actor
    make_mandate_sync(
        db_session, user=user, agent=agent,
        scope_overrides={
            "allowed_actions": ["send_offer"],
            "forbidden_actions": ["send_offer"],  # explicit forbid wins
        },
    )
    with pytest.raises(ActionNotAllowed):
        verifier.authorize(agent.id, "send_offer", {"price_cents": 1_000})


@pytest.mark.db
def test_action_not_in_allowed_raises(db_session, verifier, actor) -> None:
    user, agent = actor
    make_mandate_sync(
        db_session, user=user, agent=agent,
        scope_overrides={
            "allowed_actions": ["send_offer"],
            "forbidden_actions": [],
        },
    )
    with pytest.raises(ActionNotAllowed):
        verifier.authorize(agent.id, "accept_offer", {"price_cents": 1_000})


@pytest.mark.db
def test_action_in_allowed_passes(db_session, verifier, actor) -> None:
    user, agent = actor
    mandate = make_mandate_sync(db_session, user=user, agent=agent)
    returned = verifier.authorize(
        agent.id, "send_offer", {"price_cents": 1_000}
    )
    assert returned.id == mandate.id


# ===========================================================================
# 3. _check_constraints (6 tests)
# ===========================================================================


@pytest.mark.db
def test_geo_scope_match_passes(db_session, verifier, actor) -> None:
    user, agent = actor
    make_mandate_sync(db_session, user=user, agent=agent)
    verifier.authorize(
        agent.id, "send_offer",
        {"price_cents": 1_000, "location": "Roma, IT"},
    )


@pytest.mark.db
def test_geo_scope_mismatch_raises(db_session, verifier, actor) -> None:
    user, agent = actor
    make_mandate_sync(db_session, user=user, agent=agent)
    with pytest.raises(ConstraintViolation):
        verifier.authorize(
            agent.id, "send_offer",
            {"price_cents": 1_000, "location": "Paris, FR"},
        )


@pytest.mark.db
def test_geo_scope_no_location_in_params_passes(
    db_session, verifier, actor
) -> None:
    """`location` field absent → geo check skipped."""
    user, agent = actor
    make_mandate_sync(db_session, user=user, agent=agent)
    verifier.authorize(agent.id, "send_offer", {"price_cents": 1_000})


@pytest.mark.db
def test_category_forbidden_raises(db_session, verifier, actor) -> None:
    user, agent = actor
    make_mandate_sync(
        db_session, user=user, agent=agent,
        constraints_overrides={
            "geo_scope": ["IT"],
            "categories_allowed": ["*"],
            "categories_forbidden": ["weapons"],
            "operating_hours": "24/7",
        },
    )
    with pytest.raises(ConstraintViolation):
        verifier.authorize(
            agent.id, "create_intent",
            {"category": "weapons", "reservation_price_eur": 50},
        )


@pytest.mark.db
def test_category_allowed_wildcard_passes(db_session, verifier, actor) -> None:
    """`categories_allowed=["*"]` permits any non-forbidden category."""
    user, agent = actor
    make_mandate_sync(db_session, user=user, agent=agent)
    verifier.authorize(
        agent.id, "create_intent",
        {"category": "electronics", "reservation_price_eur": 50},
    )


@pytest.mark.db
def test_category_not_in_explicit_allowlist_raises(
    db_session, verifier, actor
) -> None:
    """Specific allowlist (no wildcard) rejects categories not in it."""
    user, agent = actor
    make_mandate_sync(
        db_session, user=user, agent=agent,
        constraints_overrides={
            "geo_scope": ["IT"],
            "categories_allowed": ["books"],  # specific
            "categories_forbidden": [],
            "operating_hours": "24/7",
        },
    )
    with pytest.raises(ConstraintViolation):
        verifier.authorize(
            agent.id, "create_intent",
            {"category": "electronics", "reservation_price_eur": 50},
        )


# ===========================================================================
# 4. _check_limits (8 tests)
# ===========================================================================


@pytest.mark.db
def test_per_deal_cap_exceeded_raises(db_session, verifier, actor) -> None:
    user, agent = actor
    make_mandate_sync(
        db_session, user=user, agent=agent,
        limits_overrides={"max_price_per_deal_eur": 200},
    )
    with pytest.raises(LimitExceeded):
        verifier.authorize(
            agent.id, "accept_offer", {"price_cents": 200_000}  # €2000
        )


@pytest.mark.db
def test_per_deal_cap_at_boundary_passes(db_session, verifier, actor) -> None:
    """Price exactly equal to cap is permitted (cap is inclusive)."""
    user, agent = actor
    make_mandate_sync(
        db_session, user=user, agent=agent,
        limits_overrides={"max_price_per_deal_eur": 100},
    )
    # €100 == cap, should pass
    verifier.authorize(agent.id, "send_offer", {"price_cents": 10_000})


@pytest.mark.db
def test_daily_volume_cap_exceeded_raises(db_session, verifier, actor) -> None:
    """spent_today + price > daily cap on accept_offer → LimitExceeded."""
    user, agent = actor
    make_mandate_sync(
        db_session, user=user, agent=agent,
        limits_overrides={
            "max_price_per_deal_eur": 1_000,
            "max_total_volume_eur_per_day": 200,
        },
        spent_today_eur=150,  # already spent €150 today
    )
    with pytest.raises(LimitExceeded):
        verifier.authorize(
            agent.id, "accept_offer", {"price_cents": 10_000}  # +€100 = €250 > €200
        )


@pytest.mark.db
def test_daily_volume_increments_correctly(db_session, verifier, actor) -> None:
    """record_usage moves spent_today_eur and deals_count."""
    user, agent = actor
    mandate = make_mandate_sync(
        db_session, user=user, agent=agent,
        limits_overrides={
            "max_price_per_deal_eur": 1_000,
            "max_total_volume_eur_per_day": 1_000,
            "max_deals_per_day": 10,
        },
    )
    db_session.commit()
    spent_before = Decimal(mandate.spent_today_eur or 0)

    # First accepted deal at €50
    verifier.authorize(agent.id, "accept_offer", {"price_cents": 5_000})
    verifier.record_usage(
        mandate, "accept_offer", {"price_cents": 5_000}, success=True
    )
    db_session.refresh(mandate)
    assert Decimal(mandate.spent_today_eur) == spent_before + Decimal("50")
    assert mandate.deals_count == 1


@pytest.mark.db
def test_mandate_total_cap_exceeded_raises(db_session, verifier, actor) -> None:
    user, agent = actor
    make_mandate_sync(
        db_session, user=user, agent=agent,
        limits_overrides={
            "max_price_per_deal_eur": 1_000,
            "max_total_volume_eur_per_day": 5_000,
            "max_total_volume_eur_per_mandate": 500,
        },
        spent_total_eur=450,  # already spent €450 total
    )
    with pytest.raises(LimitExceeded):
        verifier.authorize(
            agent.id, "accept_offer", {"price_cents": 10_000}  # +€100 = €550 > €500
        )


@pytest.mark.db
def test_deals_count_cap_per_day_exceeded(db_session, verifier, actor) -> None:
    user, agent = actor
    make_mandate_sync(
        db_session, user=user, agent=agent,
        limits_overrides={
            "max_price_per_deal_eur": 1_000,
            "max_deals_per_day": 3,
        },
        deals_count=3,  # already at cap
    )
    with pytest.raises(LimitExceeded):
        verifier.authorize(agent.id, "accept_offer", {"price_cents": 5_000})


@pytest.mark.db
def test_no_price_in_params_skips_price_checks(
    db_session, verifier, actor
) -> None:
    """An action without a price field bypasses price/volume gates."""
    user, agent = actor
    make_mandate_sync(
        db_session, user=user, agent=agent,
        limits_overrides={"max_price_per_deal_eur": 1},
        spent_today_eur=999_999,
    )
    # search_intents has no price; should pass even with absurd ledger.
    verifier.authorize(agent.id, "search_intents", {"query": "laptop"})


@pytest.mark.db
def test_action_not_price_relevant_skips_volume_checks(
    db_session, verifier, actor
) -> None:
    """send_offer with price_cents passes per-deal cap but not volume cap.

    Volume cap only applies to `accept_offer` / `create_deal`, never to
    offers in flight. send_offer with price > daily volume cap (but under
    per-deal cap) must pass.
    """
    user, agent = actor
    make_mandate_sync(
        db_session, user=user, agent=agent,
        limits_overrides={
            "max_price_per_deal_eur": 1_000,
            "max_total_volume_eur_per_day": 50,
        },
        spent_today_eur=40,
    )
    # send_offer at €100: above daily cap (€40+€100=€140 > €50) but
    # daily/total checks only fire on accept/create_deal.
    verifier.authorize(agent.id, "send_offer", {"price_cents": 10_000})


# ===========================================================================
# 5. _check_step_up (5 tests)
# ===========================================================================


@pytest.mark.db
def test_step_up_required_when_above_threshold(
    db_session, verifier, actor
) -> None:
    user, agent = actor
    make_mandate_sync(
        db_session, user=user, agent=agent,
        limits_overrides={"max_price_per_deal_eur": 1_000},
        step_up_overrides=[{"action": "accept_offer", "above_eur": 100}],
    )
    with pytest.raises(StepUpRequired):
        verifier.authorize(
            agent.id, "accept_offer", {"price_cents": 12_000}  # €120 > €100
        )


@pytest.mark.db
def test_step_up_passes_when_signature_present(
    db_session, verifier, actor
) -> None:
    """A truthy `step_up_signature` in params bypasses the gate."""
    user, agent = actor
    make_mandate_sync(
        db_session, user=user, agent=agent,
        limits_overrides={"max_price_per_deal_eur": 1_000},
        step_up_overrides=[{"action": "accept_offer", "above_eur": 100}],
    )
    verifier.authorize(
        agent.id, "accept_offer",
        {
            "price_cents": 12_000,
            "step_up_signature": {"algorithm": "webauthn", "blob": "..."},
        },
    )


@pytest.mark.db
def test_step_up_always_required(db_session, verifier, actor) -> None:
    """`always=True` rule fires regardless of price."""
    user, agent = actor
    make_mandate_sync(
        db_session, user=user, agent=agent,
        scope_overrides={
            "allowed_actions": ["modify_reservation_price"],
            "forbidden_actions": [],
        },
        step_up_overrides=[
            {"action": "modify_reservation_price", "always": True}
        ],
    )
    with pytest.raises(StepUpRequired):
        verifier.authorize(agent.id, "modify_reservation_price", {})


@pytest.mark.db
def test_step_up_below_threshold_passes(db_session, verifier, actor) -> None:
    user, agent = actor
    make_mandate_sync(
        db_session, user=user, agent=agent,
        limits_overrides={"max_price_per_deal_eur": 1_000},
        step_up_overrides=[{"action": "accept_offer", "above_eur": 100}],
    )
    # €80 < €100 threshold → no step-up needed
    verifier.authorize(agent.id, "accept_offer", {"price_cents": 8_000})


@pytest.mark.db
def test_step_up_no_threshold_no_always_passes(
    db_session, verifier, actor
) -> None:
    """A rule with neither `always` nor `above_eur` is a no-op."""
    user, agent = actor
    make_mandate_sync(
        db_session, user=user, agent=agent,
        limits_overrides={"max_price_per_deal_eur": 1_000},
        step_up_overrides=[{"action": "send_offer"}],  # no thresholds
    )
    verifier.authorize(agent.id, "send_offer", {"price_cents": 10_000})


@pytest.mark.db
def test_step_up_rule_for_different_action_is_skipped(
    db_session, verifier, actor
) -> None:
    """When a rule applies to action A, authorizing action B skips it (continue path)."""
    user, agent = actor
    make_mandate_sync(
        db_session, user=user, agent=agent,
        limits_overrides={"max_price_per_deal_eur": 1_000},
        step_up_overrides=[
            {"action": "accept_offer", "above_eur": 50},  # rule for accept_offer
        ],
    )
    # Authorizing send_offer (different action) doesn't trip the rule.
    verifier.authorize(agent.id, "send_offer", {"price_cents": 10_000})


# ===========================================================================
# 6. _reset_daily_counters_if_needed (2 tests)
# ===========================================================================


@pytest.mark.db
def test_counters_reset_on_new_day(db_session, verifier, actor) -> None:
    """last_reset_date in the past → counters zeroed, last_reset_date updated."""
    user, agent = actor
    yesterday_morning = datetime.utcnow().replace(
        hour=8, minute=0, second=0, microsecond=0
    ) - timedelta(days=1)
    mandate = make_mandate_sync(
        db_session, user=user, agent=agent,
        spent_today_eur=200,
        deals_count=5,
        last_reset_date=yesterday_morning,
    )
    db_session.commit()

    verifier.authorize(agent.id, "send_offer", {"price_cents": 1_000})
    db_session.refresh(mandate)
    assert Decimal(mandate.spent_today_eur) == Decimal("0")
    assert mandate.deals_count == 0
    assert mandate.last_reset_date.date() == date.today()


@pytest.mark.db
def test_counters_not_reset_same_day(db_session, verifier, actor) -> None:
    """last_reset_date today → counters preserved across calls."""
    user, agent = actor
    earlier_today = datetime.utcnow().replace(
        hour=0, minute=1, second=0, microsecond=0
    )
    mandate = make_mandate_sync(
        db_session, user=user, agent=agent,
        spent_today_eur=80,
        deals_count=1,
        last_reset_date=earlier_today,
    )
    db_session.commit()

    verifier.authorize(agent.id, "send_offer", {"price_cents": 1_000})
    db_session.refresh(mandate)
    assert Decimal(mandate.spent_today_eur) == Decimal("80")
    assert mandate.deals_count == 1


# ===========================================================================
# 7. record_usage (3 tests)
# ===========================================================================


@pytest.mark.db
def test_record_usage_increments_counters_on_success(
    db_session, verifier, actor
) -> None:
    user, agent = actor
    mandate = make_mandate_sync(
        db_session, user=user, agent=agent,
        limits_overrides={
            "max_price_per_deal_eur": 1_000,
            "max_total_volume_eur_per_day": 1_000,
            "max_deals_per_day": 10,
        },
    )
    db_session.commit()

    verifier.record_usage(
        mandate, "accept_offer", {"price_cents": 5_000},
        success=True, result={"deal_id": "fake"}
    )
    db_session.refresh(mandate)
    assert Decimal(mandate.spent_today_eur) == Decimal("50")
    assert Decimal(mandate.spent_total_eur) == Decimal("50")
    assert mandate.deals_count == 1


@pytest.mark.db
def test_record_usage_does_not_increment_on_failure(
    db_session, verifier, actor
) -> None:
    user, agent = actor
    mandate = make_mandate_sync(
        db_session, user=user, agent=agent,
        limits_overrides={"max_price_per_deal_eur": 1_000},
    )
    db_session.commit()
    spent_before = Decimal(mandate.spent_today_eur or 0)
    deals_before = mandate.deals_count or 0

    verifier.record_usage(
        mandate, "accept_offer", {"price_cents": 5_000},
        success=False, error_code="deal_already_taken",
    )
    db_session.refresh(mandate)
    assert Decimal(mandate.spent_today_eur) == spent_before
    assert mandate.deals_count == deals_before


@pytest.mark.db
def test_record_usage_writes_audit_log(db_session, verifier, actor) -> None:
    user, agent = actor
    mandate = make_mandate_sync(db_session, user=user, agent=agent)
    db_session.commit()

    verifier.record_usage(
        mandate, "send_offer", {"price_cents": 5_000, "match_id": "m1"},
        success=True, result={"negotiation_id": "n1"},
    )

    rows = db_session.scalars(
        select(AuditLog).where(AuditLog.agent_id == agent.id)
    ).all()
    assert len(rows) == 1
    row = rows[0]
    assert row.user_id == user.id
    assert row.mandate_id == mandate.id
    assert row.action == "send_offer"
    assert row.success is True
    assert row.params["match_id"] == "m1"
    assert row.result["negotiation_id"] == "n1"


# ===========================================================================
# 8. log_failed (2 tests) — see DQ-27 for the post-fix semantics
# ===========================================================================


@pytest.mark.db
def test_log_failed_with_active_mandate_writes_audit_log(
    db_session, verifier, actor
) -> None:
    """When a mandate is found, log_failed persists a full AuditLog row."""
    user, agent = actor
    mandate = make_mandate_sync(db_session, user=user, agent=agent)
    db_session.commit()

    err = ActionNotAllowed("send_email not allowed")
    verifier.log_failed(agent.id, "send_email", err)

    rows = db_session.scalars(
        select(AuditLog).where(AuditLog.agent_id == agent.id)
    ).all()
    assert len(rows) == 1
    row = rows[0]
    assert row.user_id == user.id
    assert row.mandate_id == mandate.id
    assert row.action == "send_email"
    assert row.success is False
    assert row.error_code == "action_not_allowed"


@pytest.mark.db
def test_log_failed_without_mandate_does_not_crash(
    db_session, verifier, actor
) -> None:
    """No mandate context → emits structlog event, NO AuditLog row written.

    The AuditLog table requires `user_id`/`mandate_id` NOT NULL. The
    scaffold's previous behavior (insert with NULLs) violated the schema.
    Per DQ-27 the fix: best-effort lookup; if no mandate, fall back to
    structlog. Test asserts no crash + no audit row.
    """
    _, agent = actor
    db_session.commit()

    err = NoActiveMandate("agent has no mandate")
    verifier.log_failed(agent.id, "send_offer", err)

    rows = db_session.scalars(
        select(AuditLog).where(AuditLog.agent_id == agent.id)
    ).all()
    assert rows == []


# ===========================================================================
# 9. Helpers (parametrized)
# ===========================================================================


@pytest.mark.parametrize(
    "params,expected",
    [
        ({"price_cents": 5_000}, Decimal("50")),
        ({"price_cents": 0}, Decimal("0")),
        ({"price_eur": 50}, Decimal("50")),
        ({"price_eur": "75.5"}, Decimal("75.5")),
        ({}, None),
        ({"other_field": "value"}, None),
    ],
)
def test_extract_price_eur(params: dict[str, Any], expected: Decimal | None) -> None:
    assert MandateVerifier._extract_price_eur(params) == expected


@pytest.mark.parametrize(
    "location,expected",
    [
        ("Roma, IT", "IT"),
        ("Milan, IT", "IT"),
        ("paris, fr", "FR"),  # uppercased
        ("Roma", None),  # no comma
        ("Roma,", None),  # trailing comma → empty part, len != 2
        ("Roma, ITA", None),  # 3-letter country
    ],
)
def test_extract_country(location: str, expected: str | None) -> None:
    assert MandateVerifier._extract_country(location) == expected
