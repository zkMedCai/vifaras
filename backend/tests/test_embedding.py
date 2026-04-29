"""Embedding service tests (brief task 4.2).

15 tests organized by concern:

  Backend behavior (4):
   1. fake backend deterministic
   2. fake backend produces uncorrelated vectors for distinct texts
   3. fake backend output is unit-norm
   4. openai backend (mocked) returns 1536-dim vector

  Cache behavior (4):
   5. cache hit avoids backend call
   6. cache miss calls backend and stores
   7. LRU eviction at max_size
   8. TTL expiration

  Retry & error (3):
   9. retry on InternalServerError eventually succeeds
  10. no retry on BadRequestError (4xx)
  11. retries exhausted → EmbeddingServiceUnavailable

  Batch (3):
  12. batch processes all inputs in order
  13. batch uses cache for partial hits, sends only missing to backend
  14. batch with empty input returns empty list

  Cost (1):
  15. estimate_cost proportional to text length

These tests construct EmbeddingService instances directly (no singleton)
so each test owns its cache + backend mock — singleton state never leaks.
The retry tests pin `retry_min_wait=0` so the suite stays fast.
"""
from __future__ import annotations

import asyncio
import math
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from app.services import embedding_service
from app.services.embedding_service import (
    EmbeddingBackend,
    EmbeddingCache,
    EmbeddingService,
    EmbeddingServiceUnavailable,
    _compute_fake_embedding,
    estimate_cost,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_service(
    *,
    backend: EmbeddingBackend = EmbeddingBackend.FAKE,
    cache: EmbeddingCache | None = None,
    max_retries: int = 3,
) -> EmbeddingService:
    # `cache or X` would substitute a fresh cache for any *empty* cache —
    # `EmbeddingCache.__len__` returning 0 makes an empty cache falsy.
    return EmbeddingService(
        backend=backend,
        cache=cache if cache is not None else EmbeddingCache(
            max_size=100, ttl_seconds=60
        ),
        max_retries=max_retries,
        retry_min_wait=0.0,
        retry_max_wait=0.0,
    )


class _FakeOpenAIClient:
    """Minimal AsyncOpenAI substitute. Pop-driven response queue.

    Each entry can be either:
      - a list of embeddings (list[list[float]]) → wrapped as a response
      - an Exception instance → raised on the call
    """

    def __init__(self, queue: list[Any]) -> None:
        self._queue = list(queue)
        self.call_count = 0
        self.last_input: Any = None
        self.embeddings = SimpleNamespace(create=self._create)

    async def _create(self, *, model: str, input: Any) -> Any:
        self.call_count += 1
        self.last_input = input
        if not self._queue:
            raise RuntimeError(
                "_FakeOpenAIClient ran out of responses; test is asking "
                "for more calls than scripted."
            )
        item = self._queue.pop(0)
        if isinstance(item, BaseException):
            raise item
        # item is list[list[float]] — wrap as openai-shaped response.
        return SimpleNamespace(
            data=[SimpleNamespace(embedding=v) for v in item]
        )


def _fake_request() -> httpx.Request:
    return httpx.Request("POST", "https://api.openai.com/v1/embeddings")


def _fake_500_response() -> httpx.Response:
    return httpx.Response(500, request=_fake_request())


def _fake_400_response() -> httpx.Response:
    return httpx.Response(400, request=_fake_request())


def _make_internal_server_error():
    import openai

    return openai.InternalServerError(
        message="boom", response=_fake_500_response(), body=None
    )


def _make_bad_request_error():
    import openai

    return openai.BadRequestError(
        message="bad input", response=_fake_400_response(), body=None
    )


# ===========================================================================
# 1. fake backend deterministic
# ===========================================================================


async def test_fake_backend_deterministic() -> None:
    service = _make_service()
    a = await service.generate("MacBook Pro 14 M3")
    b = await service.generate("MacBook Pro 14 M3")
    assert a == b
    assert len(a) == embedding_service.EMBEDDING_DIM


# ===========================================================================
# 2. fake backend produces uncorrelated vectors for distinct texts
# ===========================================================================


async def test_fake_backend_different_texts_uncorrelated() -> None:
    service = _make_service()
    a = await service.generate("Bici da corsa Bianchi")
    b = await service.generate("Cuffie Sony WH-1000XM5")
    # Cosine similarity (both unit-norm) ≈ inner product
    cos = sum(x * y for x, y in zip(a, b))
    # SHA-256-seeded vectors of sufficient dim are essentially orthogonal:
    # |cos| should be small. 0.1 is a generous bound.
    assert abs(cos) < 0.1


# ===========================================================================
# 3. fake backend output is unit-norm
# ===========================================================================


async def test_fake_backend_unit_norm() -> None:
    service = _make_service()
    vec = await service.generate("test")
    norm = math.sqrt(sum(v * v for v in vec))
    assert pytest.approx(norm, abs=1e-9) == 1.0


# ===========================================================================
# 4. openai backend (mocked) returns 1536-dim vector
# ===========================================================================


async def test_openai_backend_returns_1536_dim() -> None:
    service = _make_service(backend=EmbeddingBackend.OPENAI)
    fake_vec = [0.1] * embedding_service.EMBEDDING_DIM
    service._openai_client = _FakeOpenAIClient([[fake_vec]])

    vec = await service.generate("hello")
    assert len(vec) == embedding_service.EMBEDDING_DIM
    assert vec[0] == pytest.approx(0.1)
    assert service._openai_client.call_count == 1
    assert service._openai_client.last_input == "hello"


# ===========================================================================
# 5. cache hit avoids backend call
# ===========================================================================


async def test_cache_hit_avoids_backend_call() -> None:
    service = _make_service(backend=EmbeddingBackend.OPENAI)
    fake_vec = [0.2] * embedding_service.EMBEDDING_DIM
    service._openai_client = _FakeOpenAIClient([[fake_vec]])  # only 1 response queued

    a = await service.generate("once")
    b = await service.generate("once")  # would crash if it tried to call again
    assert a == b
    assert service._openai_client.call_count == 1
    assert service.cache.stats()["hits"] == 1


# ===========================================================================
# 6. cache miss calls backend and stores
# ===========================================================================


async def test_cache_miss_calls_backend_and_stores() -> None:
    service = _make_service(backend=EmbeddingBackend.OPENAI)
    fake_vec = [0.3] * embedding_service.EMBEDDING_DIM
    service._openai_client = _FakeOpenAIClient([[fake_vec]])

    assert len(service.cache) == 0
    await service.generate("first time")
    assert len(service.cache) == 1
    assert service.cache.stats()["misses"] == 1


# ===========================================================================
# 7. LRU eviction at max_size
# ===========================================================================


async def test_cache_lru_eviction_on_max_size() -> None:
    cache = EmbeddingCache(max_size=3, ttl_seconds=60)
    service = _make_service(cache=cache)

    await service.generate("a")
    await service.generate("b")
    await service.generate("c")
    assert len(cache) == 3

    # Add a fourth → oldest ("a") evicted.
    await service.generate("d")
    assert len(cache) == 3
    assert embedding_service._hash_text("a") not in cache
    assert embedding_service._hash_text("d") in cache


# ===========================================================================
# 8. TTL expiration
# ===========================================================================


async def test_cache_ttl_expiration() -> None:
    cache = EmbeddingCache(max_size=10, ttl_seconds=1)
    # cachetools.TTLCache uses a real time source; set a tiny TTL and
    # sleep slightly past it to observe expiry. Window of error is large
    # enough that this isn't flaky on slow CI.
    cache.set("k", [0.0] * embedding_service.EMBEDDING_DIM)
    assert cache.get("k") is not None
    # Force the inner cache's clock past TTL by manipulating its timer:
    # easier than asyncio.sleep() which slows the suite. cachetools allows
    # passing a custom timer, but the default uses time.monotonic; we
    # simulate expiry by clearing the inner state directly is wrong (would
    # bypass the test's intent), so we sleep just past the boundary.
    await asyncio.sleep(1.05)
    assert cache.get("k") is None


# ===========================================================================
# 9. retry on InternalServerError eventually succeeds
# ===========================================================================


async def test_retry_on_5xx_succeeds_eventually() -> None:
    service = _make_service(backend=EmbeddingBackend.OPENAI, max_retries=3)
    fake_vec = [0.4] * embedding_service.EMBEDDING_DIM
    service._openai_client = _FakeOpenAIClient(
        [
            _make_internal_server_error(),
            _make_internal_server_error(),
            [fake_vec],  # third attempt succeeds
        ]
    )

    vec = await service.generate("flaky text")
    assert vec[0] == pytest.approx(0.4)
    assert service._openai_client.call_count == 3


# ===========================================================================
# 10. no retry on BadRequestError (4xx)
# ===========================================================================


async def test_no_retry_on_4xx_fails_immediately() -> None:
    service = _make_service(backend=EmbeddingBackend.OPENAI, max_retries=3)
    service._openai_client = _FakeOpenAIClient([_make_bad_request_error()])

    with pytest.raises(EmbeddingServiceUnavailable):
        await service.generate("bad input")
    # Exactly one attempt — no retry on 4xx.
    assert service._openai_client.call_count == 1
    # And the BadRequestError was counted once.
    assert "BadRequestError" in service.stats()["openai_errors"]


# ===========================================================================
# 11. retries exhausted → EmbeddingServiceUnavailable
# ===========================================================================


async def test_max_retries_exceeded_raises() -> None:
    service = _make_service(backend=EmbeddingBackend.OPENAI, max_retries=3)
    service._openai_client = _FakeOpenAIClient(
        [_make_internal_server_error() for _ in range(5)]
    )

    with pytest.raises(EmbeddingServiceUnavailable):
        await service.generate("always fails")
    # max_retries=3 → exactly 3 attempts.
    assert service._openai_client.call_count == 3


# ===========================================================================
# 12. batch processes all inputs in order
# ===========================================================================


async def test_batch_processes_all_inputs() -> None:
    service = _make_service()  # FAKE backend
    texts = ["alpha", "beta", "gamma", "delta", "epsilon"]
    vecs = await service.generate_batch(texts)
    assert len(vecs) == 5
    # Order preserved + deterministic vs single-call.
    for text, vec in zip(texts, vecs, strict=True):
        assert vec == _compute_fake_embedding(text)


# ===========================================================================
# 13. batch uses cache for partial hits
# ===========================================================================


async def test_batch_uses_cache_for_partial_hits() -> None:
    service = _make_service(backend=EmbeddingBackend.OPENAI)
    # Pre-populate cache for "alpha" and "gamma".
    pre_vec_alpha = _compute_fake_embedding("alpha")
    pre_vec_gamma = _compute_fake_embedding("gamma")
    service.cache.set(embedding_service._hash_text("alpha"), pre_vec_alpha)
    service.cache.set(embedding_service._hash_text("gamma"), pre_vec_gamma)

    # Mock returns 2 embeddings (for the 2 missing texts).
    new_beta = [0.5] * embedding_service.EMBEDDING_DIM
    new_delta = [0.6] * embedding_service.EMBEDDING_DIM
    service._openai_client = _FakeOpenAIClient([[new_beta, new_delta]])

    vecs = await service.generate_batch(["alpha", "beta", "gamma", "delta"])
    assert len(vecs) == 4
    assert vecs[0] == pre_vec_alpha
    assert vecs[1] == new_beta
    assert vecs[2] == pre_vec_gamma
    assert vecs[3] == new_delta
    # Only one OpenAI call, with only the missing texts as input (in order).
    assert service._openai_client.call_count == 1
    assert service._openai_client.last_input == ["beta", "delta"]


# ===========================================================================
# 14. batch with empty input returns empty list
# ===========================================================================


async def test_batch_empty_input_returns_empty() -> None:
    service = _make_service()
    assert await service.generate_batch([]) == []


# ===========================================================================
# 15. estimate_cost proportional to text length
# ===========================================================================


def test_cost_estimate_proportional_to_text_length() -> None:
    short = estimate_cost(100)
    long = estimate_cost(1000)
    # 10× longer text → 10× cost.
    assert long == pytest.approx(short * 10, rel=1e-9)
    assert short > 0


# ===========================================================================
# 16. /api/_dev/embedding-stats route smoke (off by default → 404; on → 200)
# ===========================================================================


async def test_dev_embedding_stats_endpoint_gated(http_client, monkeypatch) -> None:
    from app.core.config import settings

    # Default: flag off → 404.
    monkeypatch.setattr(settings, "enable_dev_endpoints", False)
    r = await http_client.get("/api/_dev/embedding-stats")
    assert r.status_code == 404

    # Flip on → 200 with the singleton's stats payload.
    monkeypatch.setattr(settings, "enable_dev_endpoints", True)
    monkeypatch.setenv("EMBEDDING_BACKEND", "fake")
    embedding_service._reset_singleton_for_tests()
    try:
        r = await http_client.get("/api/_dev/embedding-stats")
        assert r.status_code == 200
        body = r.json()
        assert body["backend"] == "fake"
        assert "cache" in body
        assert "openai_calls" in body
    finally:
        embedding_service._reset_singleton_for_tests()
