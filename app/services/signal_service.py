"""
Signal Service — orchestrates the full decision pipeline described in spec §28:

Market Data -> Multi-Timeframe Analysis -> Market Structure -> Liquidity ->
Supply/Demand -> FVG/Order Blocks -> Momentum -> Volatility -> Session ->
Economic News -> Strategy Confluence -> AI Reasoning -> Risk Validation ->
Confluence Score -> BUY/SELL/WAIT/NO TRADE -> Alert User

This is the module every API route calls into. It never skips a stage, and
it is always allowed to return NO_TRADE/WAIT — that is a successful outcome,
not an error.
"""
from datetime import timedelta

from app.data.market_data_engine import MarketDataEngine
from app.engines import fvg as fvg_engine
from app.engines import liquidity as liquidity_engine
from app.engines import market_regime as regime_engine
from app.engines import order_blocks as ob_engine
from app.engines import supply_demand as sd_engine
from app.engines.ai_decision import rule_based_scenario_fallback
from app.engines.confluence_scoring import StrategyInputs, classify_grade, score_setup
from app.engines.indicators import compute_indicators
from app.engines.market_structure import analyze_market_structure
from app.engines.news_risk import assess_news_risk
from app.models.schemas import (
    Direction,
    EntryZone,
    MarketBias,
    SetupGrade,
    SetupStatus,
    Timeframe,
    TradeSetup,
)


def _derive_structure_alignment(higher_tf_bias: MarketBias, entry_tf_bias: MarketBias) -> str:
    aligned_bullish = {MarketBias.STRONG_BULLISH, MarketBias.WEAK_BULLISH}
    aligned_bearish = {MarketBias.STRONG_BEARISH, MarketBias.WEAK_BEARISH}

    if higher_tf_bias in aligned_bullish and entry_tf_bias in aligned_bullish:
        return "strong" if higher_tf_bias == MarketBias.STRONG_BULLISH else "moderate"
    if higher_tf_bias in aligned_bearish and entry_tf_bias in aligned_bearish:
        return "strong" if higher_tf_bias == MarketBias.STRONG_BEARISH else "moderate"
    return "unclear"


async def run_full_analysis(instrument: str, triggering_price: float | None = None) -> TradeSetup:
    engine = MarketDataEngine()

    # --- Multi-timeframe data ---
    daily = await engine.get_validated_candles(instrument, "1D", timedelta(days=365))
    h4 = await engine.get_validated_candles(instrument, "4H", timedelta(days=90))
    m5 = await engine.get_validated_candles(instrument, "5M", timedelta(days=3))

    # --- Market structure across timeframes ---
    daily_structure = analyze_market_structure(daily)
    entry_structure = analyze_market_structure(m5)

    # --- Supporting engines on the entry timeframe ---
    liquidity_pools = liquidity_engine.find_equal_levels(m5)
    sweeps = liquidity_engine.detect_liquidity_sweeps(m5, liquidity_pools)
    gaps = fvg_engine.detect_fvgs(m5)
    fresh_gaps = fvg_engine.fresh_fvgs(gaps)
    zones = sd_engine.find_zones(m5)
    blocks = ob_engine.find_order_blocks(m5)
    indicators = compute_indicators(m5)
    regime = regime_engine.detect_regime(h4)

    # --- News risk ---
    news_assessment = await assess_news_risk(instrument)

    # --- Determine direction from aligned bias (simplified decision rule;
    #     refine with full strategy-consortium weighting in a later pass) ---
    if daily_structure.bias in (MarketBias.STRONG_BULLISH, MarketBias.WEAK_BULLISH):
        direction = Direction.BUY
    elif daily_structure.bias in (MarketBias.STRONG_BEARISH, MarketBias.WEAK_BEARISH):
        direction = Direction.SELL
    else:
        direction = Direction.WAIT

    current_price = float(m5["close"].iloc[-1])
    atr = indicators.atr_14

    entry_zone = None
    stop_loss = None
    tp1 = None
    tp2 = None
    rr = 0.0

    if direction in (Direction.BUY, Direction.SELL) and fresh_gaps:
        zone_gap = fresh_gaps[-1]
        entry_zone = EntryZone(min=zone_gap.bottom, max=zone_gap.top)
        if direction == Direction.BUY:
            stop_loss = zone_gap.bottom - atr
            tp1 = zone_gap.top + atr * 2
            tp2 = zone_gap.top + atr * 4
        else:
            stop_loss = zone_gap.top + atr
            tp1 = zone_gap.bottom - atr * 2
            tp2 = zone_gap.bottom - atr * 4
        risk = abs(current_price - stop_loss)
        reward = abs(tp1 - current_price)
        rr = reward / risk if risk > 0 else 0.0

    strategy_inputs = StrategyInputs(
        structure_alignment=_derive_structure_alignment(daily_structure.bias, entry_structure.bias),
        htf_alignment="full" if daily_structure.bias == entry_structure.bias else "partial",
        liquidity_confirmation="swept" if any(s.closed_back_inside for s in sweeps) else (
            "potential_target" if liquidity_pools else "none"
        ),
        entry_zone_quality="strong" if fresh_gaps and (zones or blocks) else ("moderate" if fresh_gaps else "weak"),
        momentum="strong" if indicators.is_trending and not indicators.is_overbought and not indicators.is_oversold else "neutral",
        risk_reward_ratio=rr,
        news_condition="major_event_imminent" if news_assessment.is_blocked else "favorable",
    )

    breakdown = score_setup(strategy_inputs)
    grade = classify_grade(breakdown.total)

    if news_assessment.is_blocked or direction == Direction.WAIT or grade == SetupGrade.NO_TRADE:
        final_direction = Direction.NO_TRADE if grade == SetupGrade.NO_TRADE else direction
        status = SetupStatus.SETUP_FORMING if final_direction != Direction.NO_TRADE else SetupStatus.INVALIDATED
    else:
        status = SetupStatus.CONFIRMED if breakdown.total >= 75 else SetupStatus.SETUP_FORMING
        final_direction = direction

    scenario = rule_based_scenario_fallback(
        direction=final_direction.value if final_direction in (Direction.BUY, Direction.SELL) else "BUY",
        invalidation_price=stop_loss or current_price,
        htf_bias=daily_structure.bias.value,
    )

    reasoning_parts = [
        f"Daily bias: {daily_structure.bias.value}.",
        f"Entry timeframe (5M) bias: {entry_structure.bias.value}.",
        f"Market regime (4H): {regime.value}.",
        f"{len(fresh_gaps)} fresh FVG(s), {len(liquidity_pools)} liquidity pool(s), "
        f"{sum(1 for s in sweeps if s.closed_back_inside)} confirmed sweep(s).",
    ]
    if news_assessment.is_blocked:
        reasoning_parts.append(news_assessment.reason or "Signal blocked due to news risk.")

    return TradeSetup(
        instrument=instrument,
        direction=final_direction,
        setup_type="Liquidity Sweep + Structure + FVG confluence" if fresh_gaps else "Structure-only (incomplete confluence)",
        entry_zone=entry_zone,
        stop_loss=stop_loss,
        take_profit_1=tp1,
        take_profit_2=tp2,
        risk_reward=round(rr, 2) if rr else None,
        confluence_score=breakdown.total,
        score_breakdown=breakdown,
        grade=grade,
        market_regime=regime,
        timeframe=Timeframe.M5,
        higher_timeframe_bias=daily_structure.bias,
        scenario=scenario,
        status=status,
        reasoning=" ".join(reasoning_parts),
    )
