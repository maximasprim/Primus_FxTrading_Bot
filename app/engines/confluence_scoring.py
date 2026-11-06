"""
Confluence Scoring Engine (spec §11-12).

Takes structured, already-computed signals from every other engine
(StrategyInputs below) and produces a 0-100 score plus a grade. This module
does NOT compute the underlying analysis itself - it only scores it - keeping
the "no single strategy triggers a trade" principle enforceable in one place.

See tests/test_confluence_scoring.py for example inputs/outputs.
"""
from dataclasses import dataclass

from app.config import get_settings
from app.models.schemas import ScoreBreakdown, SetupGrade


@dataclass
class StrategyInputs:
    """The distilled outputs of every engine, ready to be scored.
    Populate this from the real engines once Phase 2 is complete -
    for now, services/signal_service.py builds this from partial data.
    """

    # Market Structure - up to 20 pts
    structure_alignment: str  # "strong" | "moderate" | "unclear"

    # Higher Timeframe Alignment - up to 20 pts
    htf_alignment: str  # "full" | "partial" | "conflict"

    # Liquidity Confirmation - up to 15 pts
    liquidity_confirmation: str  # "swept" | "potential_target" | "none"

    # Entry Zone - up to 15 pts
    entry_zone_quality: str  # "strong" | "moderate" | "weak"

    # Momentum - up to 10 pts
    momentum: str  # "strong" | "neutral" | "opposing"

    # Risk-to-Reward - up to 10 pts
    risk_reward_ratio: float

    # News & Market Conditions - up to 10 pts
    news_condition: str  # "favorable" | "moderate_risk" | "major_event_imminent"


def _score_structure(value: str) -> tuple[int, str]:
    points = {"strong": 20, "moderate": 10, "unclear": 0}.get(value, 0)
    reason = {
        "strong": "Market structure is strongly aligned across timeframes (20/20).",
        "moderate": "Market structure is moderately aligned - some conflicting signals (10/20).",
        "unclear": "Market structure is unclear; no reliable alignment found (0/20).",
    }.get(value, f"Unrecognized structure_alignment value '{value}' (0/20).")
    return points, reason


def _score_htf(value: str) -> tuple[int, str]:
    points = {"full": 20, "partial": 10, "conflict": 0}.get(value, 0)
    reason = {
        "full": "Entry timeframe is in full agreement with the higher-timeframe bias (20/20).",
        "partial": "Entry timeframe only partially agrees with the higher-timeframe bias (10/20).",
        "conflict": "Entry timeframe conflicts with the higher-timeframe bias (0/20).",
    }.get(value, f"Unrecognized htf_alignment value '{value}' (0/20).")
    return points, reason


def _score_liquidity(value: str) -> tuple[int, str]:
    points = {"swept": 15, "potential_target": 8, "none": 0}.get(value, 0)
    reason = {
        "swept": "A liquidity pool has already been swept and price closed back inside (15/15).",
        "potential_target": "A nearby liquidity pool exists but hasn't been swept yet (8/15).",
        "none": "No relevant liquidity pools identified (0/15).",
    }.get(value, f"Unrecognized liquidity_confirmation value '{value}' (0/15).")
    return points, reason


def _score_entry_zone(value: str) -> tuple[int, str]:
    points = {"strong": 15, "moderate": 8, "weak": 0}.get(value, 0)
    reason = {
        "strong": "Entry zone is fresh and reinforced by a supply/demand zone or order block (15/15).",
        "moderate": "Entry zone exists (fresh FVG) but lacks additional confirmation (8/15).",
        "weak": "No fresh/valid entry zone available (0/15).",
    }.get(value, f"Unrecognized entry_zone_quality value '{value}' (0/15).")
    return points, reason


def _score_momentum(value: str) -> tuple[int, str]:
    points = {"strong": 10, "neutral": 5, "opposing": 0}.get(value, 0)
    reason = {
        "strong": "Momentum indicators confirm the move without being over-extended (10/10).",
        "neutral": "Momentum is neutral - neither confirming nor opposing (5/10).",
        "opposing": "Momentum is opposing the proposed direction (0/10).",
    }.get(value, f"Unrecognized momentum value '{value}' (0/10).")
    return points, reason


def _score_risk_reward(rr: float) -> tuple[int, str]:
    if rr >= 3:
        return 10, f"Risk-to-reward is {rr:.2f} (>= 3.0) - excellent (10/10)."
    if rr >= 2:
        return 7, f"Risk-to-reward is {rr:.2f} (>= 2.0) - good (7/10)."
    if rr >= 1.5:
        return 3, f"Risk-to-reward is {rr:.2f} (>= 1.5) - marginal (3/10)."
    return 0, f"Risk-to-reward is {rr:.2f} (< 1.5) - too low (0/10)."


def _score_news(value: str) -> tuple[int, str]:
    points = {"favorable": 10, "moderate_risk": 5, "major_event_imminent": 0}.get(value, 0)
    reason = {
        "favorable": "No significant news risk in the relevant window (10/10).",
        "moderate_risk": "Moderate news risk nearby - proceed with caution (5/10).",
        "major_event_imminent": "A major news event is imminent - high risk (0/10).",
    }.get(value, f"Unrecognized news_condition value '{value}' (0/10).")
    return points, reason


def score_setup(inputs: StrategyInputs) -> ScoreBreakdown:
    structure_pts, structure_reason = _score_structure(inputs.structure_alignment)
    htf_pts, htf_reason = _score_htf(inputs.htf_alignment)
    liquidity_pts, liquidity_reason = _score_liquidity(inputs.liquidity_confirmation)
    entry_pts, entry_reason = _score_entry_zone(inputs.entry_zone_quality)
    momentum_pts, momentum_reason = _score_momentum(inputs.momentum)
    rr_pts, rr_reason = _score_risk_reward(inputs.risk_reward_ratio)
    news_pts, news_reason = _score_news(inputs.news_condition)

    return ScoreBreakdown(
        market_structure=structure_pts,
        higher_timeframe_alignment=htf_pts,
        liquidity_confirmation=liquidity_pts,
        entry_zone_quality=entry_pts,
        momentum=momentum_pts,
        risk_reward=rr_pts,
        news_and_conditions=news_pts,
        reasons={
            "market_structure": structure_reason,
            "higher_timeframe_alignment": htf_reason,
            "liquidity_confirmation": liquidity_reason,
            "entry_zone_quality": entry_reason,
            "momentum": momentum_reason,
            "risk_reward": rr_reason,
            "news_and_conditions": news_reason,
        },
    )


def confidence_level(total_score: int) -> str:
    """Plain-language confidence label companion to classify_grade() - same
    thresholds, worded for a human reading the dashboard/chat rather than
    for internal grading logic."""
    if total_score >= 85:
        return "Very High"
    if total_score >= 75:
        return "High"
    if total_score >= 60:
        return "Moderate"
    if total_score >= 40:
        return "Low"
    return "Very Low"


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
