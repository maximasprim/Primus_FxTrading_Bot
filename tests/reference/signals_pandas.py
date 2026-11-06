"""
Fast entry signal for the scalper: deterministic, no I/O, no LLM, runs in
well under a millisecond on ~120 one-minute candles.

It is a *heuristic*, not a proven edge. It scores two directions (0-100) from:
  * trend alignment     EMA8 / EMA21 / EMA50 stack                     (max 30)
  * breakout            close beyond the prior 10-bar high / low       (max 25)
  * volatility burst    last-3-bar move vs ATR14 -- this is what picks
                        up news-style spikes without a calendar        (max 20)
  * RSI(7) room         momentum present but not exhausted             (max 15)
and returns the stronger side only if it clearly beats the other.
Costs (spread vs target) are enforced separately by the engine.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from app.scalper.models import SignalResult  # reference copy of the original pandas implementation (used only to prove the numpy version is identical)

MIN_BARS = 60
_MAX_RAW = 90.0
_MIN_EDGE = 12.0          # winning side must lead by this many raw points
_MIN_ATR_TO_SPREAD = 2.0  # 1-min ATR must be at least this many x the spread, or there's no room to move


def _ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


def _atr(df: pd.DataFrame, period: int = 14) -> float:
    prev_close = df["close"].shift(1)
    tr = pd.concat(
        [df["high"] - df["low"], (df["high"] - prev_close).abs(), (df["low"] - prev_close).abs()], axis=1
    ).max(axis=1)
    return float(tr.rolling(period).mean().iloc[-1])


def _rsi(series: pd.Series, period: int = 7) -> float:
    delta = series.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean()
    last_gain, last_loss = float(gain.iloc[-1]), float(loss.iloc[-1])
    if last_loss == 0:
        return 100.0 if last_gain > 0 else 50.0
    return 100.0 - 100.0 / (1.0 + last_gain / last_loss)


def compute_signal(candles: pd.DataFrame, spread_pips: float, pip_size: float) -> SignalResult:
    if candles is None or len(candles) < MIN_BARS:
        return SignalResult(None, 0, reasons=[f"need {MIN_BARS}+ one-minute candles"])

    df = candles[["open", "high", "low", "close"]].astype(float).tail(150)
    close = df["close"]
    atr = _atr(df)
    if not np.isfinite(atr) or atr <= 0:
        return SignalResult(None, 0, reasons=["no volatility data"])
    atr_pips = atr / pip_size

    if spread_pips > 0 and atr_pips < _MIN_ATR_TO_SPREAD * spread_pips:
        return SignalResult(
            None, 0, atr_pips, [f"market too quiet: ATR {atr_pips:.1f} pips vs spread {spread_pips:.1f}"]
        )

    e8, e21, e50 = (float(_ema(close, n).iloc[-1]) for n in (8, 21, 50))
    last = float(close.iloc[-1])
    up = {"trend": 0.0, "break": 0.0, "burst": 0.0, "rsi": 0.0}
    dn = {"trend": 0.0, "break": 0.0, "burst": 0.0, "rsi": 0.0}
    why_up: list[str] = []
    why_dn: list[str] = []

    # trend stack
    if e8 > e21 > e50:
        up["trend"] = 30; why_up.append("EMA8>21>50 uptrend")
    elif e8 > e21:
        up["trend"] = 15; why_up.append("EMA8>21")
    if e8 < e21 < e50:
        dn["trend"] = 30; why_dn.append("EMA8<21<50 downtrend")
    elif e8 < e21:
        dn["trend"] = 15; why_dn.append("EMA8<21")

    # breakout of the previous 10 bars (excludes the forming bar)
    prior = df.iloc[-11:-1]
    if last > float(prior["high"].max()):
        up["break"] = 25; why_up.append("broke 10-bar high")
    if last < float(prior["low"].min()):
        dn["break"] = 25; why_dn.append("broke 10-bar low")

    # volatility burst: 3-bar displacement relative to ATR
    move = last - float(close.iloc[-4])
    ratio = abs(move) / atr
    burst = 20.0 if ratio >= 1.5 else 10.0 if ratio >= 1.0 else 0.0
    if burst:
        (up if move > 0 else dn)["burst"] = burst
        (why_up if move > 0 else why_dn).append(f"burst {ratio:.1f}x ATR")

    # RSI room
    rsi = _rsi(close)
    if 52 <= rsi <= 78:
        up["rsi"] = 15; why_up.append(f"RSI {rsi:.0f} with room")
    elif rsi > 85:
        up["rsi"] = -10; why_up.append(f"RSI {rsi:.0f} exhausted")
    if 22 <= rsi <= 48:
        dn["rsi"] = 15; why_dn.append(f"RSI {rsi:.0f} with room")
    elif rsi < 15:
        dn["rsi"] = -10; why_dn.append(f"RSI {rsi:.0f} exhausted")

    up_raw, dn_raw = max(sum(up.values()), 0.0), max(sum(dn.values()), 0.0)
    if abs(up_raw - dn_raw) < _MIN_EDGE:
        top = max(up_raw, dn_raw)
        return SignalResult(None, int(round(top / _MAX_RAW * 100)), atr_pips, ["no clear direction"])

    side, raw, reasons = ("BUY", up_raw, why_up) if up_raw > dn_raw else ("SELL", dn_raw, why_dn)
    return SignalResult(side, min(100, int(round(raw / _MAX_RAW * 100))), atr_pips, reasons)
