"""
Liquidity Analysis Engine (spec §7D).

Detects equal highs/lows (stop clusters) and liquidity sweeps (a wick beyond
a prior high/low followed by a close back inside — a classic stop-hunt pattern).

# TODO: session-high/session-low liquidity (depends on engines/session_analysis.py
# being wired to real session boundaries) and previous-day/week high/low liquidity
# (depends on daily/weekly aggregation in the market data engine).
"""
from dataclasses import dataclass
from enum import Enum

import pandas as pd


class LiquiditySide(str, Enum):
    BUY_SIDE = "buy_side"   # resting above equal highs
    SELL_SIDE = "sell_side"  # resting below equal lows


@dataclass
class LiquidityPool:
    side: LiquiditySide
    price: float
    touches: list[pd.Timestamp]

    @property
    def strength(self) -> int:
        """More touches = more resting orders = a stronger liquidity pool."""
        return len(self.touches)


@dataclass
class LiquiditySweep:
    side: LiquiditySide
    swept_level: float
    sweep_timestamp: pd.Timestamp
    closed_back_inside: bool


def find_equal_levels(df: pd.DataFrame, tolerance_pct: float = 0.0005) -> list[LiquidityPool]:
    """Cluster swing highs/lows that sit within `tolerance_pct` of each other —
    a simple stand-in for genuine equal-highs/equal-lows detection.
    """
    from app.engines.market_structure import find_swing_points, SwingType

    swings = find_swing_points(df)
    pools: list[LiquidityPool] = []

    highs = sorted((s for s in swings if s.kind == SwingType.HIGH), key=lambda s: s.price)
    lows = sorted((s for s in swings if s.kind == SwingType.LOW), key=lambda s: s.price)

    def cluster(swing_list, side: LiquiditySide) -> list[LiquidityPool]:
        clustered: list[LiquidityPool] = []
        used = set()
        for i, swing in enumerate(swing_list):
            if i in used:
                continue
            group = [swing]
            used.add(i)
            for j in range(i + 1, len(swing_list)):
                if j in used:
                    continue
                if abs(swing_list[j].price - swing.price) / swing.price <= tolerance_pct:
                    group.append(swing_list[j])
                    used.add(j)
            if len(group) >= 2:
                avg_price = sum(g.price for g in group) / len(group)
                clustered.append(
                    LiquidityPool(side=side, price=avg_price, touches=[g.timestamp for g in group])
                )
        return clustered

    pools.extend(cluster(highs, LiquiditySide.BUY_SIDE))
    pools.extend(cluster(lows, LiquiditySide.SELL_SIDE))
    return pools


def detect_liquidity_sweeps(df: pd.DataFrame, pools: list[LiquidityPool]) -> list[LiquiditySweep]:
    """A sweep = a candle wicks beyond the pool level but closes back inside it."""
    sweeps: list[LiquiditySweep] = []

    for pool in pools:
        for ts, row in df.iterrows():
            if pool.side == LiquiditySide.BUY_SIDE:
                wicked_through = row["high"] > pool.price
                closed_back_inside = row["close"] < pool.price
            else:
                wicked_through = row["low"] < pool.price
                closed_back_inside = row["close"] > pool.price

            if wicked_through:
                sweeps.append(
                    LiquiditySweep(
                        side=pool.side,
                        swept_level=pool.price,
                        sweep_timestamp=ts,
                        closed_back_inside=closed_back_inside,
                    )
                )
                break  # only record the first sweep of this pool

    return sweeps
