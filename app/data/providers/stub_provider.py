"""
A deterministic, synthetic-data provider so the rest of the system (engines,
scoring, backtesting harness) can be developed and tested before a real
market-data subscription is wired in.

# TODO (Phase 1 completion): implement a real provider, e.g. `oanda_provider.py`,
# against OANDA's REST/streaming API (or another vendor), matching this same
# `MarketDataProvider` interface, then switch `MARKET_DATA_PROVIDER` in .env.
"""
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

from app.data.providers.base import MarketDataProvider

_TIMEFRAME_MINUTES = {
    "1M_monthly": 30 * 24 * 60,
    "1W": 7 * 24 * 60,
    "1D": 24 * 60,
    "4H": 4 * 60,
    "1H": 60,
    "15M": 15,
    "5M": 5,
    "1M": 1,
}


class StubMarketDataProvider(MarketDataProvider):
    """Generates a reproducible random-walk price series for local development."""

    def __init__(self, seed: int = 42):
        self._rng = np.random.default_rng(seed)

    async def get_historical_candles(
        self,
        instrument: str,
        timeframe: str,
        start: datetime,
        end: datetime,
    ) -> pd.DataFrame:
        minutes = _TIMEFRAME_MINUTES.get(timeframe, 5)
        periods = max(int((end - start).total_seconds() // (minutes * 60)), 2)
        timestamps = pd.date_range(start=start, periods=periods, freq=f"{minutes}min", tz="UTC")

        base_price = 1.0800 if "USD" in instrument and "XAU" not in instrument else 1950.0
        steps = self._rng.normal(0, 0.0006 * base_price, size=periods).cumsum()
        close = base_price + steps
        open_ = np.roll(close, 1)
        open_[0] = base_price
        high = np.maximum(open_, close) + np.abs(self._rng.normal(0, 0.0003 * base_price, periods))
        low = np.minimum(open_, close) - np.abs(self._rng.normal(0, 0.0003 * base_price, periods))
        volume = self._rng.integers(100, 5000, size=periods).astype(float)

        return pd.DataFrame(
            {"open": open_, "high": high, "low": low, "close": close, "volume": volume},
            index=timestamps,
        )

    async def get_latest_candle(self, instrument: str, timeframe: str) -> pd.Series:
        now = datetime.now(timezone.utc)
        df = await self.get_historical_candles(instrument, timeframe, now - timedelta(days=1), now)
        return df.iloc[-1]

    async def get_current_price(self, instrument: str) -> dict:
        candle = await self.get_latest_candle(instrument, "1M")
        spread = 0.00015 * candle["close"]
        return {
            "bid": candle["close"] - spread / 2,
            "ask": candle["close"] + spread / 2,
            "timestamp": datetime.now(timezone.utc),
        }
