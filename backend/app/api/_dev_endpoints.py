"""Dev-only diagnostics endpoints (brief task 4.2).

Distinct from `_test_endpoints` (which gates by `app_env == "dev"` and is
purely for tier-gating coverage) — these endpoints expose internal service
state (cache hit rates, OpenAI cost estimate, error counters) that should
NEVER be visible in production. Gated by `settings.enable_dev_endpoints`
(default `False`); in production this flag stays off and every request
returns 404 even though the route is registered.

The route is registered unconditionally so tests can flip the flag at
runtime (`monkeypatch.setattr(settings, "enable_dev_endpoints", True)`)
without needing to rebuild the app. The handler reads the flag per-request.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.core.config import settings
from app.services import embedding_service

router = APIRouter(prefix="/api/_dev", tags=["_dev (gated)"])


@router.get("/embedding-stats")
async def embedding_stats() -> dict:
    """Snapshot of EmbeddingService telemetry. 404 unless dev flag is on."""
    if not settings.enable_dev_endpoints:
        raise HTTPException(
            status_code=404, detail={"code": "not_found", "message": "Not Found"}
        )
    return embedding_service.get_embedding_service().stats()
