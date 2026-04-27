"""Database engines and session factories.

Two engines on the same database:
  - Async (default for new FastAPI endpoints / services).
  - Sync (used by the §5 scaffold — `mandate_verifier.py` operates on a
    sqlalchemy.orm.Session). Will also be reused by Alembic in 1.2.
"""
from collections.abc import AsyncIterator, Iterator

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings

engine: AsyncEngine = create_async_engine(
    settings.database_url_async,
    echo=False,
    pool_pre_ping=True,
)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)

sync_engine: Engine = create_engine(
    settings.database_url_sync,
    echo=False,
    pool_pre_ping=True,
)
SyncSessionLocal = sessionmaker(bind=sync_engine, autoflush=False, autocommit=False)


async def get_db() -> AsyncIterator[AsyncSession]:
    async with AsyncSessionLocal() as session:
        yield session


def get_sync_db() -> Iterator[Session]:
    db = SyncSessionLocal()
    try:
        yield db
    finally:
        db.close()
