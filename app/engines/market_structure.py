"""
Market Structure Engine (spec §7A).

Detects swing highs/lows, classifies HH/HL/LH/LL sequences, and flags
Break of Structure (BOS) / Change of Character (CHoCH). This is a working
first implementation - refine swing detection (e.g. fractal width, ATR-based
filtering) as real data comes in.
"""
from dataclasses import dataclass
from enum import Enum

import numpy as np
import pandas as pd

from app.models.schemas import MarketBias


class SwingType(str, Enum):
    HIGH = "swing_high"
    LOW = "swing_low"


@dataclass
class SwingPoint:
    timestamp: pd.Timestamp
    price: float
    kind: SwingType


@dataclass
class StructureEvent:
    kind: str  # "BOS_BULLISH" | "BOS_BEARISH" | "CHOCH_BULLISH" | "CHOCH_BEARISH"
    timestamp: pd.Timestamp
    broken_level: float


@dataclass
class MarketStructureResult:
    bias: MarketBias
    swings: list[SwingPoint]
    events: list[StructureEvent]

    @property
    def latest_event(self) -> StructureEvent | None:
        return self.events[-1] if self.events else None


def find_swing_points(df: pd.DataFrame, left: int = 3, right: int = 3) -> list[SwingPoint]:
    """Fractal-style swing detection: a swing high/low is a local extreme
    with `left`/`right` bars of lower/higher highs (or higher/lower lows)
    on either side.
    """
    highs = df["high"].to_numpy()
    lows = df["low"].to_numpy()
    swings: list[SwingPoint] = []

    for i in range(left, len(df) - right):
        window_high = highs[i - left : i + right + 1]
        if highs[i] == window_high.max() and np.argmax(window_high) == left:
            swings.append(SwingPoint(df.index[i], float(highs[i]), SwingType.HIGH))

        window_low = lows[i - left : i + right + 1]
        if lows[i] == window_low.min() and np.argmin(window_low) == left:
            swings.append(SwingPoint(df.index[i], float(lows[i]), SwingType.LOW))

    swings.sort(key=lambda s: s.timestamp)
    return swings


def classify_structure(swings: list[SwingPoint]) -> MarketBias:
    """Classify overall bias from the sequence of recent swing highs/lows."""
    highs = [s for s in swings if s.kind == SwingType.HIGH][-3:]
    lows = [s for s in swings if s.kind == SwingType.LOW][-3:]

    if len(highs) < 2 or len(lows) < 2:
        return MarketBias.UNCLEAR

    higher_highs = highs[-1].price > highs[-2].price
    higher_lows = lows[-1].price > lows[-2].price
    lower_highs = highs[-1].price < highs[-2].price
    lower_lows = lows[-1].price < lows[-2].price

    if higher_highs and higher_lows:
        return MarketBias.STRONG_BULLISH
    if lower_highs and lower_lows:
        return MarketBias.STRONG_BEARISH
    if higher_lows and not higher_highs:
        return MarketBias.WEAK_BULLISH
    if lower_highs and not lower_lows:
        return MarketBias.WEAK_BEARISH
    return MarketBias.RANGING


def detect_structure_events(df: pd.DataFrame, swings: list[SwingPoint]) -> list[StructureEvent]:
    """Detect Break of Structure (continuation) and Change of Character (reversal)
    by watching for closes beyond the most recent opposing swing point.
    """
    events: list[StructureEvent] = []
    recent_high: SwingPoint | None = None
    recent_low: SwingPoint | None = None
    prevailing_bias: str | None = None

    swing_iter = iter(swings)
    next_swing = next(swing_iter, None)

    for ts, row in df.iterrows():
        while next_swing is not None and next_swing.timestamp <= ts:
            if next_swing.kind == SwingType.HIGH:
                recent_high = next_swing
            else:
                recent_low = next_swing
            next_swing = next(swing_iter, None)

        close = row["close"]
        if recent_high is not None and close > recent_high.price:
            kind = "BOS_BULLISH" if prevailing_bias in (None, "bullish") else "CHOCH_BULLISH"
            events.append(StructureEvent(kind, ts, recent_high.price))
            prevailing_bias = "bullish"
            recent_high = None  # level broken, wait for a new one

        elif recent_low is not None and close < recent_low.price:
            kind = "BOS_BEARISH" if prevailing_bias in (None, "bearish") else "CHOCH_BEARISH"
            events.append(StructureEvent(kind, ts, recent_low.price))
            prevailing_bias = "bearish"
            recent_low = None

    return events


def analyze_market_structure(df: pd.DataFrame) -> MarketStructureResult:
    swings = find_swing_points(df)
    bias = classify_structure(swings)
    events = detect_structure_events(df, swings)
    return MarketStructureResult(bias=bias, swings=swings, events=events)
