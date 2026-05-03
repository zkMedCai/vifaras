"""Minimal Anthropic API smoke test for Vifaras platform-managed AI.

Purpose:
  - verify ANTHROPIC_API_KEY is loaded through app.core.config.Settings
  - verify the configured model accepts a tiny Messages API call
  - print usage + local cost estimate without touching DB or scheduler

Run:
  uv run python scripts/smoke_anthropic.py
"""
from __future__ import annotations

import asyncio

from anthropic import AsyncAnthropic

from app.core.config import settings
from app.services import anthropic_pricing


EXPECTED_TEXT = "vifaras-anthropic-smoke-ok"


async def main() -> None:
    if not settings.anthropic_api_key:
        raise SystemExit(
            "ANTHROPIC_API_KEY is empty. Set it in .env or the process environment."
        )

    client = AsyncAnthropic(api_key=settings.anthropic_api_key)
    response = await client.messages.create(
        model=settings.anthropic_model,
        max_tokens=24,
        messages=[
            {
                "role": "user",
                "content": f"Reply with exactly this text and nothing else: {EXPECTED_TEXT}",
            }
        ],
    )

    text = "\n".join(
        block.text for block in response.content if getattr(block, "type", None) == "text"
    ).strip()
    model = getattr(response, "model", None) or settings.anthropic_model
    usage = response.usage
    estimated_cost = anthropic_pricing.calculate_cost_usd(
        model,
        input_tokens=getattr(usage, "input_tokens", 0) or 0,
        output_tokens=getattr(usage, "output_tokens", 0) or 0,
    )

    print("Anthropic smoke OK")
    print(f"model={model}")
    print(f"stop_reason={response.stop_reason}")
    print(
        "usage="
        f"input_tokens={getattr(usage, 'input_tokens', 0) or 0},"
        f"output_tokens={getattr(usage, 'output_tokens', 0) or 0}"
    )
    print(f"estimated_cost_usd={estimated_cost:.8f}")
    print(f"text={text}")

    if EXPECTED_TEXT not in text:
        raise SystemExit("Anthropic smoke returned unexpected text.")


if __name__ == "__main__":
    asyncio.run(main())
