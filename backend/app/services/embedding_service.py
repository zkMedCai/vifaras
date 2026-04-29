"""Embedding service — production-ready (brief task 4.2).

4.1 stub stayed minimal (1 function, dict cache, no retry). 4.2 hardens
the production path:

  - **Class-based** `EmbeddingService` with a backend `Enum` so the
    indirection that used to read env vars per call now lives behind a
    typed boundary. Singleton via `get_embedding_service()`; tests reset
    it via `_reset_singleton_for_tests()` before flipping env vars.
  - **`EmbeddingCache`** wraps `cachetools.TTLCache`. LRU eviction at
    `max_size`, TTL bound for memory hygiene. Tracks hits/misses for the
    `/api/_dev/embedding-stats` endpoint.
  - **Tenacity retry**: 3 attempts with exponential backoff on transient
    OpenAI failures (timeout, connection, rate-limit, 5xx). Hard-fail on
    4xx — re-trying a malformed input never helps.
  - **`generate_batch(texts)`** — OpenAI embeddings endpoint accepts an
    array (up to 2048 strings/call). V0 has no caller for it yet (intent
    creation is sync per-row); 4.2 wires the API + tests so V1 bulk
    import / re-indexing flows arrive zero-day.
  - **Cost estimation**: `estimate_cost(text_length)` ≈ 4 chars/token
    × $0.02 per 1M tokens for `text-embedding-3-small`. The estimate is
    intentionally rough — the goal is "is this costing me anything
    surprising" telemetry, not precise accounting.

Module-level shims (`generate_embedding`, `generate_embeddings_batch`,
`build_embedding_text`, `_clear_cache_for_tests`, `_fake_embedding`,
`EmbeddingServiceUnavailable`, `EMBEDDING_DIM`) are preserved so 4.1
callers (`intent_service`) keep working without churn.

Failure mode contract: `EmbeddingServiceUnavailable` is raised on
exhausted retries OR an immediate non-retryable error. Caller maps to
HTTP 503. We never persist a partial / corrupted embedding.
"""
from __future__ import annotations

import hashlib
import logging
import math
import os
import struct
from enum import Enum
from typing import Final

from cachetools import TTLCache
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from app.core.config import settings
from app.core.logging import log


logger = logging.getLogger(__name__)


EMBEDDING_DIM: Final[int] = 1536

# Approximate cost of text-embedding-3-small: $0.02 per 1M tokens.
# 1 token ≈ 4 chars (English/Italian average).
_OPENAI_3_SMALL_USD_PER_TOKEN: Final[float] = 0.02 / 1_000_000
_CHARS_PER_TOKEN: Final[float] = 4.0


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class EmbeddingServiceUnavailable(Exception):
    """OpenAI embeddings endpoint unreachable / 5xx / timeout / retries spent.

    Caller maps to HTTP 503 with `Retry-After: 30`. Distinct from config
    errors (missing API key) which surface as `RuntimeError`.
    """


# ---------------------------------------------------------------------------
# Backend enum
# ---------------------------------------------------------------------------


class EmbeddingBackend(str, Enum):
    OPENAI = "openai"
    FAKE = "fake"


# ---------------------------------------------------------------------------
# Pure helpers (used by the class AND module-level shims)
# ---------------------------------------------------------------------------


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _compute_fake_embedding(text: str) -> list[float]:
    """Deterministic 1536-dim vector seeded by SHA-256 of `text`.

    Same input → byte-identical output. Different inputs diverge cleanly
    (each round of the digest takes a different suffix). Output is
    L2-normalized so cosine similarity behaves sensibly across multiple
    fake embeddings — required by the 4.3 match service tests.
    """
    raw = b""
    counter = 0
    needed = EMBEDDING_DIM * 4
    while len(raw) < needed:
        raw += hashlib.sha256(f"{text}|{counter}".encode("utf-8")).digest()
        counter += 1
    raw = raw[:needed]
    ints = struct.unpack(f"<{EMBEDDING_DIM}I", raw)
    vec = [(i / 0xFFFFFFFF) * 2.0 - 1.0 for i in ints]
    norm = math.sqrt(sum(x * x for x in vec))
    if norm == 0.0:  # pragma: no cover — astronomically unlikely
        return vec
    return [x / norm for x in vec]


def build_embedding_text(*, title: str, description: str | None) -> str:
    """Concatenate title + description into the string we embed.

    Centralized so test assertions and production stay in lockstep.
    Brief 4.1 explicitly considered prefixing the category here; left
    out per `project_embedding_strategy_v0` because category is already
    a hard pre-similarity filter in 4.3.
    """
    if description:
        return f"{title}\n{description}"
    return title


def estimate_cost(text_length: int) -> float:
    """USD estimate for embedding `text_length` characters.

    Rough — the goal is order-of-magnitude awareness, not invoicing.
    """
    estimated_tokens = text_length / _CHARS_PER_TOKEN
    return estimated_tokens * _OPENAI_3_SMALL_USD_PER_TOKEN


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------


class EmbeddingCache:
    """LRU + TTL cache wrapper with hit/miss telemetry.

    Backed by `cachetools.TTLCache` (LRU eviction at `max_size`, TTL
    expiry on read). The inner cache is dict-like (`in`, `len`) which
    keeps test ergonomics simple. Hit/miss counters survive `clear()`
    only if explicitly reset — `clear()` resets them too, since a
    cleared cache is a fresh telemetry window.
    """

    def __init__(self, *, max_size: int = 1000, ttl_seconds: int = 86_400):
        self._inner: TTLCache = TTLCache(maxsize=max_size, ttl=ttl_seconds)
        self._hits = 0
        self._misses = 0

    def get(self, key: str) -> list[float] | None:
        try:
            value = self._inner[key]
        except KeyError:
            self._misses += 1
            return None
        self._hits += 1
        return value

    def set(self, key: str, value: list[float]) -> None:
        self._inner[key] = value

    def clear(self) -> None:
        self._inner.clear()
        self._hits = 0
        self._misses = 0

    def __contains__(self, key: str) -> bool:
        return key in self._inner

    def __len__(self) -> int:
        return len(self._inner)

    def stats(self) -> dict:
        total = self._hits + self._misses
        return {
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate": (self._hits / total) if total > 0 else 0.0,
            "size": len(self._inner),
            "max_size": self._inner.maxsize,
            "ttl_seconds": self._inner.ttl,
        }


# ---------------------------------------------------------------------------
# Service class
# ---------------------------------------------------------------------------


class EmbeddingService:
    """Owns the cache, the (optional) OpenAI client, and the retry policy.

    Construction is cheap (no network); the OpenAI client is lazy-created
    on first OpenAI call so test runs with `EmbeddingBackend.FAKE` never
    pay for it.

    Stats accumulate over the singleton lifetime — tests that need a clean
    slate should `_reset_singleton_for_tests()` between runs.
    """

    def __init__(
        self,
        *,
        backend: EmbeddingBackend,
        cache: EmbeddingCache,
        max_retries: int = 3,
        retry_min_wait: float = 2.0,
        retry_max_wait: float = 10.0,
        openai_model: str = "text-embedding-3-small",
    ) -> None:
        self.backend = backend
        self.cache = cache
        self.max_retries = max_retries
        self.retry_min_wait = retry_min_wait
        self.retry_max_wait = retry_max_wait
        self.openai_model = openai_model
        self._openai_client = None  # lazily constructed
        # OpenAI-call telemetry; cache stats live on `cache.stats()`.
        self._openai_calls = 0
        self._openai_errors: dict[str, int] = {}
        self._cost_estimate_usd = 0.0

    # --- public API ---------------------------------------------------------

    async def generate(self, text: str) -> list[float]:
        """Return a 1536-dim embedding for `text`. Cache-first."""
        key = _hash_text(text)
        cached = self.cache.get(key)
        if cached is not None:
            return cached

        if self.backend == EmbeddingBackend.FAKE:
            vec = _compute_fake_embedding(text)
        else:
            vec = await self._openai_single(text)

        self.cache.set(key, vec)
        return vec

    async def generate_batch(self, texts: list[str]) -> list[list[float]]:
        """Return embeddings in input order. Cache-first per-text.

        Empty input returns empty list. For partial cache hits, only the
        missing texts are sent to OpenAI in a single batch call. The
        OpenAI embeddings endpoint preserves input order in `response.data`.
        """
        if not texts:
            return []

        results: list[list[float] | None] = [None] * len(texts)
        missing_indices: list[int] = []
        missing_texts: list[str] = []

        for i, text in enumerate(texts):
            key = _hash_text(text)
            cached = self.cache.get(key)
            if cached is not None:
                results[i] = cached
            else:
                missing_indices.append(i)
                missing_texts.append(text)

        if missing_texts:
            if self.backend == EmbeddingBackend.FAKE:
                new_vecs = [_compute_fake_embedding(t) for t in missing_texts]
            else:
                new_vecs = await self._openai_batch(missing_texts)

            for idx, text, vec in zip(
                missing_indices, missing_texts, new_vecs, strict=True
            ):
                self.cache.set(_hash_text(text), vec)
                results[idx] = vec

        # All slots populated by construction. The cast keeps mypy happy
        # without a runtime check we'd never expect to fail.
        return [r for r in results if r is not None]

    def stats(self) -> dict:
        return {
            "backend": self.backend.value,
            "cache": self.cache.stats(),
            "openai_calls": self._openai_calls,
            "openai_errors": dict(self._openai_errors),
            "cost_estimate_usd": round(self._cost_estimate_usd, 6),
        }

    # --- OpenAI plumbing ----------------------------------------------------

    def _get_openai_client(self):
        """Lazy-construct the AsyncOpenAI client. RuntimeError if unconfigured."""
        if self._openai_client is None:
            if not settings.openai_api_key:
                raise RuntimeError(
                    "OPENAI_API_KEY is empty; set it or use "
                    "EMBEDDING_BACKEND=fake for tests."
                )
            from openai import AsyncOpenAI

            self._openai_client = AsyncOpenAI(api_key=settings.openai_api_key)
        return self._openai_client

    def _retryable_openai_exceptions(self) -> tuple[type[Exception], ...]:
        """The exception types tenacity should retry on.

        Imported lazily so `EMBEDDING_BACKEND=fake` runs never need openai
        loaded. We retry on transient transport / server-side issues
        (timeout, connection, rate limit, 5xx) and explicitly NOT on 4xx
        (auth, bad request, unprocessable) because re-trying never helps.
        """
        import openai

        return (
            openai.APITimeoutError,
            openai.APIConnectionError,
            openai.RateLimitError,
            openai.InternalServerError,
        )

    async def _openai_single(self, text: str) -> list[float]:
        retryable = self._retryable_openai_exceptions()
        client = self._get_openai_client()

        try:
            async for attempt in AsyncRetrying(
                retry=retry_if_exception_type(retryable),
                stop=stop_after_attempt(self.max_retries),
                wait=wait_exponential(
                    multiplier=1,
                    min=self.retry_min_wait,
                    max=self.retry_max_wait,
                ),
                reraise=True,
            ):
                with attempt:
                    response = await client.embeddings.create(
                        model=self.openai_model, input=text
                    )
                    self._openai_calls += 1
                    self._cost_estimate_usd += estimate_cost(len(text))
                    vec = list(response.data[0].embedding)
                    if len(vec) != EMBEDDING_DIM:  # pragma: no cover
                        raise RuntimeError(
                            f"embedding dim mismatch: expected {EMBEDDING_DIM}"
                            f", got {len(vec)}"
                        )
                    log.info(
                        "embedding.generated",
                        text_hash=_hash_text(text)[:16],
                        text_length=len(text),
                        cache_hit=False,
                        backend=self.backend.value,
                    )
                    return vec
        except Exception as exc:
            err = type(exc).__name__
            self._openai_errors[err] = self._openai_errors.get(err, 0) + 1
            log.warning(
                "embedding.openai_call_failed",
                error=err,
                message=str(exc),
            )
            raise EmbeddingServiceUnavailable(
                f"OpenAI embeddings unavailable after retries: {err}"
            ) from exc

        # Unreachable: AsyncRetrying always either yields a result or raises.
        raise EmbeddingServiceUnavailable("retry loop exited unexpectedly")  # pragma: no cover

    async def _openai_batch(self, texts: list[str]) -> list[list[float]]:
        retryable = self._retryable_openai_exceptions()
        client = self._get_openai_client()

        try:
            async for attempt in AsyncRetrying(
                retry=retry_if_exception_type(retryable),
                stop=stop_after_attempt(self.max_retries),
                wait=wait_exponential(
                    multiplier=1,
                    min=self.retry_min_wait,
                    max=self.retry_max_wait,
                ),
                reraise=True,
            ):
                with attempt:
                    response = await client.embeddings.create(
                        model=self.openai_model, input=texts
                    )
                    self._openai_calls += 1
                    total_chars = sum(len(t) for t in texts)
                    self._cost_estimate_usd += estimate_cost(total_chars)
                    vecs = [list(item.embedding) for item in response.data]
                    if any(len(v) != EMBEDDING_DIM for v in vecs):  # pragma: no cover
                        raise RuntimeError("embedding dim mismatch in batch")
                    return vecs
        except Exception as exc:
            err = type(exc).__name__
            self._openai_errors[err] = self._openai_errors.get(err, 0) + 1
            log.warning(
                "embedding.openai_batch_failed",
                error=err,
                count=len(texts),
            )
            raise EmbeddingServiceUnavailable(
                f"OpenAI embeddings batch unavailable after retries: {err}"
            ) from exc

        raise EmbeddingServiceUnavailable("retry loop exited unexpectedly")  # pragma: no cover


# ---------------------------------------------------------------------------
# Singleton + module-level shims
# ---------------------------------------------------------------------------


_singleton: EmbeddingService | None = None


def get_embedding_service() -> EmbeddingService:
    """Lazy singleton. Backend is resolved from env at first call.

    Tests that need to flip the backend should call
    `_reset_singleton_for_tests()` before the next `get_embedding_service()`.
    """
    global _singleton
    if _singleton is None:
        backend_str = os.environ.get(
            "EMBEDDING_BACKEND", settings.embedding_backend
        ).lower()
        backend = EmbeddingBackend(backend_str)
        cache = EmbeddingCache(
            max_size=settings.embedding_cache_size,
            ttl_seconds=settings.embedding_cache_ttl_seconds,
        )
        _singleton = EmbeddingService(
            backend=backend,
            cache=cache,
            max_retries=settings.embedding_max_retries,
            retry_min_wait=settings.embedding_retry_min_wait_seconds,
            retry_max_wait=settings.embedding_retry_max_wait_seconds,
            openai_model=settings.openai_embedding_model,
        )
    return _singleton


def _reset_singleton_for_tests() -> None:
    """Drop the singleton so the next call picks up new env / config."""
    global _singleton
    _singleton = None


async def generate_embedding(text: str) -> list[float]:
    """Module-level shim: delegates to the singleton's `generate`."""
    return await get_embedding_service().generate(text)


async def generate_embeddings_batch(texts: list[str]) -> list[list[float]]:
    """Module-level shim: delegates to the singleton's `generate_batch`."""
    return await get_embedding_service().generate_batch(texts)


def _clear_cache_for_tests() -> None:
    """Test seam: clear the singleton's cache (creates one if needed)."""
    get_embedding_service().cache.clear()


def _fake_embedding(text: str) -> list[float]:
    """Test seam: direct access to the deterministic fake embedding.

    Used by test factories that seed Intent rows directly without going
    through the cache (e.g. bulk seeding for tier-limit tests).
    """
    return _compute_fake_embedding(text)
