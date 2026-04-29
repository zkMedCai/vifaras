"""Match scheduler — periodic re-scan of match-starved intents (brief task 4.3).

V0 strategy: in-process `AsyncIOScheduler` (apscheduler) ticks every
`settings.match_scheduler_interval_minutes` and re-runs the matcher on
intents with fewer than `settings.match_scheduler_min_matches` discovered
matches. New entries to the marketplace can produce candidates for older
intents that didn't have any when first created — the scheduler closes
that loop.

Lifecycle is bound to FastAPI's lifespan (start at app startup, shutdown
at exit). The job is gated by `settings.enable_match_scheduler` so envs
that don't want the tick (CLI tools, batch jobs, the test http_client
which doesn't trigger lifespan) are silent.

V1+ migration: when we go multi-worker, this in-process loop won't
coordinate cleanly across processes. Move to Redis-backed Celery beat
or arq, with a leader-elected lock so only one worker runs the tick.
For V0 single-process, this is simpler.
"""
from __future__ import annotations

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import func, or_, select

from app.core.config import settings
from app.core.logging import log
from app.models.schema import Intent, Match
from app.services import match_service


_scheduler: AsyncIOScheduler | None = None


async def expire_pending_deals() -> dict[str, int]:
    """One tick: expire pending Deals past their `expires_at` (24h V0 default).

    Each expiration rolls back the linked intents `matched → active` and
    the chosen match `agreed → discovered` (see deal_service.expire_deal).
    Returns a small telemetry dict for tests + future stats endpoint.
    """
    from app.core.db import AsyncSessionLocal
    from app.models.schema import Deal
    from app.services import deal_service
    from sqlalchemy import select as _select

    expired = 0
    skipped = 0
    errored = 0

    async with AsyncSessionLocal() as db:
        candidate_ids = list(
            await db.scalars(
                _select(Deal.id)
                .where(Deal.status == "pending_signatures")
                .where(Deal.expires_at < _utcnow_naive())
                .limit(settings.match_scheduler_batch_size)
            )
        )

        for deal_id in candidate_ids:
            try:
                result = await deal_service.expire_deal(db, deal_id=deal_id)
                if result.intents_reverted or result.matches_reverted:
                    expired += 1
                else:
                    skipped += 1
            except Exception as exc:
                errored += 1
                log.warning(
                    "match_scheduler.deal_expire_failed",
                    deal_id=deal_id,
                    error=type(exc).__name__,
                    message=str(exc),
                )

    log.info(
        "match_scheduler.deal_expire_tick_complete",
        expired=expired,
        skipped=skipped,
        errored=errored,
    )
    return {"expired": expired, "skipped": skipped, "errored": errored}


def _utcnow_naive():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).replace(tzinfo=None)


async def refresh_low_match_intents() -> dict[str, int]:
    """One tick: re-match intents with `< min_matches` discovered matches.

    Returns a small telemetry dict — useful for the dev stats endpoint
    when we add it (4.4+) and for unit tests that drive a single tick.
    """
    from app.core.db import AsyncSessionLocal

    processed = 0
    skipped = 0
    errored = 0

    async with AsyncSessionLocal() as db:
        # subquery: count of discovered matches per intent (either side).
        match_count = (
            select(func.count(Match.id))
            .where(
                or_(
                    Match.buy_intent_id == Intent.id,
                    Match.sell_intent_id == Intent.id,
                )
            )
            .where(Match.status == "discovered")
            .correlate(Intent)
            .scalar_subquery()
        )

        candidates_stmt = (
            select(Intent.id)
            .where(Intent.status == "active")
            .where(Intent.side != "trade")
            .where(match_count < settings.match_scheduler_min_matches)
            .limit(settings.match_scheduler_batch_size)
        )
        candidate_ids = list((await db.scalars(candidates_stmt)).all())

        for intent_id in candidate_ids:
            try:
                await match_service.find_matches_for_intent(
                    db, intent_id=intent_id
                )
                processed += 1
            except match_service.MatchError:
                skipped += 1
            except Exception as exc:
                # Don't let a single bad intent abort the whole tick.
                errored += 1
                log.warning(
                    "match_scheduler.intent_failed",
                    intent_id=intent_id,
                    error=type(exc).__name__,
                    message=str(exc),
                )

    log.info(
        "match_scheduler.tick_complete",
        processed=processed,
        skipped=skipped,
        errored=errored,
    )
    return {"processed": processed, "skipped": skipped, "errored": errored}


def start_scheduler() -> AsyncIOScheduler | None:
    """Start the in-process scheduler if enabled. Idempotent.

    Returns the scheduler handle (or None when disabled) so the lifespan
    can keep a reference for graceful shutdown.
    """
    global _scheduler
    if not settings.enable_match_scheduler:
        log.info("match_scheduler.disabled_by_settings")
        return None
    if _scheduler is not None:
        return _scheduler

    sched = AsyncIOScheduler()
    sched.add_job(
        refresh_low_match_intents,
        "interval",
        minutes=settings.match_scheduler_interval_minutes,
        id="match_refresh_low_intents",
        replace_existing=True,
    )
    # 5.3: piggy-back on the same scheduler for deal expiration. Independent
    # tick frequency (10 min by default — deals don't auto-expire faster
    # than that and we don't want to thrash the lock graph).
    sched.add_job(
        expire_pending_deals,
        "interval",
        minutes=10,
        id="deal_expire_pending",
        replace_existing=True,
    )
    sched.start()
    _scheduler = sched
    log.info(
        "match_scheduler.started",
        interval_minutes=settings.match_scheduler_interval_minutes,
        batch_size=settings.match_scheduler_batch_size,
        min_matches=settings.match_scheduler_min_matches,
    )
    return sched


def shutdown_scheduler() -> None:
    """Stop the scheduler if it's running. Safe to call from lifespan exit."""
    global _scheduler
    if _scheduler is None:
        return
    try:
        _scheduler.shutdown(wait=False)
    finally:
        _scheduler = None
        log.info("match_scheduler.shutdown")
