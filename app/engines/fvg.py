"""
Fair Value Gap (FVG) Engine (spec §7F).

A bullish FVG: candle[i-1].high < candle[i+1].low (a gap the market left behind).
A bearish FVG: candle[i-1].low > candle[i+1].high.

Tracks mitigation status (fresh / partially mitigated / fully mitigated) as
subsequent price action returns into the gap.
"""
from dataclasses import dataclass
from enum import Enum

import pandas as pd


class FVGDirection(str, Enum):
    BULLISH = "bullish"
    BEARISH = "bearish"


class MitigationStatus(str, Enum):
    FRESH = "fresh"
    PARTIALLY_MITIGATED = "partially_mitigated"
    FULLY_MITIGATED = "fully_mitigated"


@dataclass
class FairValueGap:
    direction: FVGDirection
    top: float
    bottom: float
    formed_at: pd.Timestamp
    status: MitigationStatus = MitigationStatus.FRESH
    mitigated_at: pd.Timestamp | None = None

    @property
    def midpoint(self) -> float:
        return (self.top + self.bottom) / 2


def detect_fvgs(df: pd.DataFrame) -> list[FairValueGap]:
    gaps: list[FairValueGap] = []

    for i in range(1, len(df) - 1):
        prev_candle = df.iloc[i - 1]
        next_candle = df.iloc[i + 1]

        if prev_candle["high"] < next_candle["low"]:
            gaps.append(
                FairValueGap(
                    direction=FVGDirection.BULLISH,
                    top=float(next_candle["low"]),
                    bottom=float(prev_candle["high"]),
                    formed_at=df.index[i],
                )
            )
        elif prev_candle["low"] > next_candle["high"]:
            gaps.append(
                FairValueGap(
                    direction=FVGDirection.BEARISH,
                    top=float(prev_candle["low"]),
                    bottom=float(next_candle["high"]),
                    formed_at=df.index[i],
                )
            )

    _update_mitigation(gaps, df)
    return gaps


def _update_mitigation(gaps: list[FairValueGap], df: pd.DataFrame) -> None:
    for gap in gaps:
        subsequent = df[df.index > gap.formed_at]
        if subsequent.empty:
            continue

        if gap.direction == FVGDirection.BULLISH:
            touched = subsequent[subsequent["low"] <= gap.top]
            fully = subsequent[subsequent["low"] <= gap.bottom]
        else:
            touched = subsequent[subsequent["high"] >= gap.bottom]
            fully = subsequent[subsequent["high"] >= gap.top]

        if not fully.empty:
            gap.status = MitigationStatus.FULLY_MITIGATED
            gap.mitigated_at = fully.index[0]
        elif not touched.empty:
            gap.status = MitigationStatus.PARTIALLY_MITIGATED
            gap.mitigated_at = touched.index[0]


def fresh_fvgs(gaps: list[FairValueGap]) -> list[FairValueGap]:
    return [g for g in gaps if g.status == MitigationStatus.FRESH]


def is_price_in_fvg(price: float, gap: FairValueGap) -> bool:
    return gap.bottom <= price <= gap.top
