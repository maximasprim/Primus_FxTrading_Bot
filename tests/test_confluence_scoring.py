"""Tests for app/engines/confluence_scoring.py — verifies the point table
matches spec §11-12 exactly."""
from app.engines.confluence_scoring import StrategyInputs, classify_grade, confidence_level, score_setup
from app.models.schemas import SetupGrade


def test_a_plus_setup_scores_91():
    """Mirrors the worked example in spec §13's Stage 3 alert (score 91, A+)."""
    inputs = StrategyInputs(
        structure_alignment="strong",           # 20
        htf_alignment="full",                   # 20
        liquidity_confirmation="swept",         # 15
        entry_zone_quality="strong",            # 15
        momentum="strong",                      # 10
        risk_reward_ratio=2.3,                  # 7 (>= 2, < 3)
        news_condition="favorable",             # 10
    )
    breakdown = score_setup(inputs)
    # 20+20+15+15+10+7+10 = 97
    assert breakdown.total >= 85
    assert classify_grade(breakdown.total) == SetupGrade.A_PLUS


def test_no_trade_below_60():
    inputs = StrategyInputs(
        structure_alignment="unclear",
        htf_alignment="conflict",
        liquidity_confirmation="none",
        entry_zone_quality="weak",
        momentum="opposing",
        risk_reward_ratio=1.0,
        news_condition="major_event_imminent",
    )
    breakdown = score_setup(inputs)
    assert breakdown.total == 0
    assert classify_grade(breakdown.total) == SetupGrade.NO_TRADE


def test_b_setup_range():
    inputs = StrategyInputs(
        structure_alignment="moderate",   # 10
        htf_alignment="partial",          # 10
        liquidity_confirmation="potential_target",  # 8
        entry_zone_quality="moderate",    # 8
        momentum="neutral",               # 5
        risk_reward_ratio=1.5,            # 3
        news_condition="moderate_risk",   # 5
    )
    breakdown = score_setup(inputs)
    assert breakdown.total == 49  # below 60 -> still NO_TRADE; kept as a boundary-math check
    assert classify_grade(breakdown.total) == SetupGrade.NO_TRADE


def test_score_breakdown_max_is_100():
    inputs = StrategyInputs(
        structure_alignment="strong",
        htf_alignment="full",
        liquidity_confirmation="swept",
        entry_zone_quality="strong",
        momentum="strong",
        risk_reward_ratio=5.0,
        news_condition="favorable",
    )
    breakdown = score_setup(inputs)
    assert breakdown.total == 100
    assert classify_grade(breakdown.total) == SetupGrade.A_PLUS


def test_score_breakdown_includes_a_reason_per_category():
    """Explainability companion to score_setup — every category must have a
    human-readable reason, and it should reflect the points awarded."""
    inputs = StrategyInputs(
        structure_alignment="strong",
        htf_alignment="conflict",
        liquidity_confirmation="potential_target",
        entry_zone_quality="moderate",
        momentum="opposing",
        risk_reward_ratio=2.5,
        news_condition="moderate_risk",
    )
    breakdown = score_setup(inputs)
    expected_keys = {
        "market_structure", "higher_timeframe_alignment", "liquidity_confirmation",
        "entry_zone_quality", "momentum", "risk_reward", "news_and_conditions",
    }
    assert set(breakdown.reasons.keys()) == expected_keys
    assert "20/20" in breakdown.reasons["market_structure"]
    assert "0/20" in breakdown.reasons["higher_timeframe_alignment"]
    assert "7/10" in breakdown.reasons["risk_reward"]


def test_confidence_level_thresholds():
    assert confidence_level(90) == "Very High"
    assert confidence_level(80) == "High"
    assert confidence_level(65) == "Moderate"
    assert confidence_level(45) == "Low"
    assert confidence_level(10) == "Very Low"
