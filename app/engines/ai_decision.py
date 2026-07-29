"""
AI Analysis Engine (spec §16).

Per the spec, the AI must NOT invent technical data — it receives already-computed
structured facts from every other engine and is only asked to reason over them:
bias, structure, liquidity, entry validity, alignment, regime, news risk,
risk/reward, invalidation, and alternative scenario.

This module builds that structured prompt and calls the Anthropic API. The
actual `httpx` call is included, but requires ANTHROPIC_API_KEY to be set —
wire it up as part of Phase 5.
"""
import json
from dataclasses import asdict, dataclass, field

import httpx

from app.config import get_settings
from app.models.schemas import ScenarioAnalysis

SYSTEM_PROMPT = """You are a disciplined professional Forex/XAU trading analyst assistant.

You will be given structured, pre-computed technical facts about an instrument.
You must NOT invent or assume any technical data beyond what is given.

Answer strictly using the structured facts provided. Your response must be a
single JSON object with exactly these keys:
  "higher_timeframe_bias": string
  "current_structure_summary": string
  "liquidity_summary": string
  "premium_or_discount": string
  "entry_zone_valid": boolean
  "strategy_alignment_summary": string
  "trend_or_range": string
  "news_risk_acceptable": boolean
  "risk_reward_acceptable": boolean
  "invalidation_condition": string
  "primary_scenario": string
  "alternative_scenario": string
  "reasoning": string  (2-4 sentences, plain language, no certainty claims)

Never claim a trade is guaranteed or "perfect". If the facts are contradictory
or incomplete, say so plainly and prefer WAIT / NO TRADE framing in your reasoning.
Respond with ONLY the JSON object, no other text."""


@dataclass
class StructuredMarketFacts:
    """The exact structured inputs the spec says the AI is allowed to see (§16)."""

    instrument: str
    timeframe: str
    market_structure: dict
    liquidity_zones: list[dict]
    fvgs: list[dict]
    order_blocks: list[dict]
    support_resistance: dict
    indicators: dict
    volatility: dict
    market_regime: str
    economic_news: list[dict]
    risk_reward: float
    fields: dict = field(default_factory=dict)  # room for any extra structured facts


async def run_ai_analysis(facts: StructuredMarketFacts) -> ScenarioAnalysis | None:
    """Calls the Anthropic API with the structured facts and parses the
    response into a ScenarioAnalysis. Returns None if no API key is configured
    (callers should fall back to a rule-based scenario description in that case).
    """
    settings = get_settings()
    if not settings.anthropic_api_key:
        return None

    user_content = (
        "Structured market facts (do not invent anything beyond this):\n\n"
        + json.dumps(asdict(facts), indent=2, default=str)
    )

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": settings.anthropic_api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-sonnet-4-6",
                "max_tokens": 1000,
                "system": SYSTEM_PROMPT,
                "messages": [{"role": "user", "content": user_content}],
            },
        )
        response.raise_for_status()
        data = response.json()

    text_blocks = [block["text"] for block in data.get("content", []) if block.get("type") == "text"]
    raw_text = "\n".join(text_blocks).strip()

    try:
        parsed = json.loads(raw_text)
    except json.JSONDecodeError:
        return None

    return ScenarioAnalysis(
        primary_scenario=parsed.get("primary_scenario", ""),
        alternative_scenario=parsed.get("alternative_scenario", ""),
        confirmation_required=parsed.get("strategy_alignment_summary", ""),
        invalidation_condition=parsed.get("invalidation_condition", ""),
    )


def rule_based_scenario_fallback(
    direction: str, invalidation_price: float, htf_bias: str
) -> ScenarioAnalysis:
    """Deterministic fallback used when no AI key is configured, so the rest
    of the pipeline (spec §15: every serious setup includes scenario analysis)
    still functions without an LLM dependency.
    """
    if direction == "BUY":
        primary = "Price continues higher in line with the higher-timeframe bullish bias."
        alternative = "Price fails to hold the entry zone and breaks down, invalidating the bullish thesis."
    else:
        primary = "Price continues lower in line with the higher-timeframe bearish bias."
        alternative = "Price fails to hold the entry zone and breaks up, invalidating the bearish thesis."

    return ScenarioAnalysis(
        primary_scenario=primary,
        alternative_scenario=alternative,
        confirmation_required="Wait for a confirmed market structure shift before entry.",
        invalidation_condition=f"A confirmed close beyond {invalidation_price} invalidates this setup "
        f"(higher-timeframe bias: {htf_bias}).",
    )
