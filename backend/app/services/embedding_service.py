"""Embedding service — sync-inline OpenAI generation (brief task 4.1, extended in 4.2).

Two backends, switched by the `EMBEDDING_BACKEND` env var:

  - `EMBEDDING_BACKEND=openai` (default): real `text-embedding-3-small`
    call via `AsyncOpenAI`. Inline in the request flow — no worker
    queue, no deferred job. Decision per `project_embedding_strategy_v0`
    memory + brief: V0 traffic is small enough that ~150ms blocking on
    OpenAI per intent is fine; bulk import is V1+.
  - `EMBEDDING_BACKEND=fake`: deterministic SHA-256-seeded 1536-dim
    vector. Tests set this so the suite is hermetic — same `(title,
    description)` pair always produces the same vector, no network. The
    vector is L2-normalized so cosine similarity behaves sensibly across
    multiple deterministic embeddings.

LRU cache (in-memory, 1000 entries) deduplicates exact text repeats. V1+
will likely move this to Redis when we go multi-process, but at V0
single-process scale the in-memory cache is enough.

Failure mode: `EmbeddingServiceUnavailable` on OpenAI timeout / 5xx.
Callers MUST treat this as terminal for the create_intent flow — an
intent without an embedding is invisible to the matcher, so we'd rather
fail loudly than persist a ghost row.

This module is the deliverable for 4.1; 4.2 will extend with formal
retry/backoff, batch processing, and a configurable cache size.
"""
from __future__ import annotations

import hashlib
import math
import os
import struct
from collections import OrderedDict
from typing import Final

from app.core.config import settings
from app.core.logging import log


EMBEDDING_DIM: Final[int] = 1536
_CACHE_MAX_ENTRIES: Final[int] = 1000


class EmbeddingServiceUnavailable(Exception):
    """OpenAI embeddings endpoint unreachable / 5xx / timeout.

    Caller maps to HTTP 503 with `Retry-After: 30`. Distinct from
    config errors (missing API key) which surface as `RuntimeError`.
    """


# In-memory LRU. Key = exact text, value = list[float] (1536 dim).
_cache: OrderedDict[str, list[float]] = OrderedDict()


def _backend() -> str:
    """Resolve the active backend at call time, not import time.

    Tests flip env vars per-test; cache the choice would defeat that.
    Settings are not used here because `EMBEDDING_BACKEND` is a test seam,
    not a deployment knob.
    """
    return os.environ.get("EMBEDDING_BACKEND", "openai").lower()


def _cache_get(text: str) -> list[float] | None:
    vec = _cache.get(text)
    if vec is not None:
        # touch for LRU recency
        _cache.move_to_end(text)
    return vec


def _cache_put(text: str, vec: list[float]) -> None:
    _cache[text] = vec
    _cache.move_to_end(text)
    while len(_cache) > _CACHE_MAX_ENTRIES:
        _cache.popitem(last=False)


def _clear_cache_for_tests() -> None:
    """Test seam — call from a fixture to reset between tests."""
    _cache.clear()


def build_embedding_text(*, title: str, description: str | None) -> str:
    """Concatenate title + description into the string we embed.

    Centralized so test assertions and production stay in lockstep. If
    we later want to prepend the category (per brief's "decisioni fuori
    brief" #4), that change happens here in one place.
    """
    if description:
        return f"{title}\n{description}"
    return title


def _fake_embedding(text: str) -> list[float]:
    """Deterministic 1536-dim vector seeded by SHA-256 of `text`.

    Repeats the 32-byte digest until we have 1536 * 4 bytes, unpacks as
    little-endian unsigned 32-bit ints, maps to [-1, 1], and L2-normalizes.
    Same input → byte-identical output, every time. Different inputs
    diverge cleanly because each round of the digest takes a different
    suffix.
    """
    raw = b""
    counter = 0
    needed = EMBEDDING_DIM * 4
    while len(raw) < needed:
        raw += hashlib.sha256(f"{text}|{counter}".encode("utf-8")).digest()
        counter += 1
    raw = raw[:needed]
    # 32-bit unsigned ints → centered & scaled to [-1, 1].
    ints = struct.unpack(f"<{EMBEDDING_DIM}I", raw)
    vec = [(i / 0xFFFFFFFF) * 2.0 - 1.0 for i in ints]
    norm = math.sqrt(sum(x * x for x in vec))
    if norm == 0.0:  # pragma: no cover — astronomically unlikely for sha256 output
        return vec
    return [x / norm for x in vec]


async def _openai_embedding(text: str) -> list[float]:
    """Call OpenAI text-embedding-3-small via AsyncOpenAI. Inline.

    Wraps any transport / 5xx error as `EmbeddingServiceUnavailable` so
    callers don't need to know openai-specific exception types.
    """
    if not settings.openai_api_key:
        raise RuntimeError(
            "OPENAI_API_KEY is empty; set it or use EMBEDDING_BACKEND=fake "
            "for tests."
        )
    # Imported lazily so test runs with EMBEDDING_BACKEND=fake never need
    # to instantiate the openai client.
    from openai import AsyncOpenAI

    client = AsyncOpenAI(api_key=settings.openai_api_key)
    try:
        response = await client.embeddings.create(
            model=settings.openai_embedding_model,
            input=text,
        )
    except Exception as exc:
        log.warning(
            "embedding.openai_call_failed",
            error=type(exc).__name__,
            message=str(exc),
        )
        raise EmbeddingServiceUnavailable(
            f"OpenAI embeddings unavailable: {type(exc).__name__}"
        ) from exc

    vec = list(response.data[0].embedding)
    if len(vec) != EMBEDDING_DIM:  # pragma: no cover — guarded by model choice
        raise RuntimeError(
            f"embedding dim mismatch: expected {EMBEDDING_DIM}, got {len(vec)}"
        )
    return vec


async def generate_embedding(text: str) -> list[float]:
    """Return a 1536-dim embedding for `text`. Cache-first.

    Resolves the backend at call time (env var `EMBEDDING_BACKEND`).
    Test runs flip this fixture-side; production leaves it unset.
    """
    cached = _cache_get(text)
    if cached is not None:
        return cached

    backend = _backend()
    if backend == "fake":
        vec = _fake_embedding(text)
    else:
        vec = await _openai_embedding(text)

    _cache_put(text, vec)
    return vec
