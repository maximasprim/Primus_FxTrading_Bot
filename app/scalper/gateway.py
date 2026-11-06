"""Broker/market abstraction used by the scalper engine (MT5 or the simulator)."""
from __future__ import annotations

from abc import ABC, abstractmethod

import pandas as pd

from app.scalper.models import AccountSnapshot, Deal, OrderOutcome, ScalpPosition, SymbolSpec, Tick

# Orders placed by the scalper carry this magic number. The scalper only ever
# lists, modifies or closes positions with this magic -- your manual trades and
# the older execution pipeline (magic 20260812) are never touched.
SCALPER_MAGIC = 20260901
SCALPER_COMMENT = "PrimusScalper"


class ScalperGateway(ABC):
    name: str = "abstract"

    @abstractmethod
    async def account(self) -> AccountSnapshot: ...

    @abstractmethod
    async def spec(self, instrument: str) -> SymbolSpec: ...

    @abstractmethod
    async def tick(self, instrument: str) -> Tick | None: ...

    @abstractmethod
    async def candles(self, instrument: str, count: int = 120) -> pd.DataFrame:
        """1-minute candles, oldest first, columns open/high/low/close."""

    @abstractmethod
    async def positions(self) -> list[ScalpPosition]:
        """Open positions placed by the scalper only (matching SCALPER_MAGIC)."""

    @abstractmethod
    async def send_market(
        self, instrument: str, side: str, volume: float, sl: float, tp: float
    ) -> OrderOutcome: ...

    @abstractmethod
    async def close(self, ticket: str, volume: float | None = None, reason: str = "") -> OrderOutcome:
        """Close a position. `reason` is informational (used by the backtester's trade log)."""


    @abstractmethod
    async def modify(self, ticket: str, sl: float | None, tp: float | None) -> bool: ...

    @abstractmethod
    async def loss_money(self, instrument: str, side: str, volume: float, entry: float, sl: float) -> float | None:
        """Money lost if `entry`->`sl` is hit at `volume` lots (positive number), or None if unknown."""

    @abstractmethod
    async def deals_since(self, since_ts: float) -> list[Deal]:
        """Closed trades (net of commission/swap) by the scalper since `since_ts`, oldest first."""

    async def pip_value(self, instrument: str) -> float | None:
        """Account-currency value of ONE pip on 1.0 lot, as the broker computes it. None = unknown."""
        return None

    async def check_order(self, instrument: str) -> dict | None:
        """Dry-run an order with the broker WITHOUT trading (MT5's order_check). None = not supported."""
        return None

    async def aclose(self) -> None:  # optional cleanup
        return None
