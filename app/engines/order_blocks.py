"""
Order Block Engine (spec §7E).

An order block (in the Smart Money Concepts framework) is the last opposing
candle before a strong impulsive move that breaks structure. This is a
structured price-action heuristic, not a claim of verified institutional
activity (per spec §7E: "do not treat these concepts as guaranteed
institutional behavior").
"""
from dataclasses import dataclass
from enum import Enum

import pandas as pd

from app.engines.market_structure import StructureEvent, detect_structure_events, find_swing_points


class OrderBlockType(str, Enum):
    BULLISH = "bullish"
    BEARISH = "bearish"


@dataclass
class OrderBlock:
    block_type: OrderBlockType
    top: float
    bottom: float
    formed_at: pd.Timestamp
    caused_bos_or_choch: str  # e.g. "BOS_BULLISH"
    mitigated: bool = False


def find_order_blocks(df: pd.DataFrame) -> list[OrderBlock]:
    swings = find_swing_points(df)
    events = detect_structure_events(df, swings)

    blocks: list[OrderBlock] = []
    for event in events:
        idx = df.index.get_indexer([event.timestamp], method="nearest")[0]
        if idx <= 0:
            continue

        bullish = "BULLISH" in event.kind
        # Walk backward from the break to find the last opposing candle.
        search_start = max(0, idx - 10)
        window = df.iloc[search_start:idx]
        if window.empty:
            continue

        if bullish:
            opposing_candles = window[window["close"] < window["open"]]
        else:
            opposing_candles = window[window["close"] > window["open"]]

        if opposing_candles.empty:
            continue

        ob_candle = opposing_candles.iloc[-1]
        ob_ts = opposing_candles.index[-1]

        blocks.append(
            OrderBlock(
                block_type=OrderBlockType.BULLISH if bullish else OrderBlockType.BEARISH,
                top=float(ob_candle["high"]),
                bottom=float(ob_candle["low"]),
                formed_at=ob_ts,
                caused_bos_or_choch=event.kind,
            )
        )

    _update_mitigation(blocks, df)
    return blocks


def _update_mitigation(blocks: list[OrderBlock], df: pd.DataFrame) -> None:
    for block in blocks:
        subsequent = df[df.index > block.formed_at]
        if subsequent.empty:
            continue
        if block.block_type == OrderBlockType.BULLISH:
            broke = subsequent[subsequent["close"] < block.bottom]
        else:
            broke = subsequent[subsequent["close"] > block.top]
        block.mitigated = not broke.empty
