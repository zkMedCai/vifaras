"""Global FastAPI exception handlers (brief task 7.1.4).

Service-layer typed errors that are genuinely cross-cutting — raised
from many services with no router-specific extensions — are wired here
as application-level handlers instead of duplicating `try/except` +
`_to_http()` boilerplate in every endpoint.

Precedent: `RateLimitExceeded` (slowapi) is registered globally in
`main.py` for the same reason.

When to add a handler here vs. extend a router's `_to_http`:
  - Cross-cutting + uniform envelope (no `next_step` etc.) → here
  - Single-router or needs per-error specialisation → router's `_to_http`
"""
from __future__ import annotations

from fastapi import Request
from slowapi.util import get_remote_address
from starlette.responses import JSONResponse

from app.services.content_moderation import ModerationError


async def moderation_error_handler(
    request: Request, exc: ModerationError
) -> JSONResponse:
    """Map `ModerationError` → 422 with the canonical detail envelope.

    Shape `{"detail": {"code", "message", "field"}}` mirrors the
    `_to_http(...)` HTTPException body produced elsewhere in the API,
    so frontend error decoding (`error.body.detail.code`) works without
    branching on which mechanism raised the error.

    7.1.5 adds an audit-log emit (fire-and-forget on failure) so abuse
    review has a record of every rejection.
    """
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
                action=audit_service.SecurityActions.MODERATION_REJECTED,
                user_id=user_id,
                actor_ip=ip,
                params={
                    "endpoint": request.url.path,
                    "method": request.method,
                    "field": exc.field,
                },
                success=False,
                error_code=exc.code,
            )
            await session.commit()
    except Exception:
        pass

    return JSONResponse(
        status_code=exc.http_status,
        content={
            "detail": {
                "code": exc.code,
                "message": str(exc),
                "field": exc.field,
            }
        },
    )
