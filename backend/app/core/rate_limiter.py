"""Tick rate limiter for the agent scheduler (brief task 6.3.c).

Two layered caps:

  1. **Concurrent**: at most `max_concurrent` ticks running simultaneously.
     Implemented with `asyncio.Semaphore`. Bounds peak load on the LLM
     API and the sync verifier connection pool.

  2. **Per-minute**: at most `max_per_minute` ticks *dispatched* in any
     rolling 60-second window. Implemented with a deque of dispatch
     timestamps. Bounds sustained throughput / sustained $/min.

The two work together: concurrent caps spikes (5 ticks at once is OK,
50 isn't), per-minute caps the long tail (30 ticks/min is OK, 200/min
isn't even if each is short).

Usage:

    rl = TickRateLimiter(max_concurrent=5, max_per_minute=30)

    async def dispatch(agent_id):
        if not await rl.acquire():
            return  # rate-limited; skip this tick
        try:
            await orchestrator.run_tick(agent_id)
        finally:
            rl.release()

`acquire()` only acquires the semaphore if the per-minute cap allows;
otherwise it returns False without holding any resource. Callers MUST
balance every `True` return with exactly one `release()`.
"""
from __future__ import annotations

import asyncio
from collections import deque
from datetime import datetime, timedelta


class TickRateLimiter:
    def __init__(
        self,
        *,
        max_concurrent: int = 5,
        max_per_minute: int = 30,
    ) -> None:
        if max_concurrent < 1 or max_per_minute < 1:
            raise ValueError("max_concurrent and max_per_minute must be ≥ 1")
        self.max_concurrent = max_concurrent
        self.max_per_minute = max_per_minute
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._minute_window: deque[datetime] = deque()
        self._lock = asyncio.Lock()

    async def acquire(self) -> bool:
        """Return True if the caller may proceed, False if rate-limited.

        Order matters: per-minute check first (cheap, no resource held),
        semaphore acquisition second (may block until concurrency frees).
        If the per-minute cap is full we never touch the semaphore.
        """
        async with self._lock:
            now = datetime.utcnow()
            cutoff = now - timedelta(minutes=1)
            while self._minute_window and self._minute_window[0] < cutoff:
                self._minute_window.popleft()

            if len(self._minute_window) >= self.max_per_minute:
                return False

            self._minute_window.append(now)

        await self._semaphore.acquire()
        return True

    def release(self) -> None:
        """Release one concurrent slot. Idempotent-unsafe — call exactly
        once per `True` from `acquire()`."""
        self._semaphore.release()

    @property
    def in_flight(self) -> int:
        """Current number of acquired concurrent slots. For observability."""
        return self.max_concurrent - self._semaphore._value  # type: ignore[attr-defined]

    @property
    def minute_window_count(self) -> int:
        """Number of dispatches in the current rolling minute. Snapshot,
        not synchronised — readers tolerate the tiny race window."""
        return len(self._minute_window)
