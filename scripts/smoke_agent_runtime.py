"""End-to-end runtime smoke for Vifaras platform-managed Anthropic AI.

Purpose:
  - seed a disposable tier-2 user + active agent + active mandate
  - run one real AgentOrchestrator tick through Anthropic
  - verify DB side effects: agent summary, audit row, daily cost row
  - clean up seeded rows by default

Run:
  uv run python scripts/smoke_agent_runtime.py

Keep seeded rows for manual inspection:
  uv run python scripts/smoke_agent_runtime.py --keep

Clean interrupted disposable smoke rows:
  uv run python scripts/smoke_agent_runtime.py --cleanup-stale
"""
from __future__ import annotations

import argparse
import asyncio
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from anthropic import AsyncAnthropic
from sqlalchemy import delete, func, select

from app.agents.orchestrator import AgentOrchestrator, TickResult
from app.core import canonicalization, platform_limits
from app.core.config import settings
from app.core.datetime_helpers import utc_today
from app.core.db import AsyncSessionLocal, engine, sync_engine
from app.models.schema import (
    Agent,
    AuditLog,
    DailyCostTracking,
    Intent,
    Mandate,
    Notification,
    StepUpRequest,
    User,
    UserQuestion,
)
from app.services.audit_service import AgentActions
from app.services.auth_service import _b64url


@dataclass(frozen=True)
class SmokeSeed:
    user_id: str
    agent_id: str
    mandate_id: str


@dataclass(frozen=True)
class SmokeVerification:
    last_tick_at: datetime
    last_tick_summary: dict[str, Any]
    audit_tick_completed_count: int
    audit_total_count: int
    daily_cost_usd: float
    daily_tick_count: int


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run one real Vifaras agent tick through Anthropic."
    )
    parser.add_argument(
        "--keep",
        action="store_true",
        help="Keep disposable DB rows after verification for manual inspection.",
    )
    parser.add_argument(
        "--allow-prod",
        action="store_true",
        help="Allow running when APP_ENV is prod/production.",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=45.0,
        help="Anthropic request timeout for this smoke run.",
    )
    parser.add_argument(
        "--cleanup-stale",
        action="store_true",
        help="Delete disposable smoke rows from interrupted prior runs, then exit.",
    )
    return parser.parse_args()


def _guard_runtime(*, allow_prod: bool) -> None:
    if settings.app_env.lower() in {"prod", "production"} and not allow_prod:
        raise SystemExit(
            "Refusing to seed smoke data in production. Pass --allow-prod only "
            "for an intentional controlled production smoke."
        )


def _guard_anthropic_key() -> None:
    if not settings.anthropic_api_key:
        raise SystemExit(
            "ANTHROPIC_API_KEY is empty. Set it in .env or the process environment."
        )


async def _seed_disposable_agent() -> SmokeSeed:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    user_id = str(uuid.uuid4())
    agent_id = str(uuid.uuid4())
    mandate_id = str(uuid.uuid4())
    email = f"smoke-agent-{user_id}@example.com"

    async with AsyncSessionLocal() as db:
        user = User(
            id=user_id,
            tier=2,
            nullifier_hash=f"smoke-nullifier-{user_id}",
            passkey_credential_id=_b64url(f"smoke-credential-{user_id}".encode()),
            passkey_pubkey=_b64url(f"smoke-pubkey-{user_id}".encode()),
            passkey_sign_count=0,
            notification_email=email,
            status="active",
            created_at=now,
            last_active_at=now,
            attributes_proven={
                "isAdult": True,
                "issuingState": "IT",
                "documentValid": True,
                "documentExpiry": "2030-04-15",
            },
            attributes_verified_at=now,
            attributes_expires_at=now + timedelta(days=365),
        )
        db.add(user)
        await db.flush()

        agent = Agent(
            id=agent_id,
            user_id=user_id,
            name="Anthropic runtime smoke",
            pubkey=f"smoke-agent-pubkey-{agent_id}",
            privkey_kms_ref="smoke:no-private-key",
            status="active",
            created_at=now,
        )
        db.add(agent)
        await db.flush()

        issued_at = now
        expires_at = now + timedelta(days=1)
        payload = _build_mandate_payload(
            mandate_id=mandate_id,
            user=user,
            agent=agent,
            issued_at=issued_at,
            expires_at=expires_at,
        )
        canonical = canonicalization.canonicalize(payload).decode("utf-8")
        mandate = Mandate(
            id=mandate_id,
            agent_id=agent_id,
            user_id=user_id,
            version=platform_limits.MANDATE_SPEC_VERSION,
            scope=payload["scope"],
            limits=payload["limits"],
            step_up_required_for=payload["step_up_required_for"],
            constraints=payload["constraints"],
            spent_total_eur=Decimal("0"),
            deals_count=0,
            spent_today_eur=Decimal("0"),
            last_reset_date=issued_at,
            issued_at=issued_at,
            expires_at=expires_at,
            signature={"algorithm": "smoke", "credential_id": "smoke"},
            canonical_payload=canonical,
        )
        db.add(mandate)
        await db.commit()

    return SmokeSeed(user_id=user_id, agent_id=agent_id, mandate_id=mandate_id)


def _build_mandate_payload(
    *,
    mandate_id: str,
    user: User,
    agent: Agent,
    issued_at: datetime,
    expires_at: datetime,
) -> dict[str, Any]:
    """Build a minimal mandate payload for a no-op runtime tick.

    The smoke mandate intentionally allows only read-only tools. If Claude
    tries a marketplace write, MandateVerifier denies it before any business
    row is created.
    """
    return {
        "version": platform_limits.MANDATE_SPEC_VERSION,
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
            "allowed_actions": ["read_inbox", "check_state"],
            "forbidden_actions": list(platform_limits.V0_DEFAULT_FORBIDDEN_ACTIONS),
        },
        "limits": {
            "max_price_per_deal_eur": 25,
            "max_total_volume_eur_per_mandate": 25,
            "max_total_volume_eur_per_day": 25,
            "max_deals_per_day": 1,
            "max_active_intents": 1,
            "max_concurrent_negotiations": 1,
        },
        "step_up_required_for": [],
        "constraints": {
            "geo_scope": list(platform_limits.GEO_SCOPE_V0),
            "categories_allowed": ["*"],
            "categories_forbidden": list(platform_limits.HARD_FORBIDDEN_CATEGORIES),
            "operating_hours": platform_limits.V0_DEFAULT_OPERATING_HOURS,
        },
        "revocation": dict(platform_limits.REVOCATION_POLICY_V0),
        "challenge": "0" * 64,
    }


async def _verify_persistence(
    seed: SmokeSeed,
    result: TickResult,
) -> SmokeVerification:
    async with AsyncSessionLocal() as db:
        agent = await db.get(Agent, seed.agent_id)
        if agent is None:
            raise SystemExit("Runtime smoke failed: seeded agent disappeared.")
        if agent.last_tick_at is None:
            raise SystemExit("Runtime smoke failed: agents.last_tick_at was not set.")
        if not isinstance(agent.last_tick_summary, dict):
            raise SystemExit(
                "Runtime smoke failed: agents.last_tick_summary was not persisted."
            )

        summary = agent.last_tick_summary
        if summary.get("reason") != result.reason:
            raise SystemExit(
                "Runtime smoke failed: last_tick_summary.reason does not match result."
            )
        if float(summary.get("cost_usd", 0) or 0) <= 0:
            raise SystemExit(
                "Runtime smoke failed: last_tick_summary.cost_usd is empty."
            )

        tick_audit_count = int(
            await db.scalar(
                select(func.count())
                .select_from(AuditLog)
                .where(AuditLog.agent_id == seed.agent_id)
                .where(AuditLog.action == AgentActions.TICK_COMPLETED)
                .where(AuditLog.success.is_(True))
            )
            or 0
        )
        if tick_audit_count < 1:
            raise SystemExit("Runtime smoke failed: tick_completed audit row missing.")

        audit_total_count = int(
            await db.scalar(
                select(func.count())
                .select_from(AuditLog)
                .where(AuditLog.agent_id == seed.agent_id)
            )
            or 0
        )

        daily_row = await db.scalar(
            select(DailyCostTracking).where(
                DailyCostTracking.date == utc_today(),
                DailyCostTracking.user_id == seed.user_id,
            )
        )
        if daily_row is None:
            raise SystemExit("Runtime smoke failed: daily cost row missing.")
        daily_cost_usd = float(daily_row.total_cost_usd or 0)
        if daily_cost_usd <= 0:
            raise SystemExit("Runtime smoke failed: daily cost total is empty.")

        return SmokeVerification(
            last_tick_at=agent.last_tick_at,
            last_tick_summary=summary,
            audit_tick_completed_count=tick_audit_count,
            audit_total_count=audit_total_count,
            daily_cost_usd=daily_cost_usd,
            daily_tick_count=int(daily_row.tick_count or 0),
        )


async def _cleanup_seed(seed: SmokeSeed) -> None:
    async with AsyncSessionLocal() as db:
        await db.execute(delete(AuditLog).where(AuditLog.agent_id == seed.agent_id))
        await db.execute(delete(Notification).where(Notification.user_id == seed.user_id))
        await db.execute(delete(UserQuestion).where(UserQuestion.agent_id == seed.agent_id))
        await db.execute(delete(StepUpRequest).where(StepUpRequest.agent_id == seed.agent_id))
        await db.execute(
            delete(DailyCostTracking).where(DailyCostTracking.user_id == seed.user_id)
        )
        await db.execute(delete(Intent).where(Intent.agent_id == seed.agent_id))
        await db.execute(delete(Mandate).where(Mandate.id == seed.mandate_id))
        await db.execute(delete(Agent).where(Agent.id == seed.agent_id))
        await db.execute(delete(User).where(User.id == seed.user_id))
        await db.commit()


async def _cleanup_stale_smoke_rows() -> int:
    async with AsyncSessionLocal() as db:
        user_ids = list(
            await db.scalars(
                select(User.id).where(User.nullifier_hash.like("smoke-nullifier-%"))
            )
        )
        if not user_ids:
            return 0

        agent_ids = list(
            await db.scalars(select(Agent.id).where(Agent.user_id.in_(user_ids)))
        )
        mandate_ids = list(
            await db.scalars(select(Mandate.id).where(Mandate.user_id.in_(user_ids)))
        )

        await db.execute(delete(AuditLog).where(AuditLog.user_id.in_(user_ids)))
        if agent_ids:
            await db.execute(delete(AuditLog).where(AuditLog.agent_id.in_(agent_ids)))
            await db.execute(
                delete(UserQuestion).where(UserQuestion.agent_id.in_(agent_ids))
            )
            await db.execute(
                delete(StepUpRequest).where(StepUpRequest.agent_id.in_(agent_ids))
            )
            await db.execute(delete(Intent).where(Intent.agent_id.in_(agent_ids)))
        await db.execute(delete(Notification).where(Notification.user_id.in_(user_ids)))
        await db.execute(
            delete(DailyCostTracking).where(DailyCostTracking.user_id.in_(user_ids))
        )
        if mandate_ids:
            await db.execute(delete(Mandate).where(Mandate.id.in_(mandate_ids)))
        await db.execute(delete(Agent).where(Agent.user_id.in_(user_ids)))
        await db.execute(delete(User).where(User.id.in_(user_ids)))
        await db.commit()
        return len(user_ids)


def _print_success(
    *,
    seed: SmokeSeed,
    result: TickResult,
    verification: SmokeVerification,
    cleanup: str,
) -> None:
    final_text = (result.final_response_text or "").replace("\n", " ").strip()
    if len(final_text) > 240:
        final_text = final_text[:237] + "..."

    print("Agent runtime smoke OK")
    print(f"model={settings.anthropic_model}")
    print(f"user_id={seed.user_id}")
    print(f"agent_id={seed.agent_id}")
    print(f"mandate_id={seed.mandate_id}")
    print(f"reason={result.reason}")
    print(f"turns={result.turns_used}")
    print(f"tool_calls={result.tool_calls_count}")
    print(f"estimated_cost_usd={result.estimated_cost_usd:.8f}")
    print(f"last_tick_at={verification.last_tick_at.isoformat()}")
    print(f"summary_cost_usd={verification.last_tick_summary.get('cost_usd')}")
    print(f"audit_tick_completed={verification.audit_tick_completed_count}")
    print(f"audit_total={verification.audit_total_count}")
    print(f"daily_cost_usd={verification.daily_cost_usd:.6f}")
    print(f"daily_tick_count={verification.daily_tick_count}")
    print(f"cleanup={cleanup}")
    print(f"text={final_text}")


async def main() -> None:
    args = _parse_args()
    _guard_runtime(allow_prod=args.allow_prod)
    if args.cleanup_stale:
        deleted = await _cleanup_stale_smoke_rows()
        await engine.dispose()
        sync_engine.dispose()
        print(f"stale_smoke_users_deleted={deleted}")
        return

    _guard_anthropic_key()

    seed = await _seed_disposable_agent()
    verification: SmokeVerification | None = None
    result: TickResult | None = None
    cleanup = "kept"

    try:
        client = AsyncAnthropic(
            api_key=settings.anthropic_api_key,
            timeout=args.timeout_seconds,
        )
        result = await AgentOrchestrator(anthropic_client=client).run_tick(
            seed.agent_id
        )
        if not result.success:
            raise SystemExit(
                "Runtime smoke failed: "
                f"reason={result.reason} error={result.error or ''}"
            )
        if result.estimated_cost_usd <= 0:
            raise SystemExit("Runtime smoke failed: estimated cost is empty.")

        verification = await _verify_persistence(seed, result)
    finally:
        if not args.keep:
            await _cleanup_seed(seed)
            cleanup = "done"
        await engine.dispose()
        sync_engine.dispose()

    assert result is not None
    assert verification is not None
    _print_success(
        seed=seed,
        result=result,
        verification=verification,
        cleanup=cleanup,
    )


if __name__ == "__main__":
    asyncio.run(main())
