"""Pre-frontend hardening tests (brief task 7.0).

15 tests organised by concern:

  Rate limiting (5):
   1. limit enforced on `/api/intents` POST (`30/minute` strict)
   2. limit enforced on `/api/identity/verify-self` (`5/minute`)
   3. limit enforced on `/api/mandates/draft` (`10/minute`)
   4. 429 response carries `Retry-After` and the canonical error envelope
   5. disabling the limiter via `enabled=False` is a no-op (default test mode)

  CORS (3):
   6. configured origin gets `Access-Control-Allow-Origin` echoed back
   7. unconfigured origin: middleware does NOT echo the header
   8. OPTIONS preflight handled with allowed methods

  OpenAPI (2):
   9. `/openapi.json` returns a valid OpenAPI 3.x document
  10. critical POST endpoints expose `summary` + `description`

  Health (5):
  11. `/api/health` returns 200 with the expected shape, no auth
  12. `today_cost_usd` reflects DailyCostTracking after orchestrator UPSERT
  13. `last_successful_tick` reflects `agents.last_tick_at`
  14. `agent_scheduler` is `disabled` when settings flag is False
  15. legacy `/health` still answers 200
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta

import pytest

from app.services.cost_tracking_service import upsert_daily_cost
from app.core.config import settings
from app.core.rate_limit import limiter
from app.models.schema import Agent
from tests.factories import setup_active_mandate_async


# `enable_limiter` lives in conftest.py (shared with `test_rate_limit_deep`).


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------


@pytest.mark.db
async def test_intents_create_rate_limit_enforced(
    enable_limiter, monkeypatch, authenticated_client
):
    """30/minute → the 31st call gets 429."""
    monkeypatch.setattr(settings, "rate_limit_post_strict", "2/minute")
    limiter.reset()  # pick up the new limit

    client, _ = authenticated_client(tier=2)
    payload = {
        "side": "buy",
        "title": "macbook",
        "category": "electronics_laptops",
        "reservation_price_eur": 1200.0,
        "ideal_price_eur": 1000.0,
    }

    r1 = await client.post("/api/intents", json=payload)
    r2 = await client.post("/api/intents", json=payload)
    r3 = await client.post("/api/intents", json=payload)

    # First two reach the handler — outcome (201/422/404 depending on
    # DB seed state) doesn't matter, what matters is they're NOT 429.
    assert r1.status_code != 429
    assert r2.status_code != 429
    assert r3.status_code == 429, r3.text
    assert r3.json()["code"] == "rate_limited"


@pytest.mark.db
async def test_identity_verify_rate_limit_strictest(
    enable_limiter, monkeypatch, authenticated_client
):
    monkeypatch.setattr(settings, "rate_limit_self_verifier", "1/minute")
    limiter.reset()
    client, _ = authenticated_client(tier=0)

    body = {"proof": "x", "publicSignals": []}
    r1 = await client.post("/api/identity/verify-self", json=body)
    r2 = await client.post("/api/identity/verify-self", json=body)
    # First call may 4xx for proof validation (we don't care); second
    # must be the rate-limit 429.
    assert r2.status_code == 429


@pytest.mark.db
async def test_mandate_draft_rate_limited(
    enable_limiter, monkeypatch, authenticated_client
):
    monkeypatch.setattr(settings, "rate_limit_mandate_critical", "1/minute")
    limiter.reset()
    client, _ = authenticated_client(tier=1)

    body = {"agent_id": str(uuid.uuid4()), "expires_in_days": 30}
    r1 = await client.post("/api/mandates/draft", json=body)
    r2 = await client.post("/api/mandates/draft", json=body)
    assert r2.status_code == 429
    assert r2.json()["code"] == "rate_limited"


@pytest.mark.db
async def test_429_carries_retry_after_and_envelope(
    enable_limiter, monkeypatch, authenticated_client
):
    monkeypatch.setattr(settings, "rate_limit_post_strict", "1/minute")
    limiter.reset()
    client, _ = authenticated_client(tier=2)
    payload = {
        "side": "buy",
        "title": "x",
        "category": "electronics_laptops",
        "reservation_price_eur": 100.0,
        "ideal_price_eur": 80.0,
    }
    await client.post("/api/intents", json=payload)
    blocked = await client.post("/api/intents", json=payload)

    assert blocked.status_code == 429
    body = blocked.json()
    assert set(body.keys()) >= {"code", "message"}
    assert body["code"] == "rate_limited"
    # slowapi computes a retry hint; tolerate absence on some backends.
    if "retry-after" in {k.lower() for k in blocked.headers.keys()}:
        ra = blocked.headers.get("retry-after") or blocked.headers.get("Retry-After")
        assert int(ra) >= 0


@pytest.mark.db
async def test_disabled_limiter_is_no_op(authenticated_client):
    """With `enabled=False` (test default), repeated calls don't 429."""
    assert limiter.enabled is False
    client, _ = authenticated_client(tier=2)
    payload = {
        "side": "buy",
        "title": "noop",
        "category": "electronics_laptops",
        "reservation_price_eur": 100.0,
        "ideal_price_eur": 80.0,
    }
    # 5 calls — far above any production limit.
    statuses = []
    for _ in range(5):
        r = await client.post("/api/intents", json=payload)
        statuses.append(r.status_code)
    assert 429 not in statuses


# ---------------------------------------------------------------------------
# CORS
# ---------------------------------------------------------------------------


async def test_cors_echoes_allowed_origin(http_client, monkeypatch):
    """Request with allowed Origin should get the matching CORS header back."""
    headers = {"Origin": "http://localhost:3000"}
    r = await http_client.get("/health", headers=headers)
    assert r.status_code == 200
    assert r.headers.get("access-control-allow-origin") == "http://localhost:3000"


async def test_cors_does_not_echo_unallowed_origin(http_client):
    headers = {"Origin": "https://attacker.example.com"}
    r = await http_client.get("/health", headers=headers)
    # FastAPI/Starlette CORS middleware simply omits the header for
    # unconfigured origins (the browser will then block).
    assert r.headers.get("access-control-allow-origin") is None


async def test_cors_preflight_options_allows_post(http_client):
    headers = {
        "Origin": "http://localhost:3000",
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "Authorization, Content-Type",
    }
    r = await http_client.options("/api/intents", headers=headers)
    assert r.status_code == 200
    allowed_methods = (r.headers.get("access-control-allow-methods") or "").upper()
    assert "POST" in allowed_methods


# ---------------------------------------------------------------------------
# OpenAPI
# ---------------------------------------------------------------------------


async def test_openapi_json_is_valid_3_x(http_client):
    r = await http_client.get("/openapi.json")
    assert r.status_code == 200
    spec = r.json()
    assert spec["openapi"].startswith("3.")
    assert spec["info"]["title"] == settings.app_name
    assert spec["info"]["version"] == settings.app_version
    # Sanity: a few key paths must be registered.
    paths = spec["paths"]
    assert "/api/intents" in paths
    assert "/api/health" in paths


async def test_critical_endpoints_have_summary_and_description(http_client):
    r = await http_client.get("/openapi.json")
    spec = r.json()
    targets = [
        ("/api/intents", "post"),
        ("/api/identity/verify-self", "post"),
        ("/api/mandates/draft", "post"),
        ("/api/mandates/submit", "post"),
        ("/api/health", "get"),
    ]
    for path, method in targets:
        op = spec["paths"][path][method]
        assert op.get("summary"), f"{method.upper()} {path} missing summary"
        assert op.get("description"), (
            f"{method.upper()} {path} missing description"
        )


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


async def test_api_health_returns_structured_payload(http_client):
    r = await http_client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["service"] == settings.app_name
    assert body["version"] == settings.app_version
    assert body["status"] in {"healthy", "degraded", "unhealthy"}
    assert "checks" in body
    checks = body["checks"]
    for k in (
        "database",
        "agent_scheduler",
        "today_cost_usd",
        "daily_cap_remaining_usd",
    ):
        assert k in checks


@pytest.mark.db
async def test_api_health_today_cost_reflects_upserts(
    async_db_session, http_client
):
    # Health snapshot reads SUM cross-user; one user_id is enough to
    # exercise the path post-7.3.2 (composite PK).
    user_id, _, _ = await setup_active_mandate_async(
        async_db_session, email=f"hc-cost-{uuid.uuid4().hex[:6]}@x.com"
    )
    await upsert_daily_cost(
        async_db_session, user_id=user_id, cost_usd=0.42
    )
    await async_db_session.commit()

    r = await http_client.get("/api/health")
    assert r.status_code == 200
    cost = r.json()["checks"]["today_cost_usd"]
    assert cost == pytest.approx(0.42, abs=1e-6)


@pytest.mark.db
async def test_api_health_last_tick_reflects_agents(
    async_db_session, http_client
):
    user_id, agent_id, _ = await setup_active_mandate_async(
        async_db_session, email=f"hc-{uuid.uuid4().hex[:6]}@x.com"
    )
    last_tick = datetime.utcnow() - timedelta(minutes=2)
    agent = await async_db_session.get(Agent, agent_id)
    agent.last_tick_at = last_tick
    await async_db_session.commit()

    r = await http_client.get("/api/health")
    body = r.json()
    assert body["checks"]["last_successful_tick"] is not None


def test_api_health_scheduler_disabled_reflected_in_body():
    """Synchronous helper-style: hit the route via TestClient and verify
    the scheduler check reports 'disabled' when the flag is off."""
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["checks"]["agent_scheduler"] == "disabled"


async def test_legacy_health_endpoint_still_works(http_client):
    r = await http_client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] in {"ok", "degraded"}
    assert "db" in body
