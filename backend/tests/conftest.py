"""Shared pytest fixtures.

Per the founder's call (overriding brief §8), all DB-touching tests run
against a real `pgvector/pgvector:pg16` container managed by testcontainers.
SQLite is rejected: schema uses Postgres-only types (JSONB / UUID / Vector)
and services rely on Postgres features (cosine sim, JSONB queries,
SELECT FOR UPDATE) that don't translate.

Lifecycle:
  - `_pg_container` (session scope): lazily booted on first `db_session`
    request — sets POSTGRES_* env vars *before* `app.core.db` is imported,
    then runs `alembic upgrade head`. Stopped at session end.
    Tests that don't request `db_session` (or any descendant) never pay
    the container cost — `pytest -m "not db"` is genuinely fast.
  - `db_session` (function scope): wraps each test in an outer transaction
    with `join_transaction_mode="create_savepoint"`; rolls back on teardown
    so nothing leaks across tests.

Tests that touch the DB declare `@pytest.mark.db`. Pure computational tests
skip the marker and run with no container/session overhead.

Important: do NOT import `app.core.db` at module top in test files. It is
imported lazily inside `db_session` *after* env vars are set; importing it
at module top would create the engines with default `localhost:5432`
settings before the container is up.
"""
from __future__ import annotations

import os
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any, Callable

import pytest
from testcontainers.postgres import PostgresContainer


# ---------------------------------------------------------------------------
# Postgres testcontainer (session scope, lazy)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def _pg_container() -> Iterator[PostgresContainer]:
    """Boot pgvector container and apply migrations once for the session.

    Lazy: only invoked when a test requests `db_session` (directly or
    transitively). Pure computational tests pay zero container cost.
    """
    container = PostgresContainer(
        image="pgvector/pgvector:pg16",
        username="test",
        password="test",
        dbname="test",
    )
    container.start()
    try:
        os.environ["POSTGRES_HOST"] = container.get_container_host_ip()
        os.environ["POSTGRES_PORT"] = str(container.get_exposed_port(5432))
        os.environ["POSTGRES_USER"] = "test"
        os.environ["POSTGRES_PASSWORD"] = "test"
        os.environ["POSTGRES_DB"] = "test"

        # Imports inside the fixture so they read the just-set env vars.
        from alembic import command
        from alembic.config import Config

        repo_root = os.path.dirname(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        )
        cfg = Config(os.path.join(repo_root, "alembic.ini"))
        command.upgrade(cfg, "head")

        yield container
    finally:
        container.stop()


# ---------------------------------------------------------------------------
# DB session (function scope, auto-rollback)
# ---------------------------------------------------------------------------


@pytest.fixture
def db_session(_pg_container: PostgresContainer) -> Iterator[Any]:
    """Yields a sync Session inside an outer transaction; rolls back on teardown.

    Test code may call `session.commit()` freely — it becomes a savepoint
    release thanks to `join_transaction_mode="create_savepoint"`. The outer
    transaction is rolled back at fixture teardown, so the next test sees
    a clean DB.
    """
    from sqlalchemy.orm import Session

    from app.core.db import sync_engine

    connection = sync_engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection, join_transaction_mode="create_savepoint")

    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()


# ---------------------------------------------------------------------------
# Async DB session + FastAPI httpx client (for endpoint tests)
# ---------------------------------------------------------------------------


@pytest.fixture
async def async_db_session(_pg_container: PostgresContainer):
    """Async equivalent of `db_session`: outer transaction + savepoint commits.

    Use this in endpoint tests via the `http_client` fixture, which overrides
    the FastAPI `get_db` dependency to yield this same session — so reads
    after the API call see what the API wrote, and rollback on teardown
    wipes everything.
    """
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.core.db import engine

    async with engine.connect() as connection:
        transaction = await connection.begin()
        try:
            async with AsyncSession(
                bind=connection,
                expire_on_commit=False,
                join_transaction_mode="create_savepoint",
            ) as session:
                yield session
        finally:
            await transaction.rollback()


@pytest.fixture
async def http_client(async_db_session):
    """httpx AsyncClient wired into the FastAPI app via ASGITransport.

    Overrides `get_db` so the API uses the same `async_db_session` as the test.
    """
    from httpx import ASGITransport, AsyncClient

    from app.core.db import get_db
    from app.main import app

    async def _override_get_db():
        yield async_db_session

    app.dependency_overrides[get_db] = _override_get_db
    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            yield client
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.fixture
async def authenticated_client(http_client):
    """Factory fixture: `authenticated_client(tier=N)` → (client, ctx).

    Mints a fresh JWT access token at the requested tier and installs it as
    `Authorization: Bearer ...` on the shared `http_client`. Skips the full
    register/login dance — `require_tier` decodes the JWT only, no DB touch.

    Returns the same `http_client` (auth header now set) plus a context
    dict `{user_id, tier, access_token}`. Auth header is removed at fixture
    teardown so the next test starts clean.

    For tests that need a real registered user (e.g. login coverage), do
    the registration explicitly inside the test — this fixture is for the
    common case of "I just need a valid JWT at tier N".
    """
    import uuid as _uuid

    from app.core.security import create_access_token

    def _factory(tier: int = 0, user_id: str | None = None) -> tuple[Any, dict[str, Any]]:
        if user_id is None:
            user_id = str(_uuid.uuid4())
        token = create_access_token(user_id=user_id, tier=tier)
        http_client.headers["Authorization"] = f"Bearer {token}"
        return http_client, {
            "user_id": user_id,
            "tier": tier,
            "access_token": token,
        }

    try:
        yield _factory
    finally:
        http_client.headers.pop("Authorization", None)


# ---------------------------------------------------------------------------
# Mock: Anthropic client (canned responses)
# ---------------------------------------------------------------------------


def _make_message(content_blocks: list[Any], stop_reason: str) -> SimpleNamespace:
    """Mimic anthropic.types.Message just enough for the orchestrator loop."""
    return SimpleNamespace(content=content_blocks, stop_reason=stop_reason)


def text_block(text: str) -> SimpleNamespace:
    return SimpleNamespace(type="text", text=text)


def tool_use_block(
    name: str,
    input_dict: dict[str, Any],
    id: str = "tu_test_1",
) -> SimpleNamespace:
    return SimpleNamespace(type="tool_use", id=id, name=name, input=input_dict)


class FakeAnthropicClient:
    """Drop-in for `anthropic.Anthropic` — pops responses from a queue.

    Construct via the `anthropic_mock` fixture factory:

        client = anthropic_mock([
            anthropic_mock.message(
                [anthropic_mock.tool_use_block("check_state", {})],
                stop_reason="tool_use",
            ),
            anthropic_mock.message(
                [anthropic_mock.text_block("Done.")], stop_reason="end_turn",
            ),
        ])
        AgentOrchestrator(db, anthropic_client=client)

    Inspect `client.calls` to assert how the orchestrator drove the model.
    """

    def __init__(self, responses: list[SimpleNamespace]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs: Any) -> SimpleNamespace:
        self.calls.append(kwargs)
        if not self._responses:
            raise RuntimeError(
                "FakeAnthropicClient ran out of canned responses; "
                "the test is asking the model to do more than it scripted for."
            )
        return self._responses.pop(0)


@pytest.fixture
def anthropic_mock() -> Callable[..., FakeAnthropicClient]:
    """Factory: `anthropic_mock([msg1, msg2, ...])` → FakeAnthropicClient."""

    def _make(
        responses: list[SimpleNamespace] | None = None,
    ) -> FakeAnthropicClient:
        return FakeAnthropicClient(responses or [])

    _make.message = _make_message  # type: ignore[attr-defined]
    _make.text_block = text_block  # type: ignore[attr-defined]
    _make.tool_use_block = tool_use_block  # type: ignore[attr-defined]
    return _make


# ---------------------------------------------------------------------------
# Mock: Self Protocol verifier (placeholder until task 2.3 lands)
# ---------------------------------------------------------------------------


_DEFAULT_SELF_RESPONSE: dict[str, Any] = {
    "verified": True,
    "nullifier_hash": "mock-self-nullifier",
    "attributes": {"adult": True, "country": "IT", "valid": True},
}


@pytest.fixture
def self_verifier_mock() -> SimpleNamespace:
    """Stand-in for the (future, task 2.3) Self Protocol HTTP verifier.

    Until `app.services.identity_service` exists this fixture exposes:
      - `set_response(payload)`: declare what the verifier should return next
      - `calls`: list of recorded calls
      - `fake_post(*args, **kwargs)`: an httpx-shaped Response

    When 2.3 lands, the test will wire it into the real call site, e.g.:
      monkeypatch.setattr(
          "app.services.identity_service._post_to_verifier",
          self_verifier_mock.fake_post,
      )
    """
    state: dict[str, Any] = {
        "response": dict(_DEFAULT_SELF_RESPONSE),
        "calls": [],
    }

    def fake_post(*args: Any, **kwargs: Any) -> SimpleNamespace:
        state["calls"].append({"args": args, "kwargs": kwargs})
        return SimpleNamespace(
            status_code=200,
            json=lambda: dict(state["response"]),
            raise_for_status=lambda: None,
        )

    return SimpleNamespace(
        set_response=lambda payload: state.update(response=payload),
        calls=state["calls"],
        fake_post=fake_post,
    )
