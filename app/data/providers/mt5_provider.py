"""
MetaTrader 5 as the market-data provider (set MARKET_DATA_PROVIDER=mt5).

Reads candles and prices straight from the desktop terminal on the same
(Windows) machine: no HTTP, no API key, no rate limit -- this is what replaces
the Twelve Data 429s. Requires the MT5 terminal to be open and logged in.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone

import pandas as pd

from app.config import get_settings
from app.data.providers.base import MarketDataProvider
from app.mt5.client import MT5Client, get_mt5_client

_TF_CONST = {"1M": "TIMEFRAME_M1", "5M": "TIMEFRAME_M5", "15M": "TIMEFRAME_M15",
             "1H": "TIMEFRAME_H1", "4H": "TIMEFRAME_H4", "1D": "TIMEFRAME_D1", "1W": "TIMEFRAME_W1"}
_TF_MINUTES = {"1M": 1, "5M": 5, "15M": 15, "1H": 60, "4H": 240, "1D": 1440, "1W": 10080}


class MT5MarketDataProvider(MarketDataProvider):
    def __init__(self, client: MT5Client | None = None, symbol_suffix: str | None = None):
        self.client = client or get_mt5_client()
        self.suffix = get_settings().mt5_symbol_suffix if symbol_suffix is None else symbol_suffix

    def _symbol(self, instrument: str) -> str:
        return instrument.replace("/", "") + self.suffix

    async def _frame(self, rates, symbol: str) -> pd.DataFrame:
        if rates is None or len(rates) == 0:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        df = pd.DataFrame(rates)
        offset = await self.client.server_offset_seconds(symbol)
        df.index = pd.to_datetime(df["time"] - offset, unit="s", utc=True)
        df.index.name = "timestamp"
        df["volume"] = df["tick_volume"].astype(float)
        return df[["open", "high", "low", "close", "volume"]]

    async def get_historical_candles(self, instrument: str, timeframe: str, start: datetime, end: datetime) -> pd.DataFrame:
        const = _TF_CONST.get(timeframe)
        if const is None:
            raise ValueError(f"Unsupported timeframe for MT5: {timeframe}")
        symbol = self._symbol(instrument)
        await self.client.call("symbol_select", symbol, True)
        span_minutes = max((end - start).total_seconds() / 60.0, 1.0)
        count = min(int(math.ceil(span_minutes / _TF_MINUTES[timeframe])) + 5, 50_000)
        rates = await self.client.call("copy_rates_from_pos", symbol, self.client.const(const), 0, count)
        if rates is None:
            raise RuntimeError(f"MT5 returned no candles for {symbol}: {await self.client.last_error()}")
        df = await self._frame(rates, symbol)
        start_utc = start if start.tzinfo else start.replace(tzinfo=timezone.utc)
        return df[df.index >= pd.Timestamp(start_utc)]

    async def get_latest_candle(self, instrument: str, timeframe: str) -> pd.Series:
        const = _TF_CONST.get(timeframe)
        if const is None:
            raise ValueError(f"Unsupported timeframe for MT5: {timeframe}")
        symbol = self._symbol(instrument)
        rates = await self.client.call("copy_rates_from_pos", symbol, self.client.const(const), 0, 1)
        df = await self._frame(rates, symbol)
        if df.empty:
            raise RuntimeError(f"No candle data returned for {symbol} at {timeframe}")
        return df.iloc[-1].rename(df.index[-1])

    async def get_current_price(self, instrument: str) -> dict:
        symbol = self._symbol(instrument)
        tick = await self.client.call("symbol_info_tick", symbol)
        if tick is None:
            raise RuntimeError(f"No tick for {symbol}: {await self.client.last_error()}")
        offset = await self.client.server_offset_seconds(symbol)
        return {"bid": float(tick.bid), "ask": float(tick.ask),
                "timestamp": datetime.fromtimestamp(float(tick.time) - offset, tz=timezone.utc)}
