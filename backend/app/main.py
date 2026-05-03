"""FastAPI application entry point."""
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from prometheus_fastapi_instrumentator import Instrumentator
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from sqlalchemy import text

from app.api import (
    _dev_endpoints,
    _test_endpoints,
    agents as agents_routes,
    auth as auth_routes,
    deals as deal_routes,
    health as health_routes,
    identity as identity_routes,
    intents as intent_routes,
    legal as legal_routes,
    mandates as mandate_routes,
    market as market_routes,
    matches as match_routes,
    negotiations as negotiation_routes,
    notifications as notification_routes,
    step_up as step_up_routes,
)
from app.core.config import settings
from app.core.db import engine
from app.core.error_handlers import moderation_error_handler
from app.core.logging import configure_logging, log
from app.core.rate_limit import limiter, rate_limit_exceeded_handler
from app.core.telemetry import setup_telemetry, shutdown_telemetry
from app.services import agent_scheduler, match_scheduler
from app.services.content_moderation import ModerationError
from app.services.kms.encryption import validate_master_key


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    log.info("app.startup", env=settings.app_env, name=settings.app_name)
    validate_master_key()  # hard-fail if KMS_MASTER_KEY missing or malformed
    setup_telemetry(app)
    match_scheduler.start_scheduler()
    agent_scheduler.start_scheduler()
    try:
        yield
    finally:
        agent_scheduler.shutdown_scheduler()
        match_scheduler.shutdown_scheduler()
        await engine.dispose()
        shutdown_telemetry()
        log.info("app.shutdown")


app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    lifespan=lifespan,
)

# Rate limiting (7.0). The limiter must be registered on `app.state` so
# slowapi's middleware + exception handler can find it; the middleware
# enforces `default_limits` for every route, and per-route decorators
# tighten or relax those limits as needed.
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, rate_limit_exceeded_handler)
app.add_middleware(SlowAPIMiddleware)

# Content moderation (7.1.4). Cross-cutting service-layer error → 422
# with the canonical detail envelope, so any service that calls
# `moderate_text(...)` automatically participates without router work.
app.add_exception_handler(ModerationError, moderation_error_handler)

# CORS (7.0). Origins from env (`CORS_ALLOWED_ORIGINS=a.com,b.com`).
# Credentials enabled so the frontend can send cookies / Authorization
# headers; the production origin must be explicit (not wildcard) for
# `allow_credentials=True` to be valid per the CORS spec.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_allowed_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)

# Prometheus metrics (7.2). Auto-instruments FastAPI handlers with
# `http_requests_total`, `http_request_duration_seconds`, and
# `http_requests_inprogress`; exposes them on /metrics in Prometheus
# text format. Custom domain metrics live in `app.core.metrics` and are
# imported below at module scope so they register with the global
# CollectorRegistry the moment the app boots — otherwise the first
# scrape on a freshly-started process would miss any counter that
# hadn't been incremented yet.
from app.core import metrics as _metrics  # noqa: F401

_instrumentator = Instrumentator(
    should_group_status_codes=False,
    should_ignore_untemplated=True,
    excluded_handlers=["/metrics"],
)
_instrumentator.instrument(app).expose(app, endpoint="/metrics", include_in_schema=False)

app.include_router(auth_routes.router)
app.include_router(identity_routes.router)
app.include_router(agents_routes.router)
app.include_router(mandate_routes.router)
app.include_router(step_up_routes.router)
app.include_router(intent_routes.router)
app.include_router(market_routes.router)
app.include_router(match_routes.router)
app.include_router(negotiation_routes.router)
app.include_router(deal_routes.router)
app.include_router(notification_routes.router)
app.include_router(health_routes.router)
app.include_router(legal_routes.router)
app.include_router(_test_endpoints.router)
app.include_router(_dev_endpoints.router)


@app.get("/health")
async def healthcheck() -> dict[str, Any]:
    db_ok = False
    db_error: str | None = None
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        db_ok = True
    except Exception as exc:
        db_error = type(exc).__name__
        log.warning("health.db_check_failed", error=db_error, message=str(exc))

    payload: dict[str, Any] = {
        "status": "ok" if db_ok else "degraded",
        "service": settings.app_name,
        "env": settings.app_env,
        "db": "ok" if db_ok else "down",
    }
    if db_error:
        payload["db_error"] = db_error
    return payload
