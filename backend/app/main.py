"""FastAPI application entry point."""
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from sqlalchemy import text

from app.api import auth as auth_routes
from app.core.config import settings
from app.core.db import engine
from app.core.logging import configure_logging, log


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    log.info("app.startup", env=settings.app_env, name=settings.app_name)
    try:
        yield
    finally:
        await engine.dispose()
        log.info("app.shutdown")


app = FastAPI(title=settings.app_name, lifespan=lifespan)
app.include_router(auth_routes.router)


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
