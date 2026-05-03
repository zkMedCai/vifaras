"""Public market board endpoint tests."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

import pytest
from app.models.schema import Intent, User
from app.services import embedding_service

from tests.factories import default_user_kwargs


async def _seed_user(db, *, tier: int = 0) -> str:
    user_id = str(uuid.uuid4())
    db.add(
        User(
            id=user_id,
            **default_user_kwargs(
                tier=tier, email=f"market-{user_id[:8]}@example.com"
            ),
        )
    )
    await db.commit()
    return user_id


async def _seed_intent(
    db,
    *,
    user_id: str,
    side: str = "sell",
    status: str = "active",
    title: str = "MacBook Pro 14",
    category: str = "electronics_laptops",
    location: str | None = "Roma, IT",
    created_offset_minutes: int = 0,
    expires_delta_days: int = 14,
    reservation_price_cents: int = 120_000,
    ideal_price_cents: int = 140_000,
) -> str:
    now = datetime.utcnow()
    intent_id = str(uuid.uuid4())
    db.add(
        Intent(
            id=intent_id,
            user_id=user_id,
            agent_id=None,
            side=side,
            title=title,
            description="Public description",
            category=category,
            description_embedding=embedding_service._fake_embedding(title),
            reservation_price_cents=reservation_price_cents,
            ideal_price_cents=ideal_price_cents,
            currency="EUR",
            hard_constraints={"location": location} if location else {},
            soft_preferences={"private_note": "do not expose"},
            status=status,
            expires_at=now + timedelta(days=expires_delta_days),
            created_at=now + timedelta(minutes=created_offset_minutes),
        )
    )
    await db.commit()
    return intent_id


@pytest.mark.db
async def test_public_market_lists_active_intents_without_auth(
    async_db_session, http_client
) -> None:
    user_id = await _seed_user(async_db_session)
    location = f"MarketTown-{uuid.uuid4().hex[:8]}, IT"
    intent_id = await _seed_intent(
        async_db_session, user_id=user_id, location=location
    )

    response = await http_client.get("/api/market", params={"location": location})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0] == {
        "intent_id": intent_id,
        "side": "sell",
        "title": "MacBook Pro 14",
        "description": "Public description",
        "category": "electronics_laptops",
        "public_price_eur": 1200.0,
        "currency": "EUR",
        "location": location,
        "status": "active",
        "created_at": body["items"][0]["created_at"],
        "expires_at": body["items"][0]["expires_at"],
    }
    serialized = response.text
    assert user_id not in serialized
    assert "ideal_price" not in serialized
    assert "private_note" not in serialized


@pytest.mark.db
async def test_public_market_hides_inactive_and_expired_intents(
    async_db_session, http_client
) -> None:
    user_id = await _seed_user(async_db_session)
    location = f"MarketTown-{uuid.uuid4().hex[:8]}, IT"
    active_id = await _seed_intent(
        async_db_session,
        user_id=user_id,
        title="Active listing",
        location=location,
    )
    await _seed_intent(
        async_db_session,
        user_id=user_id,
        title="Cancelled listing",
        status="cancelled",
        location=location,
    )
    await _seed_intent(
        async_db_session,
        user_id=user_id,
        title="Expired listing",
        expires_delta_days=-1,
        location=location,
    )

    response = await http_client.get("/api/market", params={"location": location})

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert [item["intent_id"] for item in body["items"]] == [active_id]


@pytest.mark.db
async def test_public_market_filters_by_side_category_and_location(
    async_db_session, http_client
) -> None:
    user_id = await _seed_user(async_db_session)
    location = f"MarketTown-{uuid.uuid4().hex[:8]}, IT"
    target_id = await _seed_intent(
        async_db_session,
        user_id=user_id,
        side="buy",
        title="Cerco bici elettrica",
        category="vehicles_bicycles",
        location=location,
        reservation_price_cents=80_000,
        ideal_price_cents=65_000,
    )
    await _seed_intent(
        async_db_session,
        user_id=user_id,
        side="sell",
        category="vehicles_bicycles",
        location=location,
    )
    await _seed_intent(
        async_db_session,
        user_id=user_id,
        side="buy",
        category="electronics_laptops",
        location=location,
    )

    response = await http_client.get(
        "/api/market",
        params={
            "side": "buy",
            "category": "vehicles_bicycles",
            "location": location,
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["intent_id"] == target_id
    assert body["items"][0]["public_price_eur"] == 800.0
