"""Match service — semantic + price discovery (brief task 4.3).

This is where the marketplace stops being a passive intent registry and
starts proposing pairings. Given an intent A, the matcher finds intents
B on the opposite side, in the same category, with price overlap, ranked
by a combined semantic + price score.

Public surface:
  - MatchError (+ subclasses)              — typed errors with code+http_status
  - compute_price_proximity                — pure score [0, 1]
  - combine_scores                         — weighted sum
  - find_matches_for_intent(db, ...)       → list[Match]
  - list_matches_for_intent(db, ...)       → MatchListPage (read-only)
  - get_match_for_user(db, ...)            → Match (owner-only)
  - mark_match_negotiating(db, match_id)   — 5.1 transition hook
  - expire_matches_for_intent(db, ...)     — cancel/expiry cascade

Two layers of filtering before ranking:

  1. **Categorical pre-filter** — same category, opposite side, active,
     not own user, not expired. Cheap SQL filter; cuts the search space
     to a few hundred rows even with N=10K active intents.

  2. **Semantic pre-filter** — top-(limit*3) by cosine distance via the
     HNSW index (`vector_cosine_ops`, m=16, ef_construction=64). Postgres
     does an index scan; we get the closest candidates without scoring
     all of them.

  3. **Application-side scoring** — for each candidate that passes the
     price-overlap filter (buyer cap ≥ seller floor), compute
     `price_proximity` and `combined_score`. Sort. Take top-N.

The oversampling factor (×3) compensates for candidates we drop on the
price filter — empirically enough that the top-N is rarely starved.

FASE 10.2.7 adds `MATCHING_BACKEND=anthropic`: same persistence contract,
but candidate retrieval is SQL-only and Claude scores semantic compatibility.
That path lets V0 run Anthropic-only without OpenAI embeddings.

Persistence: `Match` rows are upserted on `(buy_intent_id, sell_intent_id)`
unique constraint. Net-new rows audit `match_created`; rows whose score
moved past `_AUDIT_SCORE_DELTA` audit `update_match_score`. Sub-threshold
drift (re-discovery with same embedding + same prices) is silent.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

from anthropic import AsyncAnthropic
from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.logging import log
from app.models.schema import Intent, Match
from app.services import (
    anthropic_pricing,
    audit_service,
    cost_tracking_service,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


MATCH_SIMILARITY_WEIGHT: Final[float] = 0.7
MATCH_PRICE_PROXIMITY_WEIGHT: Final[float] = 0.3

DEFAULT_MATCH_LIMIT: Final[int] = 20
MAX_MATCH_LIMIT: Final[int] = 50

# How many semantic-near candidates to pull before applying price-overlap +
# scoring. 3× the requested limit empirically clears the price filter for
# typical V0 spreads.
_OVERSAMPLING_MULTIPLIER: Final[int] = 3

# Score-update audit threshold: only emit `update_match_score` when the
# combined score moved at least this much. Sub-threshold drift is silent
# to avoid audit flood on idempotent re-discovery.
_AUDIT_SCORE_DELTA: Final[float] = 0.05


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class MatchError(Exception):
    code: str = "match_error"
    http_status: int = 400


class IntentNotFoundForMatching(MatchError):
    code = "intent_not_found"
    http_status = 404


class IntentInactiveForMatching(MatchError):
    code = "intent_inactive"
    http_status = 409


class TradeMatchingNotImplemented(MatchError):
    """V0 doesn't match `side='trade'`. Schema-ready, comes online in FASE 8."""

    code = "trade_matching_not_implemented"
    http_status = 422


class MatchingBackendUnknown(MatchError):
    code = "matching_backend_unknown"
    http_status = 500


class MatchingProviderUnavailable(MatchError):
    code = "matching_provider_unavailable"
    http_status = 503


class MatchNotFound(MatchError):
    code = "match_not_found"
    http_status = 404


class NotMatchOwner(MatchError):
    code = "not_match_owner"
    http_status = 403


class InvalidMatchTransition(MatchError):
    code = "invalid_match_transition"
    http_status = 409


# ---------------------------------------------------------------------------
# Pure scoring functions
# ---------------------------------------------------------------------------


def compute_price_proximity(
    *,
    buyer_cap_cents: int,
    buyer_ideal_cents: int,
    seller_floor_cents: int,
    seller_ideal_cents: int,
) -> float:
    """Quanto i prezzi target dei due lati sono vicini, in [0, 1].

    Concept: the deal is feasible iff `buyer_cap >= seller_floor`. The
    "deal zone" spans `[seller_floor, buyer_cap]`. Inside that zone, both
    sides have an *ideal*: where they'd most like the price to land.
    Closer ideals → easier negotiation → higher proximity score.

    Returns 1.0 when both ideals coincide with the zone center, drops
    toward 0 as ideals diverge from the center relative to zone width.
    Clamped to [0, 1].

    Caller must have already verified `buyer_cap >= seller_floor`; passing
    in non-overlapping prices yields a low (often 0) score, not an error.
    """
    deal_zone_width = max(buyer_cap_cents - seller_floor_cents, 1)
    deal_zone_center = (seller_floor_cents + buyer_cap_cents) / 2

    seller_distance = abs(seller_ideal_cents - deal_zone_center)
    buyer_distance = abs(buyer_ideal_cents - deal_zone_center)
    avg_distance = (seller_distance + buyer_distance) / 2

    proximity = 1.0 - (avg_distance / deal_zone_width)
    return max(0.0, min(1.0, proximity))


def combine_scores(*, similarity: float, price_proximity: float) -> float:
    """0.7 * similarity + 0.3 * price_proximity.

    Similarity dominates: a same-category but price-mediocre match beats
    a cross-category price-perfect one. Weights are tunable constants;
    A/B testing different weights is a V1+ exercise (settable via config
    only when we have data to drive it).
    """
    return (
        MATCH_SIMILARITY_WEIGHT * similarity
        + MATCH_PRICE_PROXIMITY_WEIGHT * price_proximity
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utcnow_naive() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _resolve_buy_sell(
    intent: Intent, candidate: Intent
) -> tuple[Intent, Intent]:
    """Return (buy_intent, sell_intent) regardless of which side `intent` is."""
    if intent.side == "buy":
        return intent, candidate
    return candidate, intent


@dataclass
class _ScoredCandidate:
    buy_intent_id: str
    sell_intent_id: str
    buy_user_id: str
    sell_user_id: str
    similarity: float
    price_proximity: float
    combined: float


def _matching_backend() -> str:
    return settings.matching_backend.strip().lower()


def uses_embedding_matching() -> bool:
    """Whether new intents must carry pgvector embeddings for discovery."""
    return _matching_backend() != "anthropic"


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


async def find_matches_for_intent(
    db: AsyncSession,
    *,
    intent_id: str,
    limit: int = DEFAULT_MATCH_LIMIT,
    anthropic_client: AsyncAnthropic | None = None,
) -> list[Match]:
    backend = _matching_backend()
    if backend == "anthropic":
        return await _find_matches_for_intent_anthropic(
            db,
            intent_id=intent_id,
            limit=limit,
            anthropic_client=anthropic_client,
        )
    if backend != "embedding":
        raise MatchingBackendUnknown(
            f"unsupported MATCHING_BACKEND={settings.matching_backend!r}"
        )
    return await _find_matches_for_intent_embedding(
        db,
        intent_id=intent_id,
        limit=limit,
    )


async def _find_matches_for_intent_embedding(
    db: AsyncSession,
    *,
    intent_id: str,
    limit: int = DEFAULT_MATCH_LIMIT,
) -> list[Match]:
    """Discover top-N matches for `intent_id`. Persists net-new ones.

    Idempotent on score-stable re-runs: existing `(buy, sell)` pairs are
    updated in place via the `uq_match` unique constraint. No duplicates.

    Returns the persisted `Match` rows for this intent ordered by
    `combined_score DESC`. Empty list if intent is missing/inactive (not
    an error — useful for "fire-and-forget" calls from `intent_service`).
    """
    intent = await db.get(Intent, intent_id)
    if intent is None or intent.status != "active":
        return []
    if intent.side == "trade":
        raise TradeMatchingNotImplemented(
            "TRADE matching not implemented in V0 (FASE 8)"
        )
    if intent.description_embedding is None:
        # Defensive: should never happen post-4.1, but a missing embedding
        # would make the cosine_distance ORDER BY explode. Skip silently.
        return []

    limit = max(1, min(MAX_MATCH_LIMIT, limit))
    opposite_side = "buy" if intent.side == "sell" else "sell"

    # 1. Vector + categorical pre-filter. Cosine distance is ascending
    #    (smallest = most similar), so ORDER BY ASC LIMIT N gives nearest.
    distance_expr = Intent.description_embedding.cosine_distance(
        intent.description_embedding
    )
    candidates_stmt = (
        select(Intent, distance_expr.label("distance"))
        .where(Intent.id != intent.id)
        .where(Intent.side == opposite_side)
        .where(Intent.category == intent.category)
        .where(Intent.status == "active")
        .where(Intent.user_id != intent.user_id)
        .where(Intent.expires_at > _utcnow_naive())
        .order_by(distance_expr.asc())
        .limit(limit * _OVERSAMPLING_MULTIPLIER)
    )
    rows = (await db.execute(candidates_stmt)).all()

    # 2. Application-side: price overlap + scoring.
    scored: list[_ScoredCandidate] = []
    for candidate, distance in rows:
        buy_intent, sell_intent = _resolve_buy_sell(intent, candidate)
        if (
            buy_intent.reservation_price_cents
            < sell_intent.reservation_price_cents
        ):
            continue  # no price overlap
        similarity = float(1.0 - distance)  # cosine_distance = 1 - cosine_sim
        # Clamp to [0, 1]: small numerical drift on identical vectors can
        # produce 1.000000001 from the cosine-distance operator.
        similarity = max(0.0, min(1.0, similarity))
        price_proximity = compute_price_proximity(
            buyer_cap_cents=buy_intent.reservation_price_cents,
            buyer_ideal_cents=buy_intent.ideal_price_cents,
            seller_floor_cents=sell_intent.reservation_price_cents,
            seller_ideal_cents=sell_intent.ideal_price_cents,
        )
        combined = combine_scores(
            similarity=similarity, price_proximity=price_proximity
        )
        scored.append(
            _ScoredCandidate(
                buy_intent_id=buy_intent.id,
                sell_intent_id=sell_intent.id,
                buy_user_id=buy_intent.user_id,
                sell_user_id=sell_intent.user_id,
                similarity=similarity,
                price_proximity=price_proximity,
                combined=combined,
            )
        )

    scored.sort(key=lambda s: s.combined, reverse=True)
    top = scored[:limit]

    # 3. Upsert + audit. We split SELECT + INSERT/UPDATE per row instead
    #    of `ON CONFLICT DO UPDATE` because the audit semantics need to
    #    distinguish "newly discovered" from "score updated". Volumes
    #    are tiny (≤limit per call), so the extra round-trips don't matter.
    persisted_ids: list[str] = []
    for sc in top:
        match_id = await _upsert_match(db, sc, owning_user_id=intent.user_id)
        persisted_ids.append(match_id)

    await db.commit()

    if not persisted_ids:
        return []

    final_stmt = (
        select(Match)
        .where(Match.id.in_(persisted_ids))
        .order_by(Match.combined_score.desc())
    )
    return list((await db.scalars(final_stmt)).all())


async def _find_matches_for_intent_anthropic(
    db: AsyncSession,
    *,
    intent_id: str,
    limit: int = DEFAULT_MATCH_LIMIT,
    anthropic_client: AsyncAnthropic | None = None,
) -> list[Match]:
    """Discover matches without embeddings: SQL pre-filter + Claude ranker.

    This is the Anthropic-only V0 path. It keeps hard privacy boundaries:
    SQL does categorical/opposite-side/price-overlap filtering and Claude
    receives only marketplace-facing item fields for semantic compatibility.
    Strategic price scoring stays deterministic in-process.
    """
    intent = await db.get(Intent, intent_id)
    if intent is None or intent.status != "active":
        return []
    if intent.side == "trade":
        raise TradeMatchingNotImplemented(
            "TRADE matching not implemented in V0 (FASE 8)"
        )

    limit = max(1, min(MAX_MATCH_LIMIT, limit))
    if await _matching_cost_cap_reached(db, user_id=intent.user_id):
        return []

    candidates = await _anthropic_candidate_rows(db, intent=intent, limit=limit)
    if not candidates:
        await db.commit()
        return []

    try:
        scored = await _score_candidates_with_anthropic(
            db=db,
            intent=intent,
            candidates=candidates,
            client=anthropic_client,
        )
    except MatchingProviderUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001
        log.error(
            "match.anthropic.call_failed",
            intent_id=intent.id,
            user_id=intent.user_id,
            error=type(exc).__name__,
            message=str(exc),
        )
        raise MatchingProviderUnavailable(
            f"Anthropic matching call failed: {type(exc).__name__}"
        ) from exc
    scored.sort(key=lambda s: s.combined, reverse=True)
    top = scored[:limit]

    persisted_ids: list[str] = []
    for sc in top:
        match_id = await _upsert_match(db, sc, owning_user_id=intent.user_id)
        persisted_ids.append(match_id)

    await db.commit()

    if not persisted_ids:
        return []

    final_stmt = (
        select(Match)
        .where(Match.id.in_(persisted_ids))
        .order_by(Match.combined_score.desc())
    )
    return list((await db.scalars(final_stmt)).all())


async def _matching_cost_cap_reached(
    db: AsyncSession, *, user_id: str
) -> bool:
    today_cost = await cost_tracking_service.get_today_cost_usd(db)
    if today_cost >= settings.max_daily_llm_cost_usd:
        log.warning(
            "match.anthropic.global_cost_cap_reached",
            user_id=user_id,
            today_cost_usd=round(today_cost, 6),
            cap_usd=settings.max_daily_llm_cost_usd,
        )
        return True

    user_cost = await cost_tracking_service.get_user_cost_today(
        db, user_id=user_id
    )
    if user_cost >= settings.daily_user_cost_cap_usd:
        log.warning(
            "match.anthropic.user_cost_cap_reached",
            user_id=user_id,
            user_cost_usd=round(user_cost, 6),
            cap_usd=settings.daily_user_cost_cap_usd,
        )
        return True
    return False


async def _anthropic_candidate_rows(
    db: AsyncSession, *, intent: Intent, limit: int
) -> list[Intent]:
    opposite_side = "buy" if intent.side == "sell" else "sell"
    candidate_limit = max(
        limit,
        min(settings.anthropic_match_candidate_limit, MAX_MATCH_LIMIT * 5),
    )
    filters = [
        Intent.id != intent.id,
        Intent.side == opposite_side,
        Intent.category == intent.category,
        Intent.status == "active",
        Intent.user_id != intent.user_id,
        Intent.expires_at > _utcnow_naive(),
    ]
    if intent.side == "buy":
        # Candidate is SELL: seller floor must fit buyer cap.
        filters.append(Intent.reservation_price_cents <= intent.reservation_price_cents)
    else:
        # Candidate is BUY: buyer cap must cover seller floor.
        filters.append(Intent.reservation_price_cents >= intent.reservation_price_cents)

    return list(
        await db.scalars(
            select(Intent)
            .where(*filters)
            .order_by(Intent.created_at.desc())
            .limit(candidate_limit)
        )
    )


async def _score_candidates_with_anthropic(
    *,
    db: AsyncSession,
    intent: Intent,
    candidates: list[Intent],
    client: AsyncAnthropic | None,
) -> list[_ScoredCandidate]:
    client = client or _default_anthropic_client()
    candidate_by_id = {candidate.id: candidate for candidate in candidates}
    response = await client.messages.create(
        model=settings.anthropic_model,
        max_tokens=settings.anthropic_match_max_tokens,
        temperature=0,
        system=_anthropic_match_system_prompt(),
        messages=[
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "source_intent": _intent_for_anthropic(intent),
                        "candidate_intents": [
                            _intent_for_anthropic(candidate)
                            for candidate in candidates
                        ],
                    },
                    ensure_ascii=False,
                ),
            }
        ],
    )

    response_model = getattr(response, "model", None) or settings.anthropic_model
    usage = getattr(response, "usage", None)
    estimated_cost = anthropic_pricing.calculate_cost_usd(
        response_model,
        input_tokens=getattr(usage, "input_tokens", 0) or 0,
        output_tokens=getattr(usage, "output_tokens", 0) or 0,
    )
    await _record_matching_cost(
        db,
        intent.user_id,
        model=response_model,
        cost_usd=estimated_cost,
    )

    raw_scores = _parse_anthropic_match_scores(_response_text(response))
    scored: list[_ScoredCandidate] = []
    for candidate_id, semantic_score in raw_scores.items():
        candidate = candidate_by_id.get(candidate_id)
        if candidate is None:
            continue
        buy_intent, sell_intent = _resolve_buy_sell(intent, candidate)
        price_proximity = compute_price_proximity(
            buyer_cap_cents=buy_intent.reservation_price_cents,
            buyer_ideal_cents=buy_intent.ideal_price_cents,
            seller_floor_cents=sell_intent.reservation_price_cents,
            seller_ideal_cents=sell_intent.ideal_price_cents,
        )
        combined = combine_scores(
            similarity=semantic_score,
            price_proximity=price_proximity,
        )
        scored.append(
            _ScoredCandidate(
                buy_intent_id=buy_intent.id,
                sell_intent_id=sell_intent.id,
                buy_user_id=buy_intent.user_id,
                sell_user_id=sell_intent.user_id,
                similarity=semantic_score,
                price_proximity=price_proximity,
                combined=combined,
            )
        )
    return scored


def _default_anthropic_client() -> AsyncAnthropic:
    if not settings.anthropic_api_key:
        raise MatchingProviderUnavailable(
            "ANTHROPIC_API_KEY is required when MATCHING_BACKEND=anthropic"
        )
    return AsyncAnthropic(api_key=settings.anthropic_api_key)


async def _record_matching_cost(
    db: AsyncSession, user_id: str, *, model: str, cost_usd: float
) -> None:
    from app.core.metrics import COST_USD_TOTAL

    COST_USD_TOTAL.labels(user_id=user_id, model=model).inc(cost_usd)
    await cost_tracking_service.upsert_daily_cost(
        db, user_id=user_id, cost_usd=cost_usd
    )


def _anthropic_match_system_prompt() -> str:
    return """You score compatibility between marketplace intents.

Return ONLY a JSON object with this exact shape:
{"scores":[{"candidate_id":"...","semantic_score":0.0}]}

Rules:
- semantic_score is a number from 0 to 1.
- 1.0 means same item/need and very likely compatible.
- 0.5 means weak but plausible.
- 0.0 means not compatible.
- Score semantic/item compatibility only. Price overlap is handled elsewhere.
- Use only the fields provided; do not infer private user identity."""


def _intent_for_anthropic(intent: Intent) -> dict[str, Any]:
    return {
        "candidate_id": intent.id,
        "side": intent.side,
        "title": intent.title,
        "description": intent.description or "",
        "category": intent.category,
        "location": _location_from_constraints(intent.hard_constraints),
    }


def _location_from_constraints(
    hard_constraints: dict[str, Any] | None,
) -> str | None:
    if not hard_constraints:
        return None
    location = hard_constraints.get("location")
    return location if isinstance(location, str) else None


def _response_text(response: Any) -> str:
    blocks = getattr(response, "content", []) or []
    return "\n".join(
        getattr(block, "text", "")
        for block in blocks
        if getattr(block, "type", None) == "text"
    ).strip()


def _parse_anthropic_match_scores(text: str) -> dict[str, float]:
    try:
        payload = json.loads(_extract_json_object(text))
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "match.anthropic.invalid_json",
            error=type(exc).__name__,
            message=str(exc),
        )
        return {}

    scores = payload.get("scores") if isinstance(payload, dict) else None
    if not isinstance(scores, list):
        return {}

    parsed: dict[str, float] = {}
    for item in scores:
        if not isinstance(item, dict):
            continue
        candidate_id = item.get("candidate_id")
        semantic_score = item.get("semantic_score")
        if not isinstance(candidate_id, str):
            continue
        try:
            score = float(semantic_score)
        except (TypeError, ValueError):
            continue
        parsed[candidate_id] = max(0.0, min(1.0, score))
    return parsed


def _extract_json_object(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`")
        if stripped.lower().startswith("json"):
            stripped = stripped[4:].strip()
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError("no JSON object in Anthropic response")
    return stripped[start : end + 1]


async def _upsert_match(
    db: AsyncSession, sc: _ScoredCandidate, *, owning_user_id: str
) -> str:
    """Insert or score-update a `Match`. Returns the row's id.

    Audit emission policy:
      - net-new row → `create_match`
      - existing row with combined_score moving by ≥ `_AUDIT_SCORE_DELTA`
        → `update_match_score`
      - existing row with sub-threshold drift → silent (idempotent re-run)
    """
    existing = await db.scalar(
        select(Match)
        .where(Match.buy_intent_id == sc.buy_intent_id)
        .where(Match.sell_intent_id == sc.sell_intent_id)
    )
    if existing is None:
        new_match = Match(
            buy_intent_id=sc.buy_intent_id,
            sell_intent_id=sc.sell_intent_id,
            similarity_score=round(sc.similarity, 4),
            price_overlap=True,
            price_proximity_score=round(sc.price_proximity, 4),
            combined_score=round(sc.combined, 4),
            status="discovered",
        )
        db.add(new_match)
        await db.flush()
        await audit_service.log_intent_event(
            db,
            user_id=owning_user_id,
            action=audit_service.MatchActions.CREATE,
            params={
                "match_id": new_match.id,
                "buy_intent_id": sc.buy_intent_id,
                "sell_intent_id": sc.sell_intent_id,
            },
            result={
                "similarity": round(sc.similarity, 4),
                "price_proximity": round(sc.price_proximity, 4),
                "combined": round(sc.combined, 4),
            },
            success=True,
        )
        from app.core.metrics import MATCHES_DISCOVERED_TOTAL
        MATCHES_DISCOVERED_TOTAL.inc()

        # 6.1 — fire-and-forget UX notification to BOTH parties on net-new
        # match. We deliberately don't notify on score-only updates: a
        # match that already exists is already in the user's list, the
        # ranking shift is internal.
        from app.services import notification_service

        for recipient in (sc.buy_user_id, sc.sell_user_id):
            await notification_service.create_notification(
                db,
                user_id=recipient,
                notification_type=notification_service.NotificationType.NEW_MATCH_DISCOVERED,
                title="Nuovo match trovato",
                body="Abbiamo trovato un intent compatibile.",
                payload={
                    "match_id": new_match.id,
                    "buy_intent_id": sc.buy_intent_id,
                    "sell_intent_id": sc.sell_intent_id,
                    "combined_score": round(sc.combined, 4),
                },
            )
        return new_match.id

    old_combined = float(existing.combined_score or 0.0)
    existing.similarity_score = round(sc.similarity, 4)
    existing.price_proximity_score = round(sc.price_proximity, 4)
    existing.combined_score = round(sc.combined, 4)
    existing.price_overlap = True
    await db.flush()

    if abs(sc.combined - old_combined) >= _AUDIT_SCORE_DELTA:
        await audit_service.log_intent_event(
            db,
            user_id=owning_user_id,
            action=audit_service.MatchActions.SCORE_UPDATED,
            params={
                "match_id": existing.id,
                "old_combined": round(old_combined, 4),
                "new_combined": round(sc.combined, 4),
            },
            result={
                "similarity": round(sc.similarity, 4),
                "price_proximity": round(sc.price_proximity, 4),
            },
            success=True,
        )
    return existing.id


# ---------------------------------------------------------------------------
# Read-only listing for API
# ---------------------------------------------------------------------------


@dataclass
class MatchListPage:
    rows: list[Match]
    total: int
    limit: int
    offset: int


async def list_matches_for_intent(
    db: AsyncSession,
    *,
    user_id: str,
    intent_id: str,
    limit: int = DEFAULT_MATCH_LIMIT,
    offset: int = 0,
    min_score: float = 0.0,
) -> MatchListPage:
    """List matches for `intent_id`, owner-only.

    Raises `IntentNotFoundForMatching` (404) if the intent doesn't exist
    OR if it belongs to another user — both 404 to avoid leaking existence.
    """
    intent = await db.get(Intent, intent_id)
    if intent is None or intent.user_id != user_id:
        raise IntentNotFoundForMatching(f"intent {intent_id!r} not found")

    limit = max(1, min(MAX_MATCH_LIMIT, limit))
    offset = max(0, offset)

    base_filters = [
        or_(
            Match.buy_intent_id == intent_id,
            Match.sell_intent_id == intent_id,
        ),
        Match.combined_score >= min_score,
    ]

    total = int(
        await db.scalar(
            select(func.count()).select_from(Match).where(and_(*base_filters))
        )
        or 0
    )

    rows = list(
        await db.scalars(
            select(Match)
            .where(and_(*base_filters))
            .order_by(Match.combined_score.desc())
            .limit(limit)
            .offset(offset)
        )
    )

    return MatchListPage(rows=rows, total=total, limit=limit, offset=offset)


async def get_match_for_user(
    db: AsyncSession, *, user_id: str, match_id: str
) -> Match:
    """Owner-only fetch of a Match.

    Raises `MatchNotFound` (404) if the row doesn't exist; `NotMatchOwner`
    (403) if it exists but the user doesn't own either side. The 403 is
    intentional here (not 404) because the detail endpoint requires
    tier ≥ 2 and the caller already proved ownership of *some* intent —
    a 404 would be misleading at this point in the flow.
    """
    match = await db.get(Match, match_id)
    if match is None:
        raise MatchNotFound(f"match {match_id!r} not found")

    buy_intent = await db.get(Intent, match.buy_intent_id)
    sell_intent = await db.get(Intent, match.sell_intent_id)

    if (
        buy_intent is None
        or sell_intent is None
        or (buy_intent.user_id != user_id and sell_intent.user_id != user_id)
    ):
        raise NotMatchOwner(
            "match exists but caller doesn't own either intent"
        )
    return match


# ---------------------------------------------------------------------------
# Lifecycle hooks
# ---------------------------------------------------------------------------


async def mark_match_negotiating(
    db: AsyncSession, *, match_id: str
) -> Match:
    """Transition `discovered → negotiating`. Used by 5.1 when a negotiation
    starts. Idempotent: already-`negotiating` is a no-op.
    """
    match = await db.get(Match, match_id)
    if match is None:
        raise MatchNotFound(f"match {match_id!r} not found")
    if match.status == "negotiating":
        return match
    if match.status != "discovered":
        # Don't silently transition out of terminal states (agreed/rejected/
        # expired) — that's a programming error in 5.x if it happens.
        raise InvalidMatchTransition(
            f"cannot mark match in status {match.status!r} as negotiating"
        )
    match.status = "negotiating"
    await db.flush()
    return match


async def expire_matches_for_intent(
    db: AsyncSession, *, intent_id: str
) -> int:
    """Mark all matches involving `intent_id` as expired. Returns row count.

    Used by `intent_service.cancel_intent` and the future scheduler-driven
    intent-expiry sweep. Only transitions out of pre-terminal states
    (`discovered`, `negotiating`) so terminal history (`agreed`, `rejected`)
    is preserved.
    """
    result = await db.execute(
        update(Match)
        .where(
            or_(
                Match.buy_intent_id == intent_id,
                Match.sell_intent_id == intent_id,
            )
        )
        .where(Match.status.in_(("discovered", "negotiating")))
        .values(status="expired")
    )
    return int(result.rowcount or 0)
