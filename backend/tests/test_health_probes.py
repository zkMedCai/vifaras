"""Liveness + readiness probe tests (brief task 7.2.5).

Coverage:
  1. /api/health/live → always 200 + {status: "alive"}
  2. /api/health/ready when scheduler disabled (default V0) → 200, scheduler="disabled"
  3. /api/health/ready when scheduler enabled + recent tick → 200, scheduler="healthy"
  4. /api/health/ready when scheduler enabled + stale tick → 503, scheduler startswith "stale"
  5. /api/health/ready when DB unreachable → 503, database startswith "unhealthy"

The scheduler heartbeat is read from the `SCHEDULER_LAST_TICK_TIMESTAMP`
Prometheus gauge (in-memory). Tests drive the gauge directly via `.set()`
and reset to 0.0 on teardown to avoid cross-test pollution.

DB unreachable scenario uses FastAPI's dependency override to yield a
session whose `execute()` raises — simpler than monkey-patching the
real engine and doesn't require shutting down testcontainers.
"""
from __future__ import annotations

from typing import Any

import pytest

from app.core.config import settings
from app.core.metrics import SCHEDULER_LAST_TICK_TIMESTAMP


@pytest.fixture
def reset_scheduler_gauge():
    """Snapshot + restore the in-memory scheduler heartbeat gauge."""
    before = SCHEDULER_LAST_TICK_TIMESTAMP._value.get()
    try:
        yield
    finally:
        SCHEDULER_LAST_TICK_TIMESTAMP.set(before)


# ---------------------------------------------------------------------------
# Liveness
# ---------------------------------------------------------------------------


async def test_liveness_returns_200_alive(http_client):
    r = await http_client.get("/api/health/live")
    assert r.status_code == 200
    assert r.json() == {"status": "alive"}


# ---------------------------------------------------------------------------
# Readiness — scheduler disabled (default V0)
# ---------------------------------------------------------------------------


async def test_readiness_ready_when_scheduler_disabled(
    http_client, monkeypatch
):
    """Default V0: enable_agent_scheduler=False → scheduler check skipped."""
    monkeypatch.setattr(settings, "enable_agent_scheduler", False)
    r = await http_client.get("/api/health/ready")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ready"
    assert body["checks"]["database"] == "healthy"
    assert body["checks"]["scheduler"] == "disabled"


# ---------------------------------------------------------------------------
# Readiness — scheduler enabled + heartbeat fresh
# ---------------------------------------------------------------------------


async def test_readiness_ready_when_scheduler_recent(
    http_client, monkeypatch, reset_scheduler_gauge
):
    """Scheduler on + heartbeat within 2× interval → healthy."""
    import time

    monkeypatch.setattr(settings, "enable_agent_scheduler", True)
    SCHEDULER_LAST_TICK_TIMESTAMP.set(time.time())  # just now

    r = await http_client.get("/api/health/ready")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ready"
    assert body["checks"]["scheduler"] == "healthy"


# ---------------------------------------------------------------------------
# Readiness — scheduler stale
# ---------------------------------------------------------------------------


async def test_readiness_503_when_scheduler_stale(
    http_client, monkeypatch, reset_scheduler_gauge
):
    """Heartbeat older than 2× interval → 503 + scheduler="stale: ..."."""
    import time

    monkeypatch.setattr(settings, "enable_agent_scheduler", True)
    monkeypatch.setattr(settings, "agent_scheduler_interval_seconds", 60)
    # 5 minutes ago — well past the 120s threshold
    SCHEDULER_LAST_TICK_TIMESTAMP.set(time.time() - 300)

    r = await http_client.get("/api/health/ready")
    assert r.status_code == 503
    body = r.json()
    assert body["status"] == "not_ready"
    assert body["checks"]["scheduler"].startswith("stale:")


async def test_readiness_no_data_when_scheduler_enabled_but_never_ticked(
    http_client, monkeypatch, reset_scheduler_gauge
):
    """Scheduler on but gauge never set (fresh boot) → no_data, still 200."""
    monkeypatch.setattr(settings, "enable_agent_scheduler", True)
    SCHEDULER_LAST_TICK_TIMESTAMP.set(0)  # never ticked

    r = await http_client.get("/api/health/ready")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ready"
    assert body["checks"]["scheduler"] == "no_data"


# ---------------------------------------------------------------------------
# Readiness — DB unreachable
# ---------------------------------------------------------------------------


async def test_readiness_503_when_db_unreachable(http_client):
    """Override get_db with a session whose execute() raises → 503."""
    from app.core.db import get_db
    from app.main import app

    class _BrokenSession:
        async def execute(self, *_a: Any, **_kw: Any) -> Any:
            raise RuntimeError("simulated DB outage")

    async def _broken_get_db():
        yield _BrokenSession()

    app.dependency_overrides[get_db] = _broken_get_db
    try:
        r = await http_client.get("/api/health/ready")
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert r.status_code == 503
    body = r.json()
    assert body["status"] == "not_ready"
    assert body["checks"]["database"].startswith("unhealthy:")
