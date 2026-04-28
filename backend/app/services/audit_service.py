"""Audit service — structured-log audit events (brief task 2.3).

Two distinct audit channels coexist in this codebase:

1. **`AuditLog` table** (schema.py): per-agent-action records with
   `(user_id, agent_id, mandate_id, action, params, result, success)`.
   `mandate_id` is `NOT NULL`, so this table is reserved for actions taken
   *by an agent under an active mandate* — i.e. anything that happens
   from FASE 5 onwards. Wired into `tool_layer` / negotiation in 5.x.

2. **structlog audit events** (this module): identity / lifecycle events
   that don't have a mandate yet (tier upgrades, login, mandate-revoke
   in the future). Emitted as JSON to stdout under the `audit.*` event
   namespace so they can be pulled by log aggregators alongside the
   per-action audit table.

V0 strategy for tier upgrade: use the structured-log channel. Tier
upgrade happens *before* the user has a mandate (mandates are tier-2),
so the `AuditLog` table can't represent it without a sentinel
`mandate_id` — we'd be lying about the schema's invariants.

All functions in this module are designed to **never raise**: audit
emission must not abort the upgrade itself. If structlog fails, we
swallow + warn — the upgrade is already durable, the audit is
secondary.
"""
from __future__ import annotations

from app.core.logging import log


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

    Like `log_tier_upgrade`, this uses the structlog channel rather than
    the `AuditLog` table — the table requires `mandate_id NOT NULL` which
    is satisfied here, but for consistency we keep all *identity-lifecycle*
    audit on structlog and reserve the table for *agent actions* under a
    live mandate (5.x+).
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
