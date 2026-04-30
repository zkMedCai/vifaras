"""HTTP rate limiting via slowapi (brief task 7.0.1).

Two-tier strategy for V0:

  - **Default**: 100 req/min/IP applied implicitly to every route.
    Bounds traffic from a single client without forcing per-route
    decoration. Configured at the `Limiter` level.

  - **Stricter per-route**: critical POST endpoints (intent CRUD,
    mandate draft, identity verification) get explicit decorators
    that override the default with a tighter window. These are the
    write paths most attractive to abuse / accidental retry storms.

Rate limit state is in-memory (slowapi default `MemoryStorage`).
Acceptable for single-worker V0; multi-worker production needs Redis
storage so caps apply globally — flagged in IDEAS_BACKLOG.

Disabled in tests by default: `enable_rate_limiting=False` makes the
limiter a no-op so existing test suites don't trip the caps. Tests
that need to exercise rate limiting flip the setting via monkeypatch.
"""
from __future__ import annotations

from fastapi import HTTPException, Request, status
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from starlette.responses import JSONResponse

from app.core.config import settings


def _key_func(request: Request) -> str:
    """Per-IP keying. Behind a load balancer, set `TRUSTED_PROXIES`
    and replace with `X-Forwarded-For`-aware extraction at 7.x."""
    return get_remote_address(request)


limiter = Limiter(
    key_func=_key_func,
    default_limits=[settings.rate_limit_default],
    enabled=settings.enable_rate_limiting,
)


def rate_limit_exceeded_handler(
    request: Request, exc: RateLimitExceeded
) -> JSONResponse:
    """Custom 429 with the `Retry-After` header slowapi computes.

    Body shape mirrors the project's standard error envelope so the
    frontend can branch on `code` rather than parse a string.
    """
    retry_after = getattr(exc, "retry_after", None)
    headers = {}
    if retry_after is not None:
        headers["Retry-After"] = str(int(retry_after))
    return JSONResponse(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        content={
            "code": "rate_limited",
            "message": "Too many requests. Please slow down.",
            "limit": str(exc.detail) if exc.detail else "",
        },
        headers=headers,
    )
