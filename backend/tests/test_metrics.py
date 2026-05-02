"""Prometheus metrics tests (brief task 7.2.2).

Coverage:
  1. /metrics endpoint returns 200 with Prometheus text/plain format
  2. /metrics is unauthenticated (V0 dev open scrape)
  3. /metrics excluded from self-instrumented http_requests_total
  4. vifaras_signup_completed_total increments on successful signup
  5. vifaras_login_completed_total increments on successful login
  6. vifaras_rate_limit_hits_total{endpoint} increments on 429
  7. vifaras_moderation_rejections_total{field,code} increments on 422
  8. vifaras_intents_created_total{category,side} increments on intent create

Prometheus counters are process-global. We assert *deltas* (post - pre)
rather than absolute values so accumulated state from other tests in the
same session doesn't break us.
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from prometheus_client import REGISTRY

from app.core.config import settings
from app.core.rate_limit import limiter
from app.core.security import create_access_token


def _sample(name: str, labels: dict[str, str] | None = None) -> float:
    """Read a single sample from the default Prometheus registry."""
    val = REGISTRY.get_sample_value(name, labels)
    return float(val) if val is not None else 0.0


def _fake_verified_registration() -> SimpleNamespace:
    return SimpleNamespace(
        credential_id=b"mock-credential-id-bytes",
        credential_public_key=b"mock-cose-encoded-public-key-bytes",
        sign_count=0,
    )


def _fake_credential() -> dict[str, Any]:
    return {
        "id": "mock-cred",
        "rawId": "mock-cred",
        "type": "public-key",
        "response": {"attestationObject": "mock", "clientDataJSON": "mock"},
    }


# ---------------------------------------------------------------------------
# /metrics endpoint shape
# ---------------------------------------------------------------------------


async def test_metrics_endpoint_returns_prometheus_format(http_client):
    response = await http_client.get("/metrics")
    assert response.status_code == 200
    # prometheus-fastapi-instrumentator uses the canonical openmetrics
    # content type; a 'text/plain' substring covers both that and the
    # legacy `text/plain` variant exposed by older clients.
    assert "text/plain" in response.headers.get("content-type", "")
    body = response.text
    # HELP and TYPE lines are required by the Prometheus exposition format.
    assert "# HELP" in body
    assert "# TYPE" in body


async def test_metrics_endpoint_unauthenticated(http_client):
    """No Authorization header — /metrics still returns 200 (V0 dev)."""
    response = await http_client.get("/metrics")
    assert response.status_code == 200


async def test_metrics_endpoint_excluded_from_self_instrumentation(
    http_client,
):
    """`/metrics` requests must not bump http_requests_total — otherwise
    every scrape would inflate its own counters and the panels would lie."""
    before = _sample(
        "http_requests_total",
        {"handler": "/metrics", "method": "GET", "status": "2xx"},
    )
    # Hit /metrics three times.
    for _ in range(3):
        await http_client.get("/metrics")
    after = _sample(
        "http_requests_total",
        {"handler": "/metrics", "method": "GET", "status": "2xx"},
    )
    assert after == before  # excluded_handlers=["/metrics"] in main.py


# ---------------------------------------------------------------------------
# Auth metrics
# ---------------------------------------------------------------------------


@pytest.mark.db
async def test_signup_completed_counter_increments(
    http_client, monkeypatch
):
    monkeypatch.setattr(
        "app.services.auth_service.verify_registration_response",
        lambda **_: _fake_verified_registration(),
    )
    before = _sample("vifaras_signup_completed_total")

    begin = await http_client.post(
        "/api/auth/register/begin",
        json={"email": f"metrics-{uuid.uuid4().hex[:8]}@example.com"},
    )
    assert begin.status_code == 200
    complete = await http_client.post(
        "/api/auth/register/complete",
        json={
            "credential": _fake_credential(),
            "challenge_token": begin.json()["challenge_token"],
        },
    )
    assert complete.status_code == 200

    after = _sample("vifaras_signup_completed_total")
    assert after == before + 1


@pytest.mark.db
async def test_login_completed_counter_increments(
    http_client, monkeypatch
):
    monkeypatch.setattr(
        "app.services.auth_service.verify_registration_response",
        lambda **_: _fake_verified_registration(),
    )
    monkeypatch.setattr(
        "app.services.auth_service.verify_authentication_response",
        lambda **_: SimpleNamespace(new_sign_count=1),
    )
    email = f"login-metric-{uuid.uuid4().hex[:8]}@example.com"

    # Register first.
    begin_r = await http_client.post(
        "/api/auth/register/begin", json={"email": email}
    )
    await http_client.post(
        "/api/auth/register/complete",
        json={
            "credential": _fake_credential(),
            "challenge_token": begin_r.json()["challenge_token"],
        },
    )

    before = _sample("vifaras_login_completed_total")

    begin_l = await http_client.post(
        "/api/auth/login/begin", json={"email": email}
    )
    complete_l = await http_client.post(
        "/api/auth/login/complete",
        json={
            "credential": _fake_credential(),
            "challenge_token": begin_l.json()["challenge_token"],
        },
    )
    assert complete_l.status_code == 200

    after = _sample("vifaras_login_completed_total")
    assert after == before + 1


# ---------------------------------------------------------------------------
# Security metrics
# ---------------------------------------------------------------------------


@pytest.mark.db
async def test_rate_limit_hits_counter_increments(
    enable_limiter, monkeypatch, authenticated_client
):
    """A 429 on /api/intents bumps rate_limit_hits_total{endpoint}."""
    monkeypatch.setattr(settings, "rate_limit_post_strict", "1/minute")
    limiter.reset()

    client, _ = authenticated_client(tier=2)
    payload = {
        "side": "buy",
        "title": "macbook",
        "category": "electronics_laptops",
        "reservation_price_eur": 1200.0,
        "ideal_price_eur": 1000.0,
    }

    before = _sample(
        "vifaras_rate_limit_hits_total", {"endpoint": "/api/intents"}
    )
    await client.post("/api/intents", json=payload)  # consumes the 1/min slot
    r2 = await client.post("/api/intents", json=payload)  # this is the 429
    assert r2.status_code == 429

    after = _sample(
        "vifaras_rate_limit_hits_total", {"endpoint": "/api/intents"}
    )
    assert after == before + 1


@pytest.mark.db
async def test_moderation_rejections_counter_increments(
    http_client, async_db_session
):
    """A 422 ModerationError on intent create bumps moderation_rejections_total."""
    from app.models.schema import User
    from tests.factories import default_user_kwargs

    user_id = str(uuid.uuid4())
    user = User(
        id=user_id,
        **default_user_kwargs(tier=2, email=f"mod-{user_id[:8]}@example.com"),
    )
    async_db_session.add(user)
    await async_db_session.commit()
    http_client.headers["Authorization"] = (
        f"Bearer {create_access_token(user_id=user_id, tier=2)}"
    )

    labels = {"field": "title", "code": "too_long"}
    before = _sample("vifaras_moderation_rejections_total", labels)
    response = await http_client.post(
        "/api/intents",
        json={
            "side": "buy",
            "title": "x" * 5000,  # blows past MAX_TITLE_LEN
            "category": "electronics_laptops",
            "reservation_price_eur": 100.0,
            "ideal_price_eur": 80.0,
            "duration_days": 14,
        },
    )
    assert response.status_code == 422
    body = response.json()
    assert body["detail"]["field"] == "title"
    assert body["detail"]["code"] == "too_long"

    after = _sample("vifaras_moderation_rejections_total", labels)
    assert after == before + 1


# ---------------------------------------------------------------------------
# Business metrics
# ---------------------------------------------------------------------------


@pytest.mark.db
async def test_intents_created_counter_increments(
    http_client, async_db_session, monkeypatch
):
    """Successful POST /api/intents bumps intents_created_total{category,side}."""
    from app.services import embedding_service
    from app.models.schema import User
    from tests.factories import default_user_kwargs

    monkeypatch.setenv("EMBEDDING_BACKEND", "fake")
    embedding_service._reset_singleton_for_tests()

    user_id = str(uuid.uuid4())
    user = User(
        id=user_id,
        **default_user_kwargs(tier=2, email=f"int-{user_id[:8]}@example.com"),
    )
    async_db_session.add(user)
    await async_db_session.commit()
    http_client.headers["Authorization"] = (
        f"Bearer {create_access_token(user_id=user_id, tier=2)}"
    )

    labels = {"category": "electronics_laptops", "side": "sell"}
    before = _sample("vifaras_intents_created_total", labels)
    response = await http_client.post(
        "/api/intents",
        json={
            "side": "sell",
            "title": "MacBook Pro 14",
            "description": "Usato 6 mesi.",
            "category": "electronics_laptops",
            "reservation_price_eur": 1200.0,
            "ideal_price_eur": 1400.0,
            "duration_days": 14,
            "hard_constraints": {"location": "Roma, IT"},
            "soft_preferences": {},
        },
    )
    assert response.status_code == 201, response.text

    after = _sample("vifaras_intents_created_total", labels)
    assert after == before + 1
