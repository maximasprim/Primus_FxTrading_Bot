"""
AI Analysis Engine (spec §16).

Per the spec, the AI must NOT invent technical data - it receives already-computed
structured facts from every other engine and is only asked to reason over them:
bias, structure, liquidity, entry validity, alignment, regime, news risk,
risk/reward, invalidation, and alternative scenario.

This module builds that structured prompt and calls whichever AI provider is
currently selected (see app/ai/settings_manager.py), through the
provider-agnostic app/ai/base.py types. It never imports httpx or knows
about any provider's wire format directly - see app/ai/factory.py.

Callers (signal_service.py) decide whether to call run_ai_analysis() at
all - it's opt-in and cached there, since calling out to an LLM on every
analysis refresh would be slow and, for paid providers, costly. If it's
not called, or fails, or returns something unparseable, everything falls
back to rule_based_scenario_fallback() below so the rest of the pipeline
(spec §15: every serious setup includes scenario analysis) always works
without any AI dependency.
"""
import json
from dataclasses import asdict, dataclass, field

from app.ai.base import AIMessage, AIProvider, AIProviderError
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


async def run_ai_analysis(facts: StructuredMarketFacts, provider: AIProvider) -> ScenarioAnalysis | None:
    """Calls `provider` with the structured facts and parses the response
    into a ScenarioAnalysis. Returns None (never raises) on any failure --
    missing key, malformed JSON, provider/network error -- so callers can
    unconditionally fall back to rule_based_scenario_fallback()."""
    user_content = (
        "Structured market facts (do not invent anything beyond this):\n\n"
        + json.dumps(asdict(facts), indent=2, default=str)
    )

    try:
        response = await provider.complete(
            system=SYSTEM_PROMPT,
            messages=[AIMessage(role="user", content=user_content)],
            tools=None,
            max_tokens=1000,
        )
    except AIProviderError:
        return None

    try:
        parsed = json.loads(response.text.strip())
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
    """Deterministic fallback used when no AI key is configured, AI-enrichment
    is turned off, or the AI call/parse fails for any reason -- so the rest
    of the pipeline (spec §15: every serious setup includes scenario analysis)
    always functions without an LLM dependency. This is also the ONLY
    behavior a fresh install has, until AI analysis enrichment is
    explicitly turned on in Settings.
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
