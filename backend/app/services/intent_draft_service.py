"""Natural-language intent draft extraction.

This is the Project Deal-style entrypoint for intent creation: the user
describes what they want the agent to do, Claude turns it into a structured
draft, and the frontend shows that draft for human review before persistence.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Final

from anthropic import AsyncAnthropic
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import categories
from app.core.config import settings
from app.core.logging import log
from app.services import anthropic_pricing, cost_tracking_service

MAX_PROMPT_LEN: Final[int] = 2000
MIN_PROMPT_LEN: Final[int] = 10
MAX_DRAFT_TOKENS: Final[int] = 1200


class IntentDraftError(Exception):
    code: str = "intent_draft_error"
    http_status: int = 400


class InvalidDraftPrompt(IntentDraftError):
    code = "invalid_draft_prompt"
    http_status = 422


class IntentDraftProviderUnavailable(IntentDraftError):
    code = "intent_draft_provider_unavailable"
    http_status = 503


class IntentDraftCostCapReached(IntentDraftError):
    code = "intent_draft_cost_cap_reached"
    http_status = 402


class IntentDraftParseFailed(IntentDraftError):
    code = "intent_draft_parse_failed"
    http_status = 502


class IntentDraft(BaseModel):
    side: str | None = Field(default=None)
    title: str = ""
    description: str | None = None
    category: str | None = None
    reservation_price_eur: float | None = None
    ideal_price_eur: float | None = None
    duration_days: int = 14
    hard_constraints: dict[str, Any] = Field(default_factory=dict)
    soft_preferences: dict[str, Any] = Field(default_factory=dict)
    confidence: float = 0.0
    missing_fields: list[str] = Field(default_factory=list)
    summary: str = ""


@dataclass
class IntentDraftResult:
    draft: IntentDraft
    model: str
    estimated_cost_usd: float
    raw_text: str | None = field(default=None, repr=False)


async def draft_intent_from_text(
    db: AsyncSession,
    *,
    user_id: str,
    prompt: str,
    anthropic_client: AsyncAnthropic | None = None,
) -> IntentDraftResult:
    prompt = prompt.strip()
    _validate_prompt(prompt)
    await _check_cost_caps(db, user_id=user_id)

    client = anthropic_client or _default_client()
    try:
        response = await client.messages.create(
            model=settings.anthropic_model,
            max_tokens=MAX_DRAFT_TOKENS,
            temperature=0,
            system=_system_prompt(),
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as exc:  # noqa: BLE001
        log.error(
            "intent_draft.anthropic_call_failed",
            user_id=user_id,
            error=type(exc).__name__,
            message=str(exc),
        )
        raise IntentDraftProviderUnavailable(
            f"Anthropic draft call failed: {type(exc).__name__}"
        ) from exc

    text = _response_text(response)
    draft = _parse_draft(text)
    model = getattr(response, "model", None) or settings.anthropic_model
    usage = getattr(response, "usage", None)
    estimated_cost = anthropic_pricing.calculate_cost_usd(
        model,
        input_tokens=getattr(usage, "input_tokens", 0) or 0,
        output_tokens=getattr(usage, "output_tokens", 0) or 0,
    )
    await _record_cost(db, user_id=user_id, model=model, cost_usd=estimated_cost)
    return IntentDraftResult(
        draft=draft,
        model=model,
        estimated_cost_usd=estimated_cost,
        raw_text=text,
    )


def _validate_prompt(prompt: str) -> None:
    if len(prompt) < MIN_PROMPT_LEN:
        raise InvalidDraftPrompt(
            f"prompt must be at least {MIN_PROMPT_LEN} characters"
        )
    if len(prompt) > MAX_PROMPT_LEN:
        raise InvalidDraftPrompt(
            f"prompt exceeds {MAX_PROMPT_LEN} characters"
        )


async def _check_cost_caps(db: AsyncSession, *, user_id: str) -> None:
    today_cost = await cost_tracking_service.get_today_cost_usd(db)
    if today_cost >= settings.max_daily_llm_cost_usd:
        raise IntentDraftCostCapReached("global daily LLM cost cap reached")

    user_cost = await cost_tracking_service.get_user_cost_today(
        db, user_id=user_id
    )
    if user_cost >= settings.daily_user_cost_cap_usd:
        raise IntentDraftCostCapReached("user daily LLM cost cap reached")


def _default_client() -> AsyncAnthropic:
    if not settings.anthropic_api_key:
        raise IntentDraftProviderUnavailable("ANTHROPIC_API_KEY is empty")
    return AsyncAnthropic(api_key=settings.anthropic_api_key, timeout=30.0)


async def _record_cost(
    db: AsyncSession, *, user_id: str, model: str, cost_usd: float
) -> None:
    from app.core.metrics import COST_USD_TOTAL

    COST_USD_TOTAL.labels(user_id=user_id, model=model).inc(cost_usd)
    await cost_tracking_service.upsert_daily_cost(
        db, user_id=user_id, cost_usd=cost_usd
    )
    await db.commit()


def _system_prompt() -> str:
    allowed_categories = "\n".join(f"- {cat}" for cat in categories.V0_CATEGORIES)
    return f"""You extract a marketplace intent draft from a user's natural-language instruction.

Return ONLY a JSON object. No markdown. No prose outside JSON.

Required JSON shape:
{{
  "side": "buy" | "sell" | null,
  "title": "short marketplace title, max 120 chars",
  "description": "clear public description, max 600 chars",
  "category": "one allowed category key or null",
  "reservation_price_eur": number | null,
  "ideal_price_eur": number | null,
  "duration_days": integer 1-30,
  "hard_constraints": {{"location": "City, IT"}},
  "soft_preferences": {{}},
  "confidence": number 0-1,
  "missing_fields": ["field_name"],
  "summary": "one short sentence in Italian"
}}

Rules:
- Use only these category keys:
{allowed_categories}
- SELL: reservation_price_eur is the minimum acceptable price.
- SELL: ideal_price_eur is the target price and must be >= reservation_price_eur.
- BUY: reservation_price_eur is the maximum acceptable price.
- BUY: ideal_price_eur is the target price and must be <= reservation_price_eur.
- Infer reasonable title, description and category from the instruction.
- Do NOT invent a price if none is stated. Put null and list the missing field.
- Normalize Italian cities as "City, IT" when present.
- If no location is present, hard_constraints={{}}.
- Default duration_days to 14 unless the user gives a duration.
- Keep public description free of phone numbers, emails, names, exact addresses and URLs."""


def _response_text(response: Any) -> str:
    blocks = getattr(response, "content", []) or []
    return "\n".join(
        getattr(block, "text", "")
        for block in blocks
        if getattr(block, "type", None) == "text"
    ).strip()


def _parse_draft(text: str) -> IntentDraft:
    try:
        payload = json.loads(_extract_json_object(text))
    except Exception as exc:  # noqa: BLE001
        raise IntentDraftParseFailed("model did not return valid JSON") from exc
    if not isinstance(payload, dict):
        raise IntentDraftParseFailed("model returned non-object JSON")

    draft = IntentDraft(
        side=_clean_side(payload.get("side")),
        title=_clean_str(payload.get("title"), max_len=200),
        description=_clean_optional_str(payload.get("description"), max_len=2000),
        category=_clean_category(payload.get("category")),
        reservation_price_eur=_clean_price(payload.get("reservation_price_eur")),
        ideal_price_eur=_clean_price(payload.get("ideal_price_eur")),
        duration_days=_clean_duration(payload.get("duration_days")),
        hard_constraints=_clean_hard_constraints(payload.get("hard_constraints")),
        soft_preferences=_clean_object(payload.get("soft_preferences")),
        confidence=_clean_confidence(payload.get("confidence")),
        summary=_clean_str(payload.get("summary"), max_len=300),
    )
    draft.missing_fields = _missing_fields(payload.get("missing_fields"), draft)
    return draft


def _extract_json_object(text: str) -> str:
    stripped = text.strip()
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError("no JSON object in response")
    return stripped[start : end + 1]


def _clean_side(value: Any) -> str | None:
    return value if value in {"buy", "sell"} else None


def _clean_category(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    return value if categories.is_allowed(value) else None


def _clean_str(value: Any, *, max_len: int) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()[:max_len]


def _clean_optional_str(value: Any, *, max_len: int) -> str | None:
    text = _clean_str(value, max_len=max_len)
    return text or None


def _clean_price(value: Any) -> float | None:
    if value is None:
        return None
    try:
        price = float(value)
    except (TypeError, ValueError):
        return None
    if price <= 0 or price > 10_000:
        return None
    return round(price, 2)


def _clean_duration(value: Any) -> int:
    try:
        duration = int(value)
    except (TypeError, ValueError):
        return 14
    return max(1, min(30, duration))


def _clean_confidence(value: Any) -> float:
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, confidence))


def _clean_object(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _clean_hard_constraints(value: Any) -> dict[str, Any]:
    obj = _clean_object(value)
    location = obj.get("location")
    if isinstance(location, str) and "," in location:
        city, country = location.rsplit(",", 1)
        country = country.strip().upper()
        if city.strip() and len(country) == 2:
            return {"location": f"{city.strip()}, {country}"}
    return {}


def _missing_fields(value: Any, draft: IntentDraft) -> list[str]:
    missing = set()
    if isinstance(value, list):
        missing.update(item for item in value if isinstance(item, str))
    if draft.side is None:
        missing.add("side")
    if not draft.title:
        missing.add("title")
    if draft.category is None:
        missing.add("category")
    if draft.reservation_price_eur is None:
        missing.add("reservation_price_eur")
    if draft.ideal_price_eur is None:
        missing.add("ideal_price_eur")
    return sorted(missing)
