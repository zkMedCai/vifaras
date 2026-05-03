"""Real Anthropic matching smoke for the local dev database.

Creates two public active intents with no embeddings, runs the
`MATCHING_BACKEND=anthropic` matcher once, and verifies a Match row exists.

The smoke rows are intentionally kept by default so `/market` can show them.

Run:
  uv run python scripts/smoke_anthropic_matching.py
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

from anthropic import AsyncAnthropic
from app.core.config import settings
from app.core.db import AsyncSessionLocal
from app.models.schema import Intent, Match, User
from app.services import cost_tracking_service, match_service
from sqlalchemy import select


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


async def _create_user(db, *, label: str, run_id: str) -> User:
    user = User(
        id=str(uuid.uuid4()),
        nullifier_hash=f"smoke:anthropic-matching:{run_id}:{label}",
        attributes_proven={"adult": True, "country": "IT", "valid": True},
        attributes_verified_at=_now(),
        attributes_expires_at=_now() + timedelta(days=365),
        passkey_credential_id=f"smoke-cred-{run_id}-{label}",
        passkey_pubkey=f"smoke-pubkey-{run_id}-{label}",
        passkey_sign_count=0,
        notification_email=f"smoke-{run_id}-{label}@example.com",
        status="active",
        created_at=_now(),
        last_active_at=_now(),
    )
    db.add(user)
    await db.flush()
    return user


async def _create_intent(
    db,
    *,
    user: User,
    run_id: str,
    side: str,
) -> Intent:
    is_buy = side == "buy"
    title = (
        f"SMOKE Anthropic BUY MacBook Pro 14 {run_id}"
        if is_buy
        else f"SMOKE Anthropic SELL MacBook Pro 14 {run_id}"
    )
    description = (
        "Cerco MacBook Pro 14 Apple Silicon, almeno 16GB RAM, ottime "
        "condizioni, preferibilmente a Roma."
        if is_buy
        else "Vendo MacBook Pro 14 Apple Silicon 16GB RAM, usato poco, "
        "ottime condizioni, consegna a Roma."
    )
    intent = Intent(
        id=str(uuid.uuid4()),
        user_id=user.id,
        agent_id=None,
        side=side,
        title=title,
        description=description,
        category="electronics_laptops",
        description_embedding=None,
        reservation_price_cents=1_300_00 if is_buy else 1_000_00,
        ideal_price_cents=1_050_00 if is_buy else 1_180_00,
        currency="EUR",
        hard_constraints={"location": "Roma, IT"},
        soft_preferences={},
        status="active",
        expires_at=_now() + timedelta(days=14),
        created_at=_now(),
    )
    db.add(intent)
    await db.flush()
    return intent


async def main() -> None:
    if settings.matching_backend.strip().lower() != "anthropic":
        raise SystemExit(
            "MATCHING_BACKEND must be 'anthropic' for this smoke. "
            f"Current value: {settings.matching_backend!r}"
        )
    if not settings.anthropic_api_key:
        raise SystemExit("ANTHROPIC_API_KEY is empty.")

    run_id = uuid.uuid4().hex[:8]

    async with AsyncSessionLocal() as db:
        seller = await _create_user(db, label="seller", run_id=run_id)
        buyer = await _create_user(db, label="buyer", run_id=run_id)
        sell_intent = await _create_intent(
            db, user=seller, run_id=run_id, side="sell"
        )
        buy_intent = await _create_intent(
            db, user=buyer, run_id=run_id, side="buy"
        )
        await db.commit()

        before_cost = await cost_tracking_service.get_today_cost_usd(db)
        client = AsyncAnthropic(api_key=settings.anthropic_api_key, timeout=30.0)
        matches = await match_service.find_matches_for_intent(
            db,
            intent_id=sell_intent.id,
            limit=5,
            anthropic_client=client,
        )
        after_cost = await cost_tracking_service.get_today_cost_usd(db)

        match = await db.scalar(
            select(Match)
            .where(Match.buy_intent_id == buy_intent.id)
            .where(Match.sell_intent_id == sell_intent.id)
        )

        print("Anthropic matching smoke")
        print(f"run_id={run_id}")
        print(f"seller_user_id={seller.id}")
        print(f"buyer_user_id={buyer.id}")
        print(f"sell_intent_id={sell_intent.id}")
        print(f"buy_intent_id={buy_intent.id}")
        print(f"matches_returned={len(matches)}")
        print(f"today_cost_delta_usd={after_cost - before_cost:.8f}")

        if match is None:
            raise SystemExit("Anthropic matching smoke FAILED: no Match row created.")

        print("Anthropic matching smoke OK")
        print(f"match_id={match.id}")
        print(f"similarity_score={float(match.similarity_score or 0):.4f}")
        print(f"price_proximity_score={float(match.price_proximity_score or 0):.4f}")
        print(f"combined_score={float(match.combined_score or 0):.4f}")
        print("market_url=http://127.0.0.1:3000/market")


if __name__ == "__main__":
    asyncio.run(main())
