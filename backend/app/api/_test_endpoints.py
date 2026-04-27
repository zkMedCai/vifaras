"""Dev-only endpoints used to exercise the tier gating dependency in tests.

Registered ONLY when `settings.app_env == "dev"`. In any other environment
the router is empty so the endpoints don't exist.

Each endpoint is a no-op that returns the authenticated user's `(user_id,
tier)` — useful for asserting that `require_tier(N)` admits/rejects
correctly without coupling the test to any business endpoint.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from app.core.config import settings
from app.core.security import CurrentUser, require_tier

router = APIRouter(prefix="/api/_test", tags=["_test (dev only)"])


if settings.app_env == "dev":

    @router.get("/tier0")
    async def tier0_endpoint(
        user: CurrentUser = Depends(require_tier(0)),
    ) -> dict[str, object]:
        return {"ok": True, "user_id": user.user_id, "tier": user.tier}

    @router.get("/tier1")
    async def tier1_endpoint(
        user: CurrentUser = Depends(require_tier(1)),
    ) -> dict[str, object]:
        return {"ok": True, "user_id": user.user_id, "tier": user.tier}

    @router.get("/tier2")
    async def tier2_endpoint(
        user: CurrentUser = Depends(require_tier(2)),
    ) -> dict[str, object]:
        return {"ok": True, "user_id": user.user_id, "tier": user.tier}
