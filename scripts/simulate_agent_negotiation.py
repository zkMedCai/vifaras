"""Deterministic buyer-agent/seller-agent negotiation smoke.

This is a dev helper for the browser flow after FASE 10.1.4.2:

  compatible intents + match
  -> seller agent offer
  -> buyer agent counter-offer
  -> seller agent final counter-offer
  -> buyer agent accept
  -> pending_signatures deal

When real tier-2 account emails are supplied, created marketplace rows are
kept by default so the users can inspect `/negotiations/{id}` and sign the
resulting `/deals/{id}` from the browser.

Run against existing local users:

  uv run python scripts/simulate_agent_negotiation.py \
    --buyer-email grey.area@outlook.it \
    --seller-email salmoit83@gmail.com

Run with disposable users and clean up automatically:

  uv run python scripts/simulate_agent_negotiation.py
"""
from __future__ import annotations

import argparse
import asyncio
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import Session

from app.agents.tool_layer import AsyncToolHandler, ToolResult
from app.core import canonicalization, platform_limits
from app.core.config import settings
from app.core.db import AsyncSessionLocal, engine, sync_engine
from app.models.schema import (
    Agent,
    AuditLog,
    Deal,
    DealMessage,
    DealSignatureDraft,
    Intent,
    Mandate,
    Match,
    Negotiation,
    Notification,
    StepUpRequest,
    User,
    UserQuestion,
)
from app.services.auth_service import _b64url
from app.services.mandate_verifier import MandateVerifier


SMOKE_EMAIL_PREFIX = "simulate-agent-negotiation"


@dataclass(frozen=True)
class Party:
    user_id: str
    agent_id: str
    mandate_id: str
    email: str
    disposable: bool


@dataclass(frozen=True)
class SimulatedRun:
    buyer: Party
    seller: Party
    buy_intent_id: str
    sell_intent_id: str
    match_id: str
    negotiation_id: str
    deal_id: str
    agreed_price_cents: int
    turns: list[dict[str, Any]]
    cleanup: str


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Simulate a buyer/seller agent negotiation and create a pending deal."
    )
    parser.add_argument("--buyer-email", help="Existing tier-2 buyer notification email.")
    parser.add_argument("--seller-email", help="Existing tier-2 seller notification email.")
    parser.add_argument(
        "--seller-offer-eur",
        type=Decimal,
        default=Decimal("95"),
        help="Initial seller offer. Keep below local mandate caps for real accounts.",
    )
    parser.add_argument(
        "--buyer-counter-eur",
        type=Decimal,
        default=Decimal("88"),
        help="Buyer counter-offer.",
    )
    parser.add_argument(
        "--seller-final-eur",
        type=Decimal,
        default=Decimal("90"),
        help="Seller final counter-offer accepted by buyer.",
    )
    parser.add_argument(
        "--keep",
        action="store_true",
        help="Keep disposable rows. Existing-user runs are kept by default.",
    )
    parser.add_argument(
        "--cleanup",
        action="store_true",
        help="Delete rows created by this run after verification.",
    )
    parser.add_argument(
        "--allow-prod",
        action="store_true",
        help="Allow running when APP_ENV is prod/production.",
    )
    return parser.parse_args()


def _guard_runtime(*, allow_prod: bool) -> None:
    if settings.app_env.lower() in {"prod", "production"} and not allow_prod:
        raise SystemExit(
            "Refusing to create negotiation smoke data in production. "
            "Pass --allow-prod only for an intentional controlled smoke."
        )


def _eur_to_cents(value: Decimal) -> int:
    return int((value * Decimal(100)).to_integral_value())


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def _resolve_or_seed_party(
    db,
    *,
    role: str,
    email: str | None,
) -> Party:
    if email:
        user = await db.scalar(
            select(User).where(func.lower(User.notification_email) == email.lower())
        )
        if user is None:
            raise SystemExit(f"{role} user not found for email={email!r}")
        if int(user.tier or 0) < 2:
            raise SystemExit(f"{role} user {user.id} is tier {user.tier}, expected tier 2")

        agent = await db.scalar(
            select(Agent)
            .where(Agent.user_id == user.id)
            .where(Agent.status == "active")
            .order_by(Agent.created_at.desc())
        )
        if agent is None:
            raise SystemExit(f"{role} user {user.id} has no active agent")

        mandate = await db.scalar(
            select(Mandate)
            .where(Mandate.user_id == user.id)
            .where(Mandate.agent_id == agent.id)
            .where(Mandate.revoked_at.is_(None))
            .where(Mandate.expires_at > _now())
            .order_by(Mandate.issued_at.desc())
        )
        if mandate is None:
            raise SystemExit(f"{role} agent {agent.id} has no active mandate")

        return Party(
            user_id=user.id,
            agent_id=agent.id,
            mandate_id=mandate.id,
            email=email,
            disposable=False,
        )

    return await _seed_disposable_party(db, role=role)


async def _seed_disposable_party(db, *, role: str) -> Party:
    now = _now()
    user_id = str(uuid.uuid4())
    agent_id = str(uuid.uuid4())
    mandate_id = str(uuid.uuid4())
    email = f"{SMOKE_EMAIL_PREFIX}-{role}-{user_id[:8]}@example.com"

    user = User(
        id=user_id,
        tier=2,
        nullifier_hash=f"{SMOKE_EMAIL_PREFIX}-nullifier-{user_id}",
        attributes_proven={
            "isAdult": True,
            "issuingState": "IT",
            "documentValid": True,
            "documentExpiry": "2030-04-15",
        },
        attributes_verified_at=now,
        attributes_expires_at=now + timedelta(days=365),
        passkey_credential_id=_b64url(f"{SMOKE_EMAIL_PREFIX}-credential-{user_id}".encode()),
        passkey_pubkey=_b64url(f"{SMOKE_EMAIL_PREFIX}-pubkey-{user_id}".encode()),
        passkey_sign_count=0,
        notification_email=email,
        status="active",
        created_at=now,
        last_active_at=now,
    )
    db.add(user)
    await db.flush()

    agent = Agent(
        id=agent_id,
        user_id=user_id,
        name=f"{role.title()} negotiation smoke",
        pubkey=f"{SMOKE_EMAIL_PREFIX}-agent-pubkey-{agent_id}",
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
        canonical_payload=canonicalization.canonicalize(payload).decode("utf-8"),
    )
    db.add(mandate)
    await db.commit()

    return Party(
        user_id=user_id,
        agent_id=agent_id,
        mandate_id=mandate_id,
        email=email,
        disposable=True,
    )


def _build_mandate_payload(
    *,
    mandate_id: str,
    user: User,
    agent: Agent,
    issued_at: datetime,
    expires_at: datetime,
) -> dict[str, Any]:
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
            "allowed_actions": list(platform_limits.V0_DEFAULT_ALLOWED_ACTIONS),
            "forbidden_actions": list(platform_limits.V0_DEFAULT_FORBIDDEN_ACTIONS),
        },
        "limits": {
            "max_price_per_deal_eur": 1000,
            "max_total_volume_eur_per_mandate": 1000,
            "max_total_volume_eur_per_day": 1000,
            "max_deals_per_day": 10,
            "max_active_intents": 10,
            "max_concurrent_negotiations": 10,
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


async def _seed_intents_and_match(
    *,
    buyer: Party,
    seller: Party,
    seller_offer_cents: int,
    buyer_counter_cents: int,
    seller_final_cents: int,
) -> tuple[str, str, str]:
    now = _now()
    run_id = uuid.uuid4().hex[:8]
    seller_floor = max(1_00, min(buyer_counter_cents, seller_final_cents) - 10_00)
    buyer_cap = max(seller_offer_cents, seller_final_cents) + 10_00

    async with AsyncSessionLocal() as db:
        buy_intent = Intent(
            id=str(uuid.uuid4()),
            user_id=buyer.user_id,
            agent_id=buyer.agent_id,
            side="buy",
            title=f"Buyer agent smoke {run_id}",
            description="Buyer agent wants a compatible item for deterministic negotiation.",
            category="electronics_laptops",
            description_embedding=None,
            reservation_price_cents=buyer_cap,
            ideal_price_cents=buyer_counter_cents,
            currency="EUR",
            hard_constraints={"delivery": "insured_shipping", "smoke": True},
            soft_preferences={"agent_negotiation_smoke": run_id},
            status="active",
            expires_at=now + timedelta(days=7),
            created_at=now,
        )
        sell_intent = Intent(
            id=str(uuid.uuid4()),
            user_id=seller.user_id,
            agent_id=seller.agent_id,
            side="sell",
            title=f"Seller agent smoke {run_id}",
            description="Seller agent offers a compatible item for deterministic negotiation.",
            category="electronics_laptops",
            description_embedding=None,
            reservation_price_cents=seller_floor,
            ideal_price_cents=seller_offer_cents,
            currency="EUR",
            hard_constraints={"delivery": "insured_shipping", "smoke": True},
            soft_preferences={"agent_negotiation_smoke": run_id},
            status="active",
            expires_at=now + timedelta(days=7),
            created_at=now,
        )
        db.add_all([buy_intent, sell_intent])
        await db.flush()

        match = Match(
            id=str(uuid.uuid4()),
            buy_intent_id=buy_intent.id,
            sell_intent_id=sell_intent.id,
            similarity_score=Decimal("0.9300"),
            price_overlap=True,
            price_proximity_score=Decimal("0.8700"),
            combined_score=Decimal("0.9120"),
            status="discovered",
            created_at=now,
        )
        db.add(match)
        await db.commit()

    return buy_intent.id, sell_intent.id, match.id


async def _run_tool(agent_id: str, tool_name: str, params: dict[str, Any]) -> dict[str, Any]:
    async with AsyncSessionLocal() as db:
        with Session(sync_engine) as sync_db:
            verifier = MandateVerifier(sync_db)
            result = await AsyncToolHandler(
                db,
                agent_id,
                verifier=verifier,
            ).handle(tool_name, params)
    _raise_unless_ok(tool_name, result)
    assert result.data is not None
    return result.data


def _raise_unless_ok(tool_name: str, result: ToolResult) -> None:
    if result.status == "ok":
        return
    details = result.data if result.data is not None else result.error
    raise SystemExit(f"{tool_name} failed: status={result.status} details={details}")


async def _simulate_negotiation(
    *,
    buyer: Party,
    seller: Party,
    buy_intent_id: str,
    sell_intent_id: str,
    match_id: str,
    seller_offer_cents: int,
    buyer_counter_cents: int,
    seller_final_cents: int,
) -> tuple[str, str, int, list[dict[str, Any]]]:
    first = await _run_tool(
        seller.agent_id,
        "send_offer",
        {
            "match_id": match_id,
            "price_cents": seller_offer_cents,
            "message": "Seller agent: initial offer based on item condition and delivery.",
        },
    )
    negotiation_id = first["negotiation_id"]

    await _run_tool(
        buyer.agent_id,
        "send_counter_offer",
        {
            "negotiation_id": negotiation_id,
            "price_cents": buyer_counter_cents,
            "message": "Buyer agent: counter-offer within mandate budget.",
        },
    )
    await _run_tool(
        seller.agent_id,
        "send_counter_offer",
        {
            "negotiation_id": negotiation_id,
            "price_cents": seller_final_cents,
            "message": "Seller agent: final acceptable price.",
        },
    )
    accepted = await _run_tool(
        buyer.agent_id,
        "accept_offer",
        {"negotiation_id": negotiation_id},
    )

    async with AsyncSessionLocal() as db:
        deal = await db.scalar(select(Deal).where(Deal.id == accepted["deal_id"]))
        negotiation = await db.scalar(
            select(Negotiation).where(Negotiation.id == negotiation_id)
        )
        if deal is None or negotiation is None:
            raise SystemExit("Simulation failed: negotiation or deal missing after accept.")
        if deal.status != "pending_signatures":
            raise SystemExit(f"Expected pending_signatures deal, got {deal.status!r}.")
        if deal.buy_intent_id != buy_intent_id or deal.sell_intent_id != sell_intent_id:
            raise SystemExit("Simulation failed: deal does not reference seeded intents.")
        turns = list((negotiation.state or {}).get("turns") or [])
        turn_types = [turn.get("type") for turn in turns]
        if turn_types != ["offer", "counter_offer", "counter_offer", "accept"]:
            raise SystemExit(f"Unexpected turn sequence: {turn_types}")

    return negotiation_id, accepted["deal_id"], int(accepted["agreed_price_cents"]), turns


async def _cleanup_created(run: SimulatedRun) -> None:
    async with AsyncSessionLocal() as db:
        ids = [
            run.buy_intent_id,
            run.sell_intent_id,
            run.match_id,
            run.negotiation_id,
            run.deal_id,
        ]
        await db.execute(
            delete(AuditLog).where(
                or_(
                    AuditLog.params["intent_id"].astext.in_(ids),
                    AuditLog.params["buy_intent_id"].astext.in_(ids),
                    AuditLog.params["sell_intent_id"].astext.in_(ids),
                    AuditLog.params["match_id"].astext.in_(ids),
                    AuditLog.params["negotiation_id"].astext.in_(ids),
                    AuditLog.params["deal_id"].astext.in_(ids),
                )
            )
        )
        await db.execute(
            delete(Notification).where(
                or_(
                    Notification.payload["intent_id"].astext.in_(ids),
                    Notification.payload["match_id"].astext.in_(ids),
                    Notification.payload["negotiation_id"].astext.in_(ids),
                    Notification.payload["deal_id"].astext.in_(ids),
                )
            )
        )
        await db.execute(delete(DealSignatureDraft).where(DealSignatureDraft.deal_id == run.deal_id))
        await db.execute(delete(DealMessage).where(DealMessage.deal_id == run.deal_id))
        await db.execute(delete(Deal).where(Deal.id == run.deal_id))
        await db.execute(delete(Negotiation).where(Negotiation.id == run.negotiation_id))
        await db.execute(delete(Match).where(Match.id == run.match_id))
        await db.execute(delete(Intent).where(Intent.id.in_([run.buy_intent_id, run.sell_intent_id])))

        if run.buyer.disposable and run.seller.disposable:
            user_ids = [run.buyer.user_id, run.seller.user_id]
            agent_ids = [run.buyer.agent_id, run.seller.agent_id]
            mandate_ids = [run.buyer.mandate_id, run.seller.mandate_id]
            await db.execute(delete(AuditLog).where(AuditLog.user_id.in_(user_ids)))
            await db.execute(delete(AuditLog).where(AuditLog.agent_id.in_(agent_ids)))
            await db.execute(delete(Notification).where(Notification.user_id.in_(user_ids)))
            await db.execute(delete(UserQuestion).where(UserQuestion.agent_id.in_(agent_ids)))
            await db.execute(delete(StepUpRequest).where(StepUpRequest.agent_id.in_(agent_ids)))
            await db.execute(delete(Mandate).where(Mandate.id.in_(mandate_ids)))
            await db.execute(delete(Agent).where(Agent.id.in_(agent_ids)))
            await db.execute(delete(User).where(User.id.in_(user_ids)))

        await db.commit()


def _print_success(run: SimulatedRun) -> None:
    print("Agent negotiation simulation OK")
    print(f"buyer_email={run.buyer.email}")
    print(f"buyer_user_id={run.buyer.user_id}")
    print(f"buyer_agent_id={run.buyer.agent_id}")
    print(f"seller_email={run.seller.email}")
    print(f"seller_user_id={run.seller.user_id}")
    print(f"seller_agent_id={run.seller.agent_id}")
    print(f"buy_intent_id={run.buy_intent_id}")
    print(f"sell_intent_id={run.sell_intent_id}")
    print(f"match_id={run.match_id}")
    print(f"negotiation_id={run.negotiation_id}")
    print(f"deal_id={run.deal_id}")
    print(f"agreed_price_cents={run.agreed_price_cents}")
    print("turns=")
    for turn in run.turns:
        print(
            "  "
            f"#{turn['turn_number']} {turn['type']} "
            f"agent={turn['agent_id']} price_cents={turn['price_cents']}"
        )
    print(f"frontend_negotiation_url=http://localhost:3000/negotiations/{run.negotiation_id}")
    print(f"frontend_deal_url=http://localhost:3000/deals/{run.deal_id}")
    print(f"cleanup={run.cleanup}")


async def main() -> None:
    args = _parse_args()
    _guard_runtime(allow_prod=args.allow_prod)
    if bool(args.buyer_email) != bool(args.seller_email):
        raise SystemExit("Pass both --buyer-email and --seller-email, or neither.")
    if args.cleanup and args.keep:
        raise SystemExit("--cleanup and --keep are mutually exclusive.")

    existing_users = bool(args.buyer_email and args.seller_email)
    cleanup_created = args.cleanup or (not existing_users and not args.keep)
    seller_offer_cents = _eur_to_cents(args.seller_offer_eur)
    buyer_counter_cents = _eur_to_cents(args.buyer_counter_eur)
    seller_final_cents = _eur_to_cents(args.seller_final_eur)

    async with AsyncSessionLocal() as db:
        buyer = await _resolve_or_seed_party(
            db,
            role="buyer",
            email=args.buyer_email,
        )
        seller = await _resolve_or_seed_party(
            db,
            role="seller",
            email=args.seller_email,
        )

    buy_intent_id, sell_intent_id, match_id = await _seed_intents_and_match(
        buyer=buyer,
        seller=seller,
        seller_offer_cents=seller_offer_cents,
        buyer_counter_cents=buyer_counter_cents,
        seller_final_cents=seller_final_cents,
    )
    negotiation_id, deal_id, agreed_price_cents, turns = await _simulate_negotiation(
        buyer=buyer,
        seller=seller,
        buy_intent_id=buy_intent_id,
        sell_intent_id=sell_intent_id,
        match_id=match_id,
        seller_offer_cents=seller_offer_cents,
        buyer_counter_cents=buyer_counter_cents,
        seller_final_cents=seller_final_cents,
    )

    run = SimulatedRun(
        buyer=buyer,
        seller=seller,
        buy_intent_id=buy_intent_id,
        sell_intent_id=sell_intent_id,
        match_id=match_id,
        negotiation_id=negotiation_id,
        deal_id=deal_id,
        agreed_price_cents=agreed_price_cents,
        turns=turns,
        cleanup="pending",
    )
    if cleanup_created:
        await _cleanup_created(run)
        run = SimulatedRun(**{**run.__dict__, "cleanup": "done"})
    else:
        run = SimulatedRun(**{**run.__dict__, "cleanup": "kept"})

    await engine.dispose()
    sync_engine.dispose()
    _print_success(run)


if __name__ == "__main__":
    asyncio.run(main())
