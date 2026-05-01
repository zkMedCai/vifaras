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
async def _async_db_connection(_pg_container: PostgresContainer):
    """Async connection wrapped in an outer transaction (rollback on teardown).

    Both `async_db_session` (test-side reads) and the per-request sessions
    minted by `http_client` bind to this same connection — so all writes
    are visible across both sides, and the outer rollback wipes the test
    cleanly.
    """
    from app.core.db import engine

    async with engine.connect() as connection:
        transaction = await connection.begin()
        try:
            yield connection
        finally:
            await transaction.rollback()


@pytest.fixture
async def async_db_session(_async_db_connection):
    """Test-side AsyncSession for reading state after API calls.

    Each test gets one session here for assertions. API requests use their
    own (fresh) sessions minted by `http_client` — sharing one session
    across the whole test breaks `with_for_update()` (savepoint+greenlet
    interaction in SQLAlchemy 2.0 async).
    """
    from sqlalchemy.ext.asyncio import AsyncSession

    async with AsyncSession(
        bind=_async_db_connection,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    ) as session:
        yield session


@pytest.fixture
async def http_client(_async_db_connection, async_db_session):
    """httpx AsyncClient wired into the FastAPI app via ASGITransport.

    Each API request gets a **fresh** AsyncSession bound to the test's
    connection (so writes are inside the outer transaction and visible to
    `async_db_session`'s reads, but each request has its own session
    lifecycle). This mirrors production where `get_db` mints a new session
    per request via `AsyncSessionLocal()`.

    Why fresh-per-request: sharing one session across requests breaks
    sequences like `commit → with_for_update`, because SQLAlchemy's async
    savepoint creation drops out of the greenlet bridge after a commit
    on the same session. Per-request sessions auto-begin cleanly.
    """
    from httpx import ASGITransport, AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.core.db import get_db
    from app.main import app

    async def _override_get_db():
        async with AsyncSession(
            bind=_async_db_connection,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        ) as session:
            yield session

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
# Rate limiter toggle (7.0 / 7.1)
# ---------------------------------------------------------------------------


@pytest.fixture
def enable_limiter(monkeypatch):
    """Flip slowapi on for the duration of one test, then reset state.

    The limiter is OFF by default in the test suite (`enable_rate_limiting
    = False`) so the 370+ existing tests don't trip caps when they fire
    bursts of requests in parametrize loops. Tests that need to assert
    429 behaviour explicitly opt in via this fixture.

    `limiter.reset()` clears the in-memory bucket state both on entry and
    teardown — between tests we want zero leakage of counters.
    """
    from app.core.rate_limit import limiter

    monkeypatch.setattr(limiter, "enabled", True)
    limiter.reset()
    try:
        yield
    finally:
        limiter.reset()
        monkeypatch.setattr(limiter, "enabled", False)


# ---------------------------------------------------------------------------
# Mock: Anthropic client (canned responses)
# ---------------------------------------------------------------------------


def _make_message(
    content_blocks: list[Any],
    stop_reason: str,
    *,
    input_tokens: int = 1000,
    output_tokens: int = 200,
) -> SimpleNamespace:
    """Mimic anthropic.types.Message just enough for the orchestrator loop.

    Default usage values (1000 in, 200 out) keep cost-tracking tests
    deterministic without forcing every test to pass them explicitly.
    """
    usage = SimpleNamespace(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )
    return SimpleNamespace(
        content=content_blocks,
        stop_reason=stop_reason,
        usage=usage,
    )


def text_block(text: str) -> SimpleNamespace:
    return SimpleNamespace(type="text", text=text)


def tool_use_block(
    name: str,
    input_dict: dict[str, Any],
    id: str = "tu_test_1",
) -> SimpleNamespace:
    return SimpleNamespace(type="tool_use", id=id, name=name, input=input_dict)


class FakeAnthropicClient:
    """Drop-in for `anthropic.AsyncAnthropic` — pops responses from a queue.

    Async since the 6.3.b orchestrator uses `AsyncAnthropic`. Each entry in
    the queue is either a fake `Message` (returned) or an `Exception`
    (raised) — the second form lets tests cover the `claude_error` branch.

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
        AgentOrchestrator(anthropic_client=client)

    Inspect `client.calls` to assert how the orchestrator drove the model.
    """

    def __init__(self, responses: list[Any]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []
        self.messages = SimpleNamespace(create=self._create)

    async def _create(self, **kwargs: Any) -> SimpleNamespace:
        self.calls.append(kwargs)
        if not self._responses:
            raise RuntimeError(
                "FakeAnthropicClient ran out of canned responses; "
                "the test is asking the model to do more than it scripted for."
            )
        nxt = self._responses.pop(0)
        if isinstance(nxt, BaseException):
            raise nxt
        return nxt


@pytest.fixture
def anthropic_mock() -> Callable[..., FakeAnthropicClient]:
    """Factory: `anthropic_mock([msg1, msg2, ...])` → FakeAnthropicClient."""

    def _make(
        responses: list[Any] | None = None,
    ) -> FakeAnthropicClient:
        return FakeAnthropicClient(responses or [])

    _make.message = _make_message  # type: ignore[attr-defined]
    _make.text_block = text_block  # type: ignore[attr-defined]
    _make.tool_use_block = tool_use_block  # type: ignore[attr-defined]
    return _make


# ---------------------------------------------------------------------------
# Mock: Self Protocol verifier (task 2.3+)
# ---------------------------------------------------------------------------


def _self_response_template(
    *,
    user_identifier: str,
    nullifier: str,
    attributes: dict[str, Any],
    scope: str = "marketplace-it-v0",
    verified: bool = True,
    error_code: str | None = None,
) -> dict[str, Any]:
    """Build a Self verifier response in the canonical wire format."""
    if not verified:
        return {
            "verified": False,
            "errorCode": error_code or "PROOF_INVALID",
            "errorMessage": f"mock: {error_code}",
            "scope": scope,
            "userIdentifier": user_identifier,
        }
    return {
        "verified": True,
        "nullifier": nullifier,
        "attributes": attributes,
        "scope": scope,
        "userIdentifier": user_identifier,
    }


@pytest.fixture
def self_verifier_mock(monkeypatch) -> SimpleNamespace:
    """Auto-patches `identity_service._post_to_self_verifier`.

    Tests drive it via:
        self_verifier_mock.set_response(payload)        # next call returns this
        self_verifier_mock.set_error(httpx.TimeoutException("..."))  # next call raises
        self_verifier_mock.calls                         # list of recorded payloads
        self_verifier_mock.reset()                       # clear state

    Preset factories (return payloads ready for `set_response`):
        valid_italian_adult_proof(user_identifier=..., nullifier=...)
        expired_document_proof(user_identifier=...)
        non_italian_proof(user_identifier=...)
        minor_proof(user_identifier=...)
        invalid_proof(user_identifier=...)        # verified=false
        nullifier_reuse_proof(user_identifier=...) # verified=false NULLIFIER_REUSE

    The patch is auto-installed on fixture entry — no explicit monkeypatch
    in the test body. Tests that don't request the fixture pay nothing.
    """
    import httpx

    state: dict[str, Any] = {
        "response": None,
        "error": None,
        "calls": [],
    }

    async def fake_post(payload: dict[str, Any]) -> dict[str, Any]:
        state["calls"].append(payload)
        if state["error"] is not None:
            raise state["error"]
        if state["response"] is None:
            raise RuntimeError(
                "self_verifier_mock: no response set; call set_response() "
                "or set_error() before invoking the verify-self endpoint"
            )
        return dict(state["response"])

    monkeypatch.setattr(
        "app.services.identity_service._post_to_self_verifier",
        fake_post,
    )

    def set_response(payload: dict[str, Any]) -> None:
        state["response"] = payload
        state["error"] = None

    def set_error(exc: BaseException) -> None:
        state["error"] = exc
        state["response"] = None

    def reset() -> None:
        state["response"] = None
        state["error"] = None
        state["calls"].clear()

    # ------- preset factories -------

    def valid_italian_adult_proof(
        *,
        user_identifier: str,
        nullifier: str = "self_nullifier_valid_it_adult",
        document_expiry: str = "2030-04-15",
    ) -> dict[str, Any]:
        return _self_response_template(
            user_identifier=user_identifier,
            nullifier=nullifier,
            attributes={
                "isAdult": True,
                "issuingState": "IT",
                "documentValid": True,
                "documentExpiry": document_expiry,
            },
        )

    def expired_document_proof(
        *,
        user_identifier: str,
        nullifier: str = "self_nullifier_expired_doc",
    ) -> dict[str, Any]:
        # Verifier marks verified=true but the doc is expired; our service
        # rejects belt-and-suspenders.
        return _self_response_template(
            user_identifier=user_identifier,
            nullifier=nullifier,
            attributes={
                "isAdult": True,
                "issuingState": "IT",
                "documentValid": True,
                "documentExpiry": "2020-01-01",
            },
        )

    def non_italian_proof(
        *,
        user_identifier: str,
        nullifier: str = "self_nullifier_fr_user",
    ) -> dict[str, Any]:
        return _self_response_template(
            user_identifier=user_identifier,
            nullifier=nullifier,
            attributes={
                "isAdult": True,
                "issuingState": "FR",
                "documentValid": True,
                "documentExpiry": "2030-04-15",
            },
        )

    def minor_proof(
        *,
        user_identifier: str,
        nullifier: str = "self_nullifier_minor",
    ) -> dict[str, Any]:
        return _self_response_template(
            user_identifier=user_identifier,
            nullifier=nullifier,
            attributes={
                "isAdult": False,
                "issuingState": "IT",
                "documentValid": True,
                "documentExpiry": "2030-04-15",
            },
        )

    def invalid_proof(
        *,
        user_identifier: str,
        error_code: str = "PROOF_INVALID",
    ) -> dict[str, Any]:
        return _self_response_template(
            user_identifier=user_identifier,
            nullifier="",
            attributes={},
            verified=False,
            error_code=error_code,
        )

    def nullifier_reuse_proof(
        *,
        user_identifier: str,
    ) -> dict[str, Any]:
        return _self_response_template(
            user_identifier=user_identifier,
            nullifier="",
            attributes={},
            verified=False,
            error_code="NULLIFIER_REUSE",
        )

    return SimpleNamespace(
        set_response=set_response,
        set_error=set_error,
        reset=reset,
        calls=state["calls"],
        # presets
        valid_italian_adult_proof=valid_italian_adult_proof,
        expired_document_proof=expired_document_proof,
        non_italian_proof=non_italian_proof,
        minor_proof=minor_proof,
        invalid_proof=invalid_proof,
        nullifier_reuse_proof=nullifier_reuse_proof,
        # Convenience: tests can pull the timeout exception class without
        # importing httpx themselves.
        TimeoutException=httpx.TimeoutException,
    )
