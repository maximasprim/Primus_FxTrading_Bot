"""
Twelve Data market data provider.

# IMPORTANT -- READ BEFORE RELYING ON THIS FOR LIVE DECISIONS
Written against Twelve Data's published REST API docs (api.twelvedata.com),
but NOT tested against a live account -- verify each method yourself with a
real API key before trusting it for analysis. Two things worth knowing
going in:

  1. RATE LIMITS: the free "Basic" plan is limited to 8 requests/minute.
     A single multi-timeframe snapshot (5 timeframes across however many
     instruments you're watching) can burn through that fast -- the market
     data engine doesn't currently throttle or cache calls. If you see
     HTTP 429 errors, that's why.
  2. BID/ASK: Twelve Data's forex feed does not expose real dealer bid/ask
     spread on the free tier -- get_current_price() below synthesizes an
     approximate spread around the single reported price. Don't treat that
     spread as accurate for real execution decisions.

API docs: https://twelvedata.com/docs
"""
from datetime import datetime, timezone

import httpx
import pandas as pd

from app.config import get_settings
from app.data.providers.base import MarketDataProvider

_TIMEFRAME_TO_TD_INTERVAL = {
    "1D": "1day",
    "1W": "1week",
    "4H": "4h",
    "1H": "1h",
    "15M": "15min",
    "5M": "5min",
    "1M": "1min",
}

_BASE_URL = "https://api.twelvedata.com"


class TwelveDataProvider(MarketDataProvider):
    def __init__(self, api_key: str | None = None):
        settings = get_settings()
        self.api_key = api_key or settings.market_data_api_key
        if not self.api_key:
            raise ValueError(
                "TwelveDataProvider requires an API key. Set MARKET_DATA_API_KEY in .env "
                "and MARKET_DATA_PROVIDER=twelvedata."
            )

    async def get_historical_candles(
        self, instrument: str, timeframe: str, start: datetime, end: datetime
    ) -> pd.DataFrame:
        interval = _TIMEFRAME_TO_TD_INTERVAL.get(timeframe)
        if interval is None:
            raise ValueError(f"Unsupported timeframe for Twelve Data: {timeframe}")

        params = {
            "symbol": instrument,  # Twelve Data uses "EUR/USD" directly -- matches our internal format
            "interval": interval,
            "start_date": start.strftime("%Y-%m-%d %H:%M:%S"),
            "end_date": end.strftime("%Y-%m-%d %H:%M:%S"),
            "order": "ASC",
            "apikey": self.api_key,
        }
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(f"{_BASE_URL}/time_series", params=params)
            response.raise_for_status()
            data = response.json()

        if data.get("status") == "error":
            raise RuntimeError(f"Twelve Data error for {instrument}: {data.get('message')}")

        values = data.get("values", [])
        if not values:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

        rows = [
            {
                "timestamp": pd.Timestamp(v["datetime"]),
                "open": float(v["open"]),
                "high": float(v["high"]),
                "low": float(v["low"]),
                "close": float(v["close"]),
                "volume": float(v.get("volume") or 0),
            }
            for v in values
        ]
        df = pd.DataFrame(rows).set_index("timestamp")
        if df.index.tz is None:
            df.index = df.index.tz_localize("UTC")
        return df

    async def get_latest_candle(self, instrument: str, timeframe: str) -> pd.Series:
        interval = _TIMEFRAME_TO_TD_INTERVAL.get(timeframe)
        if interval is None:
            raise ValueError(f"Unsupported timeframe for Twelve Data: {timeframe}")

        params = {"symbol": instrument, "interval": interval, "outputsize": 1, "apikey": self.api_key}
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(f"{_BASE_URL}/time_series", params=params)
            response.raise_for_status()
            data = response.json()

        if data.get("status") == "error":
            raise RuntimeError(f"Twelve Data error for {instrument}: {data.get('message')}")

        values = data.get("values", [])
        if not values:
            raise RuntimeError(f"No candle data returned for {instrument} at {timeframe}")

        v = values[0]
        return pd.Series(
            {
                "open": float(v["open"]),
                "high": float(v["high"]),
                "low": float(v["low"]),
                "close": float(v["close"]),
                "volume": float(v.get("volume") or 0),
            },
            name=pd.Timestamp(v["datetime"]),
        )

    async def get_current_price(self, instrument: str) -> dict:
        params = {"symbol": instrument, "apikey": self.api_key}
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(f"{_BASE_URL}/price", params=params)
            response.raise_for_status()
            data = response.json()

        if "price" not in data:
            raise RuntimeError(f"Twelve Data error for {instrument}: {data}")

        mid = float(data["price"])
        synthetic_spread = mid * 0.00015
        return {
            "bid": mid - synthetic_spread / 2,
            "ask": mid + synthetic_spread / 2,
            "timestamp": datetime.now(timezone.utc),
        }