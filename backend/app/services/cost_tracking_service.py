"""Per-user daily LLM cost tracking (FASE 7.3.2).

Three responsibilities, all backed by the `daily_cost_tracking` table
keyed `(date, user_id)`:

  - `upsert_daily_cost(user_id, cost_usd)` — additive write, single
    `INSERT ... ON CONFLICT (date, user_id) DO UPDATE` statement.
    Atomic at the row level even under concurrent ticks for the same
    user. Best-effort: a failed write logs a warning and swallows
    (the audit row already captured the cost in its params; drifting
    the daily aggregate by a few cents is preferable to losing the
    tick outcome).

  - `get_user_cost_today(user_id)` — soft-cap path. Single-row read
    on the composite-PK index, microsecond latency.

  - `get_today_cost_usd()` — hard-cap path. SUM cross-user for today.
    Preserves the kill-switch semantics that pre-existed [7.3.2]:
    when this returns `>= settings.max_daily_llm_cost_usd`, the
    scheduler stops dispatching for the rest of the UTC day.

UTC anchoring: every "today" lookup goes through `utc_today()` from
`core.datetime_helpers` — never `date.today()`, which silently rolls
at local midnight on a non-UTC host (see [7.2.5] postmortem).
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.datetime_helpers import utc_today
from app.core.logging import log
from app.models.schema import DailyCostTracking


async def upsert_daily_cost(
    db: AsyncSession, *, user_id: str, cost_usd: float
) -> None:
    """Add `cost_usd` + 1 tick to today's `(date, user_id)` row.

    The orchestrator already commits the audit row; this best-effort
    write feeds the cap accumulator. Swallows failures (logged) so a
    transient DB hiccup never blocks the tick outcome.

    Side effect: refreshes the `vifaras_cost_user_daily_usd{user_id}`
    Prometheus gauge with the post-upsert today total. The gauge is
    process-local and observability-only — source of truth stays the
    DB row. A failed gauge update is logged + swallowed so it never
    propagates over the orchestrator.
    """
    today = utc_today()
    upserted = False
    try:
        stmt = pg_insert(DailyCostTracking).values(
            date=today,
            user_id=user_id,
            total_cost_usd=cost_usd,
            tick_count=1,
            updated_at=datetime.now(timezone.utc).replace(tzinfo=None),
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["date", "user_id"],
            set_={
                "total_cost_usd": (
                    DailyCostTracking.total_cost_usd + cost_usd
                ),
                "tick_count": DailyCostTracking.tick_count + 1,
                "updated_at": stmt.excluded.updated_at,
            },
        )
        await db.execute(stmt)
        upserted = True
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "cost_tracking.upsert_failed",
            user_id=user_id,
            error=type(exc).__name__,
            message=str(exc),
        )

    if upserted:
        # Separate try block: we want the metric refresh to fail soft
        # WITHOUT re-raising (the upsert already committed-via-flush, the
        # cap accumulator is correct in DB). The gauge is a snapshot for
        # the Prometheus scraper, not a correctness signal.
        try:
            from app.core.metrics import COST_USER_DAILY_USD

            new_total = await get_user_cost_today(db, user_id=user_id)
            COST_USER_DAILY_USD.labels(user_id=user_id).set(new_total)
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "cost_tracking.gauge_refresh_failed",
                user_id=user_id,
                error=type(exc).__name__,
                message=str(exc),
            )


async def get_user_cost_today(db: AsyncSession, *, user_id: str) -> float:
    """USD spent by `user_id` for the current UTC day. 0.0 if no row."""
    stmt = select(DailyCostTracking.total_cost_usd).where(
        DailyCostTracking.date == utc_today(),
        DailyCostTracking.user_id == user_id,
    )
    result = await db.scalar(stmt)
    return float(result or 0.0)


async def get_today_cost_usd(db: AsyncSession) -> float:
    """Global USD spent for the current UTC day (sum across all users).

    Backs the kill-switch hard cap. Pre-[7.3.2] this read a single
    row keyed by date; post-[7.3.2] it sums per-user rows. The
    semantics from the scheduler's perspective are unchanged.
    """
    stmt = select(
        func.coalesce(func.sum(DailyCostTracking.total_cost_usd), 0)
    ).where(DailyCostTracking.date == utc_today())
    result = await db.scalar(stmt)
    return float(result or 0.0)
