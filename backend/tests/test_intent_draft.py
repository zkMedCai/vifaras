"""Natural-language intent draft endpoint/service tests."""
from __future__ import annotations

import uuid

import pytest
from app.core.config import settings
from app.core.security import create_access_token
from app.models.schema import DailyCostTracking, User
from app.services import intent_draft_service
from sqlalchemy import select

from tests.conftest import FakeAnthropicClient, _make_message, text_block
from tests.factories import default_user_kwargs


async def _seed_user(db, *, tier: int = 0) -> str:
    user_id = str(uuid.uuid4())
    db.add(
        User(
            id=user_id,
            **default_user_kwargs(
                tier=tier, email=f"draft-{user_id[:8]}@example.com"
            ),
        )
    )
    await db.commit()
    return user_id


def _bearer(client, user_id: str, tier: int = 0) -> None:
    client.headers["Authorization"] = (
        f"Bearer {create_access_token(user_id=user_id, tier=tier)}"
    )


@pytest.mark.db
async def test_draft_intent_from_text_parses_anthropic_json_and_tracks_cost(
    async_db_session, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "anthropic_api_key", "sk-ant-test")
    monkeypatch.setattr(settings, "anthropic_model", "claude-sonnet-4-5")
    user_id = await _seed_user(async_db_session)
    client = FakeAnthropicClient(
        [
            _make_message(
                [
                    text_block(
                        """{
                          "side": "sell",
                          "title": "Bici da corsa taglia M",
                          "description": "Bici da corsa in buone condizioni, ritiro a Roma.",
                          "category": "sport_bicycles",
                          "reservation_price_eur": 600,
                          "ideal_price_eur": 750,
                          "duration_days": 14,
                          "hard_constraints": {"location": "Roma, IT"},
                          "soft_preferences": {"pickup": true},
                          "confidence": 0.91,
                          "missing_fields": [],
                          "summary": "Bozza vendita bici pronta."
                        }"""
                    )
                ],
                stop_reason="end_turn",
                input_tokens=250,
                output_tokens=120,
            )
        ]
    )

    result = await intent_draft_service.draft_intent_from_text(
        async_db_session,
        user_id=user_id,
        prompt="Voglio vendere una bici da corsa taglia M a Roma, minimo 600 euro.",
        anthropic_client=client,
    )

    assert result.draft.side == "sell"
    assert result.draft.category == "sport_bicycles"
    assert result.draft.reservation_price_eur == 600
    assert result.draft.ideal_price_eur == 750
    assert result.draft.hard_constraints == {"location": "Roma, IT"}
    assert result.draft.missing_fields == []
    assert result.estimated_cost_usd > 0
    cost_row = await async_db_session.scalar(
        select(DailyCostTracking).where(DailyCostTracking.user_id == user_id)
    )
    assert cost_row is not None
    assert float(cost_row.total_cost_usd) == pytest.approx(
        result.estimated_cost_usd
    )


def test_parse_draft_sanitizes_invalid_fields() -> None:
    draft = intent_draft_service._parse_draft(  # noqa: SLF001
        """```json
        {
          "side": "rent",
          "title": "X",
          "description": "",
          "category": "weapons",
          "reservation_price_eur": -1,
          "ideal_price_eur": null,
          "duration_days": 90,
          "hard_constraints": {"location": "Roma"},
          "confidence": 2
        }
        ```"""
    )

    assert draft.side is None
    assert draft.category is None
    assert draft.reservation_price_eur is None
    assert draft.duration_days == 30
    assert draft.hard_constraints == {}
    assert draft.confidence == 1.0
    assert {
        "side",
        "category",
        "reservation_price_eur",
        "ideal_price_eur",
    }.issubset(set(draft.missing_fields))


@pytest.mark.db
async def test_draft_intent_from_text_endpoint_returns_service_result(
    async_db_session, http_client, monkeypatch
) -> None:
    user_id = await _seed_user(async_db_session)
    _bearer(http_client, user_id)

    async def fake_draft(*_, **__):
        return intent_draft_service.IntentDraftResult(
            draft=intent_draft_service.IntentDraft(
                side="buy",
                title="MacBook Pro 14",
                description="Cerco MacBook Pro 14 in buone condizioni.",
                category="electronics_laptops",
                reservation_price_eur=1300,
                ideal_price_eur=1100,
                duration_days=14,
                hard_constraints={"location": "Roma, IT"},
                confidence=0.88,
                missing_fields=[],
                summary="Bozza acquisto pronta.",
            ),
            model="claude-sonnet-4-5",
            estimated_cost_usd=0.00123,
        )

    monkeypatch.setattr(
        intent_draft_service, "draft_intent_from_text", fake_draft
    )

    response = await http_client.post(
        "/api/intents/draft-from-text",
        json={"prompt": "Cerco MacBook Pro 14 a Roma, massimo 1300 euro."},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["side"] == "buy"
    assert body["category"] == "electronics_laptops"
    assert body["hard_constraints"] == {"location": "Roma, IT"}
    assert body["model"] == "claude-sonnet-4-5"
    assert body["estimated_cost_usd"] == pytest.approx(0.00123)


async def test_draft_intent_from_text_endpoint_requires_auth(http_client) -> None:
    response = await http_client.post(
        "/api/intents/draft-from-text",
        json={"prompt": "Voglio vendere una bici a Roma, minimo 600 euro."},
    )

    assert response.status_code == 401
