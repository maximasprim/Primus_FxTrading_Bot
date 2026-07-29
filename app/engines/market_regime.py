"""
Market Regime Detection Engine (spec §10).

Classifies the current environment using ADX (trend strength) and ATR
percentile (volatility) as primary signals, combined with the market
structure bias. Different downstream strategies should be weighted
differently depending on the regime returned here.
"""
import numpy as np
import pandas as pd
import pandas_ta as ta

from app.models.schemas import MarketRegime


def detect_regime(df: pd.DataFrame, atr_lookback: int = 100) -> MarketRegime:
    adx_df = ta.adx(df["high"], df["low"], df["close"], length=14)
    adx_col = [c for c in adx_df.columns if c.startswith("ADX_")][0]
    current_adx = float(adx_df[adx_col].iloc[-1])

    atr = ta.atr(df["high"], df["low"], df["close"], length=14)
    recent_atr = atr.tail(atr_lookback).dropna()
    if recent_atr.empty:
        current_atr_percentile = 50.0
    else:
        current_atr_percentile = float(
            (recent_atr <= recent_atr.iloc[-1]).mean() * 100
        )

    # Simple breakout check: current close outside the recent N-bar range.
    recent_range = df.iloc[-21:-1]
    breakout = (
        not recent_range.empty
        and (df["close"].iloc[-1] > recent_range["high"].max() or df["close"].iloc[-1] < recent_range["low"].min())
    )

    if breakout:
        return MarketRegime.BREAKOUT
    if current_atr_percentile >= 80:
        return MarketRegime.HIGH_VOLATILITY
    if current_atr_percentile <= 20:
        return MarketRegime.LOW_VOLATILITY
    if current_adx >= 25:
        return MarketRegime.STRONG_TREND
    if current_adx >= 15:
        return MarketRegime.WEAK_TREND
    return MarketRegime.RANGE
