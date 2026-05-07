"""Audit service — structured-log + AuditLog table emitters (brief tasks 2.3, 4.1, 4.2).

Two complementary audit channels coexist in this codebase:

1. **`AuditLog` table** (schema.py): per-marketplace-action records with
   `(user_id, agent_id?, mandate_id?, action, params, result, success)`.
   Post-4.1, `agent_id` and `mandate_id` are nullable — the table now
   covers both agent-under-mandate actions (5.x onward) AND user-initiated
   marketplace actions before a mandate exists (intent CRUD at tier 0/1).
   Wired into `tool_layer` for agent actions, and `intent_service` for
   user-initiated CRUD. Always written via `log_intent_event` /
   `log_action` in this module so the call shape stays uniform.

2. **structlog audit events** (this module): identity / lifecycle events
   that don't fit the per-action shape (tier upgrades, mandate signed).
   Emitted as JSON to stdout under the `audit.*` event namespace so they
   can be pulled by log aggregators alongside the per-action audit table.

Routing rule of thumb:
  - "User clicked / agent invoked tool / something happened to an Intent
    or Negotiation or Deal" → `AuditLog` table.
  - "User's identity tier or mandate state transitioned" → structlog.

All functions are designed to **never raise**: audit emission must not
abort the upstream operation. If the table write or structlog fails, we
swallow + warn — the operation is already durable, the audit is secondary.
"""
from __future__ import annotations

from typing import Any, Final

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import log
from app.models.schema import AuditLog


# ---------------------------------------------------------------------------
# Action codes vocabulary (4.2 pre-emptive)
# ---------------------------------------------------------------------------
#
# Lowercase verb-noun, present-tense — matches the `AuditLog.action` schema
# comment example ("create_intent, send_offer, accept, ...") and aligns with
# the tool-action names in `platform_limits.V0_DEFAULT_ALLOWED_ACTIONS`. So
# `WHERE action='accept_offer'` returns BOTH user-initiated and agent-
# initiated occurrences of that action — one query, no UNION.
#
# Defined as nested namespaces so call sites read like
# `audit_service.IntentActions.CREATE`. Adding a new code is a code change;
# string-typed actions in callers are a drift vector we want to avoid.

class IntentActions:
    """Action codes for Intent CRUD (FASE 4.1)."""

    CREATE: Final[str] = "create_intent"
    UPDATE: Final[str] = "update_intent"
    CANCEL: Final[str] = "cancel_intent"
    EXPIRE: Final[str] = "expire_intent"  # scheduler-driven (FASE 6)


class MatchActions:
    """Action codes for Match lifecycle (FASE 4.3)."""

    CREATE: Final[str] = "create_match"
    SCORE_UPDATED: Final[str] = "update_match_score"
    EXPIRE: Final[str] = "expire_match"


class NegotiationActions:
    """Action codes for Negotiation lifecycle (FASE 5)."""

    START: Final[str] = "start_negotiation"
    SEND_OFFER: Final[str] = "send_offer"
    SEND_COUNTER_OFFER: Final[str] = "send_counter_offer"
    ACCEPT_OFFER: Final[str] = "accept_offer"
    REJECT_OFFER: Final[str] = "reject_offer"
    CAP: Final[str] = "cap_negotiation"
    COMPLETE: Final[str] = "complete_negotiation"
    EXPIRE: Final[str] = "expire_negotiation"
    CANCEL: Final[str] = "cancel_negotiation"


class DealActions:
    """Action codes for Deal lifecycle (FASE 5)."""

    CREATE: Final[str] = "create_deal"
    SIGN: Final[str] = "sign_deal"
    BUYER_SIGN: Final[str] = "buyer_sign_deal"
    SELLER_SIGN: Final[str] = "seller_sign_deal"
    CONFIRM: Final[str] = "confirm_deal"
    CHAT_UNLOCKED: Final[str] = "deal_chat_unlocked"
    TRADE_WINDOW_OPEN: Final[str] = "trade_window_open"
    TRADE_SHIPPING_MARKED: Final[str] = "trade_shipping_marked"
    TRADE_DELIVERED: Final[str] = "trade_delivered"
    TRADE_COMPLETED: Final[str] = "trade_completed"
    SHIPPING_METHOD_SELECTED: Final[str] = "shipping_method_selected"
    DISPUTE: Final[str] = "dispute_deal"
    COMPLETE: Final[str] = "complete_deal"
    CANCEL: Final[str] = "cancel_deal"
    EXPIRE: Final[str] = "expire_deal"
    SEND_MESSAGE: Final[str] = "send_message"


class AgentActions:
    """Action codes for orchestrator tick lifecycle (FASE 6.3)."""

    TICK_COMPLETED: Final[str] = "tick_completed"
    TICK_FAILED: Final[str] = "tick_failed"
    TICK_SKIPPED: Final[str] = "tick_skipped"


class SchedulerActions:
    """Action codes for the agent scheduler discovery cycle (FASE 6.3.c).

    Per-tick events are already covered by `AgentActions`; these record
    *meta* events about the scheduler itself — what it discovered, what
    it skipped, when caps fired. Aggregated, not per-agent.
    """

    DISCOVERY_RUN: Final[str] = "scheduler_discovery_run"
    DAILY_CAP_HIT: Final[str] = "scheduler_daily_cap_hit"
    RATE_LIMIT_HIT: Final[str] = "scheduler_rate_limit_hit"


class SecurityActions:
    """Action codes for security / abuse-detection events (FASE 7.1).

    These are signals worth a manual review pass — not lifecycle. Distinct
    from `SchedulerActions.RATE_LIMIT_HIT` (scheduler-side, per-agent
    pacing) since this family covers HTTP-side abuse signals visible at
    the request boundary.
    """

    RATE_LIMIT_API_HIT: Final[str] = "rate_limit_api_hit"
    MODERATION_REJECTED: Final[str] = "moderation_rejected"
    SEQUENTIAL_EMAIL_DETECTED: Final[str] = "sequential_email_detected"
    # Constant only at 7.1 — the failure-aggregation hook is V0.5+
    # (no real attacker pattern observed yet to calibrate threshold).
    BURST_LOGIN_ATTEMPTS: Final[str] = "burst_login_attempts"
    # 7.3.3: per-user daily cost cap reached → scheduler skipped a tick.
    # Anomaly signal worth review (recurrent hits = a stuck agent or a
    # user with abnormal usage that may need outreach).
    USER_COST_CAP_REACHED: Final[str] = "user_cost_cap_reached"
    # 7.4.2: refresh token reuse detection. A consumed (rotated) token was
    # presented again — likely compromise (token leaked + replayed). The
    # whole user token chain has been revoked; user must re-authenticate.
    REFRESH_TOKEN_REUSE: Final[str] = "refresh_token_reuse"


class AuthActions:
    """Action codes for authentication lifecycle events (FASE 7.1).

    Distinct from `SecurityActions` (anomaly signals) — this family is
    normal-flow auth lifecycle. Will grow with `LOGIN_COMPLETE`,
    `LOGOUT`, `REFRESH`, etc. when the corresponding hooks land.
    """

    REGISTER_COMPLETE: Final[str] = "register_complete"


async def log_tier_upgrade(
    *,
    user_id: str,
    from_tier: int,
    to_tier: int,
    nullifier_hash: str,
    agent_id: str | None,
) -> None:
    """Audit an identity tier transition. Never raises.

    Emits a structured `audit.tier_upgrade` event with all the fields a
    later compliance review would need to reconstruct the upgrade. No PII
    leaves the function: `nullifier_hash` is the same opaque hash already
    on `User.nullifier_hash`, and the agent_id / user_id are UUIDs.

    Called post-commit by the identity service so a structlog hiccup
    doesn't roll back the (already-durable) tier transition.
    """
    try:
        log.info(
            "audit.tier_upgrade",
            user_id=user_id,
            from_tier=from_tier,
            to_tier=to_tier,
            nullifier_hash=nullifier_hash,
            agent_id=agent_id,
        )
    except Exception as exc:
        # Never let audit failure break the caller; structlog is generally
        # bullet-proof but a misconfigured handler could throw on emit.
        try:
            log.warning(
                "audit.tier_upgrade.emit_failed",
                error=type(exc).__name__,
                message=str(exc),
            )
        except Exception:
            pass


async def log_mandate_signed(
    *,
    user_id: str,
    mandate_id: str,
    agent_id: str,
) -> None:
    """Audit a mandate signing event (tier 1 → 2 transition). Never raises.

    Like `log_tier_upgrade`, this uses the structlog channel: identity-
    lifecycle events stay on structlog even though the `AuditLog` table
    could now hold them with the relaxed FKs.
    """
    try:
        log.info(
            "audit.mandate_signed",
            user_id=user_id,
            mandate_id=mandate_id,
            agent_id=agent_id,
        )
    except Exception as exc:
        try:
            log.warning(
                "audit.mandate_signed.emit_failed",
                error=type(exc).__name__,
                message=str(exc),
            )
        except Exception:
            pass


async def log_intent_event(
    db: AsyncSession,
    *,
    user_id: str,
    action: str,
    params: dict[str, Any],
    result: dict[str, Any] | None = None,
    success: bool = True,
    error_code: str | None = None,
    agent_id: str | None = None,
    mandate_id: str | None = None,
) -> None:
    """Insert an `AuditLog` row for a user-initiated intent CRUD action.

    `action` is a verb-noun snake_case string aligned with the existing
    schema convention (`create_intent`, `update_intent`, `cancel_intent`).

    Most callers will pass `agent_id=None, mandate_id=None` because intent
    CRUD is exposed at tier 0 where no agent exists yet. Both are nullable
    post-4.1 migration.

    Never raises. The row is added + flushed but NOT committed — the
    caller controls transaction boundaries. If the flush fails (rare),
    we log + swallow rather than break the upstream service operation.
    """
    try:
        row = AuditLog(
            user_id=user_id,
            agent_id=agent_id,
            mandate_id=mandate_id,
            action=action,
            params=params,
            result=result,
            success=success,
            error_code=error_code,
        )
        db.add(row)
        await db.flush()
    except Exception as exc:
        try:
            log.warning(
                "audit.intent_event.write_failed",
                action=action,
                error=type(exc).__name__,
                message=str(exc),
            )
        except Exception:
            pass


async def log_agent_event(
    db: AsyncSession,
    *,
    user_id: str,
    agent_id: str,
    action: str,
    params: dict[str, Any],
    result: dict[str, Any] | None = None,
    success: bool = True,
    error_code: str | None = None,
    mandate_id: str | None = None,
) -> None:
    """Insert an `AuditLog` row for an orchestrator tick lifecycle event.

    Thin wrapper over `log_intent_event` with `agent_id` required (every
    tick has an agent). Used by the orchestrator (FASE 6.3) to record
    `tick_completed` / `tick_failed` / `tick_skipped` rows. Per-tool-call
    audit rows are still emitted by individual services through the
    `MandateVerifier.record_usage` path — this function is for the
    tick-meta event only, no double logging.

    Never raises (delegates to `log_intent_event`).
    """
    await log_intent_event(
        db,
        user_id=user_id,
        action=action,
        params=params,
        result=result,
        success=success,
        error_code=error_code,
        agent_id=agent_id,
        mandate_id=mandate_id,
    )


async def log_security_event(
    db: AsyncSession,
    *,
    action: str,
    user_id: str | None = None,
    actor_ip: str | None = None,
    params: dict[str, Any] | None = None,
    success: bool = True,
    error_code: str | None = None,
) -> None:
    """Insert an `AuditLog` row for a security / abuse-detection event (7.1.5).

    Distinct from `log_intent_event` on three axes:

      - `user_id` is optional — pre-auth events (rate-limit hit on
        `/api/auth/*`, anonymous register-burst from one IP) genuinely
        have no user context. Schema relaxed at migration `a522942e0df5`.
      - `actor_ip` is first-class — `who/what/when/where` audit shape.
      - No `agent_id`/`mandate_id` — security signals are pre-mandate
        by definition (mandate flow itself is hardened by the existing
        `MandateActions` / step-up audit path).

    Never raises. Flush-only, no commit — caller controls the
    transaction boundary (handlers that don't have a session in scope
    can mint one via `AsyncSessionLocal()` and `await session.commit()`
    around this call).
    """
    try:
        row = AuditLog(
            user_id=user_id,
            agent_id=None,
            mandate_id=None,
            action=action,
            params=params,
            result=None,
            success=success,
            error_code=error_code,
            actor_ip=actor_ip,
        )
        db.add(row)
        await db.flush()
    except Exception as exc:
        try:
            log.warning(
                "audit.security_event.write_failed",
                action=action,
                error=type(exc).__name__,
                message=str(exc),
            )
        except Exception:
            pass
