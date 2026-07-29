"""
Confluence Scoring Engine (spec §11-12).

Takes structured, already-computed signals from every other engine
(StrategyInputs below) and produces a 0-100 score plus a grade. This module
does NOT compute the underlying analysis itself — it only scores it — keeping
the "no single strategy triggers a trade" principle enforceable in one place.

See tests/test_confluence_scoring.py for example inputs/outputs.
"""
from dataclasses import dataclass

from app.config import get_settings
from app.models.schemas import ScoreBreakdown, SetupGrade


@dataclass
class StrategyInputs:
    """The distilled outputs of every engine, ready to be scored.
    Populate this from the real engines once Phase 2 is complete —
    for now, services/signal_service.py builds this from partial data.
    """

    # Market Structure — up to 20 pts
    structure_alignment: str  # "strong" | "moderate" | "unclear"

    # Higher Timeframe Alignment — up to 20 pts
    htf_alignment: str  # "full" | "partial" | "conflict"

    # Liquidity Confirmation — up to 15 pts
    liquidity_confirmation: str  # "swept" | "potential_target" | "none"

    # Entry Zone — up to 15 pts
    entry_zone_quality: str  # "strong" | "moderate" | "weak"

    # Momentum — up to 10 pts
    momentum: str  # "strong" | "neutral" | "opposing"

    # Risk-to-Reward — up to 10 pts
    risk_reward_ratio: float

    # News & Market Conditions — up to 10 pts
    news_condition: str  # "favorable" | "moderate_risk" | "major_event_imminent"


def _score_structure(value: str) -> int:
    return {"strong": 20, "moderate": 10, "unclear": 0}.get(value, 0)


def _score_htf(value: str) -> int:
    return {"full": 20, "partial": 10, "conflict": 0}.get(value, 0)


def _score_liquidity(value: str) -> int:
    return {"swept": 15, "potential_target": 8, "none": 0}.get(value, 0)


def _score_entry_zone(value: str) -> int:
    return {"strong": 15, "moderate": 8, "weak": 0}.get(value, 0)


def _score_momentum(value: str) -> int:
    return {"strong": 10, "neutral": 5, "opposing": 0}.get(value, 0)


def _score_risk_reward(rr: float) -> int:
    if rr >= 3:
        return 10
    if rr >= 2:
        return 7
    if rr >= 1.5:
        return 3
    return 0


def _score_news(value: str) -> int:
    return {"favorable": 10, "moderate_risk": 5, "major_event_imminent": 0}.get(value, 0)


def score_setup(inputs: StrategyInputs) -> ScoreBreakdown:
    return ScoreBreakdown(
        market_structure=_score_structure(inputs.structure_alignment),
        higher_timeframe_alignment=_score_htf(inputs.htf_alignment),
        liquidity_confirmation=_score_liquidity(inputs.liquidity_confirmation),
        entry_zone_quality=_score_entry_zone(inputs.entry_zone_quality),
        momentum=_score_momentum(inputs.momentum),
        risk_reward=_score_risk_reward(inputs.risk_reward_ratio),
        news_and_conditions=_score_news(inputs.news_condition),
    )


def classify_grade(total_score: int) -> SetupGrade:
    """Spec §12 thresholds."""
    if total_score >= 85:
        return SetupGrade.A_PLUS
    if total_score >= 75:
        return SetupGrade.A
    if total_score >= 60:
        return SetupGrade.B
    return SetupGrade.NO_TRADE


def is_alertable(total_score: int) -> bool:
    """Whether this setup clears the configurable MINIMUM_ENTRY_SCORE for a
    Stage 3 confirmed-entry alert (spec §13)."""
    settings = get_settings()
    return total_score >= settings.minimum_entry_score
