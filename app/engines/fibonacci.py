"""
Fibonacci Engine (spec §7G) - a confluence tool only, never a standalone trigger.
"""
from dataclasses import dataclass

LEVELS = (0.382, 0.5, 0.618, 0.705, 0.786)


@dataclass
class FibonacciRetracement:
    swing_start: float
    swing_end: float
    levels: dict[float, float]

    def nearest_level(self, price: float) -> tuple[float, float]:
        """Return (level_ratio, level_price) closest to the given price."""
        return min(self.levels.items(), key=lambda kv: abs(kv[1] - price))

    def is_price_near_level(self, price: float, tolerance_pct: float = 0.001) -> bool:
        _, level_price = self.nearest_level(price)
        return abs(price - level_price) / level_price <= tolerance_pct


def compute_retracement(swing_start: float, swing_end: float) -> FibonacciRetracement:
    """swing_start -> swing_end defines the impulsive leg; retracement levels
    are computed back from swing_end toward swing_start.
    """
    diff = swing_end - swing_start
    levels = {level: swing_end - diff * level for level in LEVELS}
    return FibonacciRetracement(swing_start=swing_start, swing_end=swing_end, levels=levels)


def is_discount_zone(price: float, swing_start: float, swing_end: float) -> bool:
    """Discount = below the 50% level of a bullish leg (cheap to buy)."""
    midpoint = (swing_start + swing_end) / 2
    return price < midpoint if swing_end > swing_start else price > midpoint


def is_premium_zone(price: float, swing_start: float, swing_end: float) -> bool:
    return not is_discount_zone(price, swing_start, swing_end)
