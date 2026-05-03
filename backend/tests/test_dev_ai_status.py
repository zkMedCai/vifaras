"""Dev/founder AI status endpoint coverage."""

from __future__ import annotations

import uuid

import pytest
from app.core.config import settings
from app.services.cost_tracking_service import upsert_daily_cost

from tests.factories import setup_active_mandate_async


async def test_dev_ai_status_endpoint_gated(http_client, monkeypatch) -> None:
    monkeypatch.setattr(settings, "enable_dev_endpoints", False)
    r = await http_client.get("/api/_dev/ai/status")
    assert r.status_code == 404


async def test_dev_ai_status_reports_provider_config_without_secrets(
    http_client, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "enable_dev_endpoints", True)
    monkeypatch.setattr(settings, "anthropic_api_key", "sk-ant-secret-test")
    monkeypatch.setattr(settings, "anthropic_model", "claude-sonnet-4-5")
    monkeypatch.setattr(settings, "openai_api_key", "")
    monkeypatch.setattr(settings, "embedding_backend", "fake")

    r = await http_client.get("/api/_dev/ai/status")

    assert r.status_code == 200
    body = r.json()
    assert body["providers"]["anthropic"] == {
        "configured": True,
        "model": "claude-sonnet-4-5",
        "pricing_known": True,
    }
    assert body["providers"]["openai_embeddings"] == {
        "configured": False,
        "backend": "fake",
        "model": settings.openai_embedding_model,
    }
    assert "sk-ant-secret-test" not in r.text


@pytest.mark.db
async def test_dev_ai_status_reports_cost_guardrails(
    async_db_session, http_client, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "enable_dev_endpoints", True)
    monkeypatch.setattr(settings, "max_daily_llm_cost_usd", 1.0)
    monkeypatch.setattr(settings, "daily_user_cost_cap_usd", 0.25)
    monkeypatch.setattr(settings, "agent_tick_cost_cap_usd", 0.05)

    user_id, _, _ = await setup_active_mandate_async(
        async_db_session,
        email=f"ai-status-{uuid.uuid4().hex[:6]}@x.com",
    )
    await upsert_daily_cost(
        async_db_session, user_id=user_id, cost_usd=0.42
    )
    await async_db_session.commit()

    r = await http_client.get("/api/_dev/ai/status")

    assert r.status_code == 200
    cost = r.json()["cost"]
    assert cost["today_cost_usd"] == pytest.approx(0.42, abs=1e-6)
    assert cost["max_daily_llm_cost_usd"] == 1.0
    assert cost["daily_cap_remaining_usd"] == pytest.approx(0.58, abs=1e-6)
    assert cost["daily_cap_reached"] is False
    assert cost["daily_user_cost_cap_usd"] == 0.25
    assert cost["agent_tick_cost_cap_usd"] == 0.05
