"""
Abstract provider interface. Implement this for each real data source
(OANDA, a broker API, a data vendor, etc.) so providers can be swapped
without touching the rest of the system (spec §24: "modular provider
architecture so data providers can be replaced").
"""
from abc import ABC, abstractmethod
from datetime import datetime

import pandas as pd


class MarketDataProvider(ABC):
    """Contract every real-time/historical data source must satisfy."""

    @abstractmethod
    async def get_historical_candles(
        self,
        instrument: str,
        timeframe: str,
        start: datetime,
        end: datetime,
    ) -> pd.DataFrame:
        """Return a DataFrame indexed by UTC timestamp with columns:
        open, high, low, close, volume, (optionally bid, ask).
        """
        raise NotImplementedError

    @abstractmethod
    async def get_latest_candle(self, instrument: str, timeframe: str) -> pd.Series:
        """Return the most recent (possibly still-forming) candle."""
        raise NotImplementedError

    @abstractmethod
    async def get_current_price(self, instrument: str) -> dict:
        """Return {'bid': float, 'ask': float, 'timestamp': datetime}."""
        raise NotImplementedError
