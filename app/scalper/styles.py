"""
Entry styles for the scalper.

Every style is a pure function with the SAME signature as the original signal,
    fn(candles, spread_pips, pip_size) -> SignalResult(side, score 0-100, atr_pips, reasons)
so the engine, the backtester and its controls treat them identically. They differ in
WHAT they bet on:

  momentum        (original) trade with a move that is already underway
  breakout        trade a break out of a quiet range, hoping it keeps going
  spike_fade      trade AGAINST an over-stretched spike, hoping it snaps back
  range_reversal  trade the edges of a range back toward its middle

None of these has a proven edge. They exist so you can TEST different ideas against the
same costs and the same controls (backtester) instead of tweaking one idea's knobs.
Scores are not comparable between styles, so each style has its own suggested minimum.
"""
from __future__ import annotations

from typing import Callable

import numpy as np

from app.scalper.models import SignalResult
from app.scalper.signals import MIN_BARS, _MIN_ATR_TO_SPREAD, _WINDOW, _atr, _ema_last, _rsi, compute_signal

STYLE_INFO: dict[str, dict] = {
    "momentum": {
        "label": "Momentum (original)",
        "hint": "Joins a move that is already running (EMA trend + breakout + burst + RSI). Your backtests show it chases spikes and loses to costs.",
        "suggested_min_score": 65,
    },
    "breakout": {
        "label": "Breakout from a quiet range",
        "hint": "Waits for price to leave a 30-minute range after a squeeze, with a strong close and trend agreement. Pair it with a bigger take-profit (about 2x ATR) than stop-loss (about 1x).",
        "suggested_min_score": 60,
    },
    "spike_fade": {
        "label": "Spike fade (opposite of chasing)",
        "hint": "Bets against a sudden over-stretched move (3-minute burst of 1.5x+ ATR, extreme RSI, rejection wick) snapping back. Avoid around major news. Use a tight stop.",
        "suggested_min_score": 60,
    },
    "range_reversal": {
        "label": "Range reversal",
        "hint": "In a sideways market, sells the top and buys the bottom of the last 40-minute range, aiming back toward the middle. Does nothing in trends.",
        "suggested_min_score": 60,
    },
}


def _prep(candles, spread_pips: float, pip_size: float):
    """Shared checks. Returns (arrays, atr, atr_pips) or a SignalResult explaining why not."""
    if candles is None or len(candles) < MIN_BARS:
        return SignalResult(None, 0, reasons=[f"need {MIN_BARS}+ one-minute candles"])
    tail = candles.tail(_WINDOW)
    o = tail["open"].to_numpy(dtype=float)
    h = tail["high"].to_numpy(dtype=float)
    l = tail["low"].to_numpy(dtype=float)
    c = tail["close"].to_numpy(dtype=float)
    atr = _atr(h, l, c)
    if not np.isfinite(atr) or atr <= 0:
        return SignalResult(None, 0, reasons=["no volatility data"])
    atr_pips = atr / pip_size
    if spread_pips > 0 and atr_pips < _MIN_ATR_TO_SPREAD * spread_pips:
        return SignalResult(None, 0, atr_pips, [f"market too quiet: ATR {atr_pips:.1f} pips vs spread {spread_pips:.1f}"])
    return (o, h, l, c), atr, atr_pips


def _tr(h: np.ndarray, l: np.ndarray, c: np.ndarray) -> np.ndarray:
    prev = c[:-1]
    return np.maximum(h[1:] - l[1:], np.maximum(np.abs(h[1:] - prev), np.abs(l[1:] - prev)))


def _finish(side: str | None, raw: float, atr_pips: float, reasons: list[str], none_reason: str) -> SignalResult:
    if side is None:
        return SignalResult(None, 0, atr_pips, [none_reason])
    return SignalResult(side, int(min(100, round(raw))), atr_pips, reasons)


# ---------------------------------------------------------------------------- breakout
def breakout_signal(candles, spread_pips: float, pip_size: float) -> SignalResult:
    prep = _prep(candles, spread_pips, pip_size)
    if isinstance(prep, SignalResult):
        return prep
    (o, h, l, c), atr, atr_pips = prep
    last, op, hi_now, lo_now = c[-1], o[-1], h[-1], l[-1]
    hi_prior, lo_prior = h[-31:-1].max(), l[-31:-1].min()
    need = 0.10 * atr                                       # must clear the range by a margin, not merely touch it
    side = "BUY" if last > hi_prior + need else "SELL" if last < lo_prior - need else None
    if side is None:
        return SignalResult(None, 0, atr_pips, ["inside the 30-minute range"])
    sign = 1 if side == "BUY" else -1
    raw, why = 30.0, [f"broke the 30-minute {'high' if sign > 0 else 'low'}"]

    tr = _tr(h, l, c)
    short, long_ = tr[-6:-1].mean(), tr[-31:-1].mean()      # compression BEFORE the break
    squeeze = short / long_ if long_ > 0 else 1.0
    if squeeze < 0.8:
        raw += 25; why.append(f"squeeze before the break ({squeeze:.2f})")
    elif squeeze < 1.0:
        raw += 10; why.append(f"mild squeeze ({squeeze:.2f})")

    rng = hi_now - lo_now
    pos = (last - lo_now) / rng if rng > 0 else 0.5         # where the forming bar closes inside its own range
    if (sign > 0 and pos >= 0.75) or (sign < 0 and pos <= 0.25):
        raw += 20; why.append("closing near the extreme")

    e21, e50 = _ema_last(c, 21), _ema_last(c, 50)
    if (sign > 0 and last > e50 and e21 > e50) or (sign < 0 and last < e50 and e21 < e50):
        raw += 20; why.append("with the larger trend")
    if rng / atr >= 1.0:
        raw += 10; why.append("range expansion")
    return _finish(side, raw, atr_pips, why, "")


# ---------------------------------------------------------------------------- spike fade
def spike_fade_signal(candles, spread_pips: float, pip_size: float) -> SignalResult:
    prep = _prep(candles, spread_pips, pip_size)
    if isinstance(prep, SignalResult):
        return prep
    (o, h, l, c), atr, atr_pips = prep
    last = c[-1]
    move = last - c[-4]
    ratio = abs(move) / atr
    if ratio < 1.5:
        return SignalResult(None, 0, atr_pips, [f"no spike ({ratio:.1f}x ATR in 3 minutes, need 1.5x)"])
    up = move > 0
    side = "SELL" if up else "BUY"                          # fade it
    raw = 35.0 if ratio < 2.0 else 50.0 if ratio < 2.5 else 60.0
    why = [f"{'up' if up else 'down'} spike {ratio:.1f}x ATR in 3 minutes"]

    rsi = _rsi(c)
    if up:
        raw += 25 if rsi >= 80 else 12 if rsi >= 72 else 0
        if rsi >= 72:
            why.append(f"RSI {rsi:.0f} overbought")
    else:
        raw += 25 if rsi <= 20 else 12 if rsi <= 28 else 0
        if rsi <= 28:
            why.append(f"RSI {rsi:.0f} oversold")

    rng = h[-1] - l[-1]
    if rng > 0:
        wick = (h[-1] - max(o[-1], last)) if up else (min(o[-1], last) - l[-1])
        if wick / rng >= 0.35:
            raw += 15; why.append("rejection wick")

    stretch = abs(last - _ema_last(c, 21)) / atr
    if stretch >= 2.0:
        raw += 10; why.append(f"{stretch:.1f}x ATR from the 21-EMA")
    elif stretch >= 1.5:
        raw += 5

    e8, e21, e50 = _ema_last(c, 8), _ema_last(c, 21), _ema_last(c, 50)
    if (up and e8 > e21 > e50) or (not up and e8 < e21 < e50):
        raw -= 15; why.append("penalty: strong trend in the spike's direction")  # don't stand in front of a trend
    return _finish(side, raw, atr_pips, why, "")


# ---------------------------------------------------------------------------- range reversal
def range_reversal_signal(candles, spread_pips: float, pip_size: float) -> SignalResult:
    prep = _prep(candles, spread_pips, pip_size)
    if isinstance(prep, SignalResult):
        return prep
    (o, h, l, c), atr, atr_pips = prep
    last = c[-1]
    hi, lo = h[-41:-1].max(), l[-41:-1].min()
    width = hi - lo
    if width < 3 * atr or (spread_pips > 0 and width / pip_size < 4 * spread_pips):
        return SignalResult(None, 0, atr_pips, ["range too narrow to trade"])
    path = np.abs(np.diff(c[-41:])).sum()
    er = abs(c[-1] - c[-41]) / path if path > 0 else 1.0    # Kaufman efficiency: ~0 sideways, ~1 trending
    if er > 0.40:
        return SignalResult(None, 0, atr_pips, [f"trending, not ranging (efficiency {er:.2f})"])
    if last < lo - 0.2 * atr or last > hi + 0.2 * atr:
        return SignalResult(None, 0, atr_pips, ["price left the range (that's a breakout, not a reversal)"])

    p = (last - lo) / width
    if p <= 0.20:
        side, zone = "BUY", 30.0 if p <= 0.12 else 15.0
    elif p >= 0.80:
        side, zone = "SELL", 30.0 if p >= 0.88 else 15.0
    else:
        return SignalResult(None, 0, atr_pips, ["middle of the range"])
    raw = zone + (30.0 if er <= 0.30 else 15.0)
    why = [f"at the range {'bottom' if side == 'BUY' else 'top'} ({p:.0%})", f"sideways market (efficiency {er:.2f})"]

    rsi = _rsi(c)
    if side == "BUY":
        raw += 20 if rsi <= 32 else 10 if rsi <= 40 else 0
    else:
        raw += 20 if rsi >= 68 else 10 if rsi >= 60 else 0
    if (side == "BUY" and rsi <= 40) or (side == "SELL" and rsi >= 60):
        why.append(f"RSI {rsi:.0f}")

    rng = h[-1] - l[-1]
    if rng > 0:
        wick = (min(o[-1], last) - l[-1]) if side == "BUY" else (h[-1] - max(o[-1], last))
        if wick / rng >= 0.35:
            raw += 20; why.append("rejection wick")
    return _finish(side, raw, atr_pips, why, "")


STYLE_FUNCS: dict[str, Callable] = {
    "momentum": compute_signal,
    "breakout": breakout_signal,
    "spike_fade": spike_fade_signal,
    "range_reversal": range_reversal_signal,
}
STYLE_NAMES = tuple(STYLE_FUNCS)


def signal_for_style(style: str) -> Callable:
    try:
        return STYLE_FUNCS[style]
    except KeyError:
        raise ValueError(f"unknown entry style {style!r}; choose from {list(STYLE_FUNCS)}") from None
