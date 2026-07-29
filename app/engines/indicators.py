"""
Technical Indicator Engine (spec §7H).

Wraps pandas-ta to compute EMAs, RSI, MACD, ATR, and ADX, plus simple
divergence detection. These are confirmation tools only — per the spec,
indicators must not override strong market structure.
"""
from dataclasses import dataclass

import pandas as pd
import pandas_ta as ta


@dataclass
class IndicatorSnapshot:
    ema_20: float
    ema_50: float
    ema_100: float
    ema_200: float
    rsi_14: float
    macd: float
    macd_signal: float
    macd_hist: float
    atr_14: float
    adx_14: float

    @property
    def trend_stack_bullish(self) -> bool:
        """20 > 50 > 100 > 200 EMA — classic bullish stack."""
        return self.ema_20 > self.ema_50 > self.ema_100 > self.ema_200

    @property
    def trend_stack_bearish(self) -> bool:
        return self.ema_20 < self.ema_50 < self.ema_100 < self.ema_200

    @property
    def is_overbought(self) -> bool:
        return self.rsi_14 >= 70

    @property
    def is_oversold(self) -> bool:
        return self.rsi_14 <= 30

    @property
    def is_trending(self) -> bool:
        """ADX > 25 is a common threshold for 'trending' vs. weak/ranging."""
        return self.adx_14 > 25


def compute_indicators(df: pd.DataFrame) -> IndicatorSnapshot:
    working = df.copy()
    working["ema_20"] = ta.ema(working["close"], length=20)
    working["ema_50"] = ta.ema(working["close"], length=50)
    working["ema_100"] = ta.ema(working["close"], length=100)
    working["ema_200"] = ta.ema(working["close"], length=200)

    working["rsi_14"] = ta.rsi(working["close"], length=14)

    macd_df = ta.macd(working["close"])
    working = working.join(macd_df)

    working["atr_14"] = ta.atr(working["high"], working["low"], working["close"], length=14)

    adx_df = ta.adx(working["high"], working["low"], working["close"], length=14)
    working = working.join(adx_df)

    latest = working.iloc[-1]
    macd_cols = [c for c in working.columns if c.startswith("MACD_")]
    macds_cols = [c for c in working.columns if c.startswith("MACDs_")]
    macdh_cols = [c for c in working.columns if c.startswith("MACDh_")]
    adx_cols = [c for c in working.columns if c.startswith("ADX_")]

    return IndicatorSnapshot(
        ema_20=float(latest["ema_20"]),
        ema_50=float(latest["ema_50"]),
        ema_100=float(latest["ema_100"]),
        ema_200=float(latest["ema_200"]),
        rsi_14=float(latest["rsi_14"]),
        macd=float(latest[macd_cols[0]]) if macd_cols else float("nan"),
        macd_signal=float(latest[macds_cols[0]]) if macds_cols else float("nan"),
        macd_hist=float(latest[macdh_cols[0]]) if macdh_cols else float("nan"),
        atr_14=float(latest["atr_14"]),
        adx_14=float(latest[adx_cols[0]]) if adx_cols else float("nan"),
    )


def detect_rsi_divergence(df: pd.DataFrame, lookback: int = 30) -> str | None:
    """Very simple divergence check: compares the last two significant swing
    highs/lows in price vs. RSI over the lookback window.
    Returns "bullish", "bearish", or None. Refine with proper swing detection
    (see engines/market_structure.py) for production use.
    """
    window = df.tail(lookback).copy()
    window["rsi_14"] = ta.rsi(df["close"], length=14).tail(lookback)

    price_high_idx = window["high"].idxmax()
    price_low_idx = window["low"].idxmin()

    if price_high_idx == window.index[-1] and window.loc[price_high_idx, "rsi_14"] < window["rsi_14"].max():
        return "bearish"
    if price_low_idx == window.index[-1] and window.loc[price_low_idx, "rsi_14"] > window["rsi_14"].min():
        return "bullish"
    return None
