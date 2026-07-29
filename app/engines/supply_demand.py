"""
Supply & Demand Zone Engine (spec §7C).

Identifies zones from strong directional departures (a "base" of consolidation
followed by an impulsive move away from it), and tracks how many times a zone
has been retested.

# TODO: this first pass uses a simple ATR-relative impulse threshold to find
# departures. For production, consider: base-candle-count filtering, volume
# confirmation if available, and confluence tagging with order blocks (see
# order_blocks.py) and FVGs (see fvg.py) as the spec calls for.
"""
from dataclasses import dataclass
from enum import Enum

import pandas as pd
import pandas_ta as ta


class ZoneType(str, Enum):
    SUPPLY = "supply"
    DEMAND = "demand"


class ZoneState(str, Enum):
    FRESH = "fresh"
    TESTED = "tested"
    MITIGATED = "mitigated"


@dataclass
class SupplyDemandZone:
    zone_type: ZoneType
    top: float
    bottom: float
    formed_at: pd.Timestamp
    departure_strength: float  # move size in units of ATR at formation
    test_count: int = 0
    state: ZoneState = ZoneState.FRESH


def find_zones(df: pd.DataFrame, impulse_atr_multiple: float = 2.0) -> list[SupplyDemandZone]:
    atr = ta.atr(df["high"], df["low"], df["close"], length=14)
    zones: list[SupplyDemandZone] = []

    for i in range(14, len(df) - 1):
        candle_range = df["high"].iloc[i] - df["low"].iloc[i]
        current_atr = atr.iloc[i]
        if pd.isna(current_atr) or current_atr == 0:
            continue

        body_move = df["close"].iloc[i] - df["open"].iloc[i]
        strength = abs(body_move) / current_atr

        if strength >= impulse_atr_multiple:
            base_candle = df.iloc[i - 1]
            if body_move > 0:
                zones.append(
                    SupplyDemandZone(
                        zone_type=ZoneType.DEMAND,
                        top=float(base_candle["high"]),
                        bottom=float(base_candle["low"]),
                        formed_at=df.index[i - 1],
                        departure_strength=float(strength),
                    )
                )
            else:
                zones.append(
                    SupplyDemandZone(
                        zone_type=ZoneType.SUPPLY,
                        top=float(base_candle["high"]),
                        bottom=float(base_candle["low"]),
                        formed_at=df.index[i - 1],
                        departure_strength=float(strength),
                    )
                )

    _update_zone_state(zones, df)
    return zones


def _update_zone_state(zones: list[SupplyDemandZone], df: pd.DataFrame) -> None:
    for zone in zones:
        subsequent = df[df.index > zone.formed_at]
        if subsequent.empty:
            continue

        touches = subsequent[(subsequent["low"] <= zone.top) & (subsequent["high"] >= zone.bottom)]
        zone.test_count = len(touches)

        if zone.zone_type == ZoneType.DEMAND:
            broke_through = subsequent[subsequent["close"] < zone.bottom]
        else:
            broke_through = subsequent[subsequent["close"] > zone.top]

        if not broke_through.empty:
            zone.state = ZoneState.MITIGATED
        elif zone.test_count > 0:
            zone.state = ZoneState.TESTED


def freshest_zone(zones: list[SupplyDemandZone], zone_type: ZoneType) -> SupplyDemandZone | None:
    candidates = [z for z in zones if z.zone_type == zone_type and z.state == ZoneState.FRESH]
    if not candidates:
        return None
    return max(candidates, key=lambda z: z.formed_at)
