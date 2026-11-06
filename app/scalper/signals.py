"""
Fast entry signal for the scalper: deterministic, no I/O, no LLM. Pure numpy,
~20x faster than the first pandas version (identical output -- see
tests/test_scalper_signals_equivalence.py), which matters for backtests that
call it hundreds of thousands of times.

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

from app.scalper.models import SignalResult

MIN_BARS = 60
_WINDOW = 150
_MAX_RAW = 90.0
_MIN_EDGE = 12.0          # winning side must lead by this many raw points
_MIN_ATR_TO_SPREAD = 2.0  # 1-min ATR must be at least this many x the spread, or there's no room to move

_weights_cache: dict[tuple[int, float], np.ndarray] = {}


def _ema_weights(n: int, alpha: float) -> np.ndarray:
    """Weights w so that (w . x) is the LAST value of an adjust=False EMA over n points."""
    key = (n, alpha)
    w = _weights_cache.get(key)
    if w is None:
        k = np.arange(n - 1, -1, -1, dtype=float)  # n-1 ... 0
        w = alpha * (1.0 - alpha) ** k
        w[0] = (1.0 - alpha) ** (n - 1)
        _weights_cache[key] = w
    return w


def _ema_last(x: np.ndarray, span: int) -> float:
    return float(_ema_weights(len(x), 2.0 / (span + 1.0)) @ x)


def _atr(high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int = 14) -> float:
    prev = close[:-1]
    tr = np.maximum(high[1:] - low[1:], np.maximum(np.abs(high[1:] - prev), np.abs(low[1:] - prev)))
    return float(tr[-period:].mean())


def _rsi(close: np.ndarray, period: int = 7) -> float:
    delta = np.diff(close)
    alpha = 1.0 / period
    gain = float(_ema_weights(len(delta), alpha) @ np.clip(delta, 0.0, None))
    loss = float(_ema_weights(len(delta), alpha) @ np.clip(-delta, 0.0, None))
    if loss == 0:
        return 100.0 if gain > 0 else 50.0
    return 100.0 - 100.0 / (1.0 + gain / loss)


def compute_signal(candles, spread_pips: float, pip_size: float) -> SignalResult:
    if candles is None or len(candles) < MIN_BARS:
        return SignalResult(None, 0, reasons=[f"need {MIN_BARS}+ one-minute candles"])

    tail = candles.tail(_WINDOW)
    high = tail["high"].to_numpy(dtype=float)
    low = tail["low"].to_numpy(dtype=float)
    close = tail["close"].to_numpy(dtype=float)

    atr = _atr(high, low, close)
    if not np.isfinite(atr) or atr <= 0:
        return SignalResult(None, 0, reasons=["no volatility data"])
    atr_pips = atr / pip_size

    if spread_pips > 0 and atr_pips < _MIN_ATR_TO_SPREAD * spread_pips:
        return SignalResult(
            None, 0, atr_pips, [f"market too quiet: ATR {atr_pips:.1f} pips vs spread {spread_pips:.1f}"]
        )

    e8, e21, e50 = (_ema_last(close, n) for n in (8, 21, 50))
    last = float(close[-1])
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
    if last > float(high[-11:-1].max()):
        up["break"] = 25; why_up.append("broke 10-bar high")
    if last < float(low[-11:-1].min()):
        dn["break"] = 25; why_dn.append("broke 10-bar low")

    # volatility burst: 3-bar displacement relative to ATR
    move = last - float(close[-4])
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
