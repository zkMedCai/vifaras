"""HTTP rate limiting via slowapi (brief task 7.0.1, extended at 7.1).

Two-tier strategy:

  - **Default**: 100 req/min/IP applied implicitly to every route.
    Bounds traffic from a single client without forcing per-route
    decoration. Configured at the `Limiter` level.

  - **Per-route, IP-keyed**: critical pre-auth endpoints (auth,
    identity verification, mandate draft) get tighter caps keyed by
    remote address so bursts from one IP can't enumerate or DOS the
    flow.

  - **Per-route, user-keyed (7.1)**: authenticated CRUD endpoints
    (intents/match/deals/negotiations) cap per `User.id` so one
    abusive account can't burn through a budget shared with quiet
    co-tenants on the same NAT/IP. The `user_key` extractor decodes
    the bearer access token; on a missing/invalid header it falls
    back to IP — the auth dependency then 401s the request, but the
    failed attempt has still been counted against an IP bucket so
    brute-force on the auth check itself is bounded.

Rate limit state is in-memory (slowapi default `MemoryStorage`).
Acceptable for single-worker V0; multi-worker production needs Redis
storage so caps apply globally — flagged in IDEAS_BACKLOG.

Disabled in tests by default: `enable_rate_limiting=False` makes the
limiter a no-op so existing test suites don't trip the caps. Tests
that need to exercise rate limiting flip the setting via monkeypatch.
"""
from __future__ import annotations

from fastapi import Request, status
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from starlette.responses import JSONResponse

from app.core.config import settings


def _key_func(request: Request) -> str:
    """Per-IP keying. Behind a load balancer, set `TRUSTED_PROXIES`
    and replace with `X-Forwarded-For`-aware extraction at 7.x."""
    return get_remote_address(request)


def user_key(request: Request) -> str:
    """Per-user keying for authenticated endpoints (7.1).

    Decodes the bearer access token to extract `sub` (user_id). On any
    failure (missing header, malformed scheme, expired/invalid JWT)
    falls back to IP via `get_remote_address`. The endpoint's auth
    dependency rejects the unauth'd request with 401 immediately after,
    but the limiter has already counted the attempt against the IP
    bucket — so brute force on the auth check itself is still bounded.

    Returns a namespaced string so user-keyed buckets and IP-keyed
    buckets can never alias by accident.
    """
    auth = request.headers.get("Authorization") or request.headers.get(
        "authorization"
    )
    if not auth:
        return get_remote_address(request)
    parts = auth.strip().split(maxsplit=1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1]:
        return get_remote_address(request)
    try:
        # Local import: avoids circular `core.security -> core.config`
        # under module-load and keeps `rate_limit` importable from
        # `main.py` early in the FastAPI startup sequence.
        from app.core.security import decode_access_token

        payload = decode_access_token(parts[1])
        sub = payload.get("sub")
        if sub:
            return f"user:{sub}"
    except Exception:
        pass
    return get_remote_address(request)


limiter = Limiter(
    key_func=_key_func,
    default_limits=[settings.rate_limit_default],
    enabled=settings.enable_rate_limiting,
)


async def rate_limit_exceeded_handler(
    request: Request, exc: RateLimitExceeded
) -> JSONResponse:
    """Custom 429 with the `Retry-After` header slowapi computes.

    Body shape mirrors the project's standard error envelope so the
    frontend can branch on `code` rather than parse a string. 7.1.5
    adds an audit-log emit (sync, fire-and-forget on failure) so abuse
    review has a record of every cap hit — both authenticated (user_id
    from JWT when present) and anonymous (auth-endpoint bursts).
    """
    retry_after = getattr(exc, "retry_after", None)
    headers = {}
    if retry_after is not None:
        headers["Retry-After"] = str(int(retry_after))

    # Metrics hook — record cap hit per endpoint for Grafana panels.
    try:
        from app.core.metrics import RATE_LIMIT_HITS_TOTAL
        RATE_LIMIT_HITS_TOTAL.labels(endpoint=request.url.path).inc()
    except Exception:
        pass

    # Audit hook — never break the response on audit failure.
    try:
        from app.core.db import AsyncSessionLocal
        from app.core.security import try_extract_user_id
        from app.services import audit_service

        auth_header = request.headers.get("Authorization") or request.headers.get(
            "authorization"
        )
        user_id = try_extract_user_id(auth_header)
        ip = get_remote_address(request)
        async with AsyncSessionLocal() as session:
            await audit_service.log_security_event(
                session,
                action=audit_service.SecurityActions.RATE_LIMIT_API_HIT,
                user_id=user_id,
                actor_ip=ip,
                params={
                    "endpoint": request.url.path,
                    "method": request.method,
                    "limit": str(exc.detail) if exc.detail else "",
                },
                success=False,
                error_code="rate_limited",
            )
            await session.commit()
    except Exception:
        # Audit must never break the response. Logged inside log_security_event
        # already; nothing more to do here.
        pass

    return JSONResponse(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        content={
            "code": "rate_limited",
            "message": "Too many requests. Please slow down.",
            "limit": str(exc.detail) if exc.detail else "",
        },
        headers=headers,
    )
