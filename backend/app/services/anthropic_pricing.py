"""Anthropic API pricing — model rate table + cost calculator (FASE 7.3.2).

Extracted from `app/agents/orchestrator.py` to enable:

  - Test isolation: pricing logic is a pure function, testable without
    spinning up the orchestrator + DB session.
  - Future-proofing: V0.5+ adding Haiku/Opus = adding a row to the
    rate table, no orchestrator refactor.
  - Single Responsibility: orchestrator orchestrates, pricing prices.

Rates are USD per million tokens, model-id keyed. Source:
https://docs.anthropic.com/en/docs/about-claude/pricing (snapshot
2026-05). Prompt-cache discount is NOT modeled in V0 — `_estimate_cost`
returns list-price; real billing comes from the Anthropic dashboard.
The estimate exists to power the daily cap kill-switch and the
per-user soft cap, both of which want a conservative upper bound on
spend, not a billing-accurate figure.

V0.5+ refresh pattern: env override (`ANTHROPIC_PRICING_OVERRIDE_JSON`)
+ a periodic checker. Today the rates are hardcoded; the founder
audits them quarterly.
"""
from __future__ import annotations

from typing import Final, Mapping


_USD_PER_MTOK: Final[Mapping[str, Mapping[str, float]]] = {
    # Sonnet 4.5 — V0 production model.
    "claude-sonnet-4-5": {"input": 3.00, "output": 15.00},
    "claude-sonnet-4-5-20250929": {"input": 3.00, "output": 15.00},
    # Reserved for V0.5+ multi-model dispatch. Listed today so the
    # fallback in `calculate_cost_usd` is reachable from tests.
    "claude-opus-4-7": {"input": 15.00, "output": 75.00},
    "claude-haiku-4-5-20251001": {"input": 0.80, "output": 4.00},
}

# Conservative fallback when an unknown model id surfaces (e.g. an
# `anthropic` SDK upgrade ships a new alias before we update the
# table). Sonnet rates over-estimate against Haiku and under-estimate
# against Opus — but ticks dispatched outside the explicit table are
# vanishingly rare in V0, and the cap is a guard rail, not a meter.
_FALLBACK_MODEL: Final[str] = "claude-sonnet-4-5"


def calculate_cost_usd(
    model: str, *, input_tokens: int, output_tokens: int
) -> float:
    """USD cost for a single Anthropic call. Pure function.

    Negative or `None`-derived tokens are normalised to 0 so the
    function is total over whatever shape `usage` returns. Unknown
    model ids fall back to Sonnet rates with a structured log emitted
    by `_lookup_rates` (so it shows up in observability when it does
    happen, but doesn't raise — the cap accumulator must never crash
    the tick).
    """
    in_toks = max(0, int(input_tokens or 0))
    out_toks = max(0, int(output_tokens or 0))
    rates = _lookup_rates(model)
    return (
        in_toks * rates["input"] / 1_000_000
        + out_toks * rates["output"] / 1_000_000
    )


def _lookup_rates(model: str) -> Mapping[str, float]:
    rates = _USD_PER_MTOK.get(model)
    if rates is not None:
        return rates
    # Don't log here at module import time; importer's structlog may
    # not be configured. The caller path runs inside the orchestrator
    # which has logging set up.
    from app.core.logging import log

    log.warning(
        "anthropic_pricing.unknown_model",
        model=model,
        fallback=_FALLBACK_MODEL,
    )
    return _USD_PER_MTOK[_FALLBACK_MODEL]


def known_models() -> tuple[str, ...]:
    """Tuple of model ids the rate table covers — handy for tests."""
    return tuple(_USD_PER_MTOK.keys())
