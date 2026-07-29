"""
OANDA v20 REST API market data provider.

# IMPORTANT -- READ BEFORE USING IN PRODUCTION
# This provider is written against OANDA's published v20 REST API
# documentation, but has NOT been tested against a live OANDA account --
# this sandbox's network egress is restricted to package registries
# (pypi/npm/github), not third-party APIs like oanda.com. Before relying on
# this for real trading decisions:
#   1. Get a free OANDA practice/demo account and API token.
#   2. Set MARKET_DATA_PROVIDER=oanda, MARKET_DATA_API_KEY=<your token>,
#      and OANDA_ACCOUNT_ID / OANDA_ENVIRONMENT in your .env.
#   3. Smoke-test each method below individually (a short script calling
#      get_current_price() first is the fastest sanity check) before wiring
#      it into the full analysis pipeline.
#   4. Watch for: rate limits (OANDA throttles aggressively), instrument
#      name mapping (OANDA uses "EUR_USD" not "EUR/USD" -- see
#      _to_oanda_instrument below), and candle "completeness" (OANDA marks
#      in-progress candles with `"complete": false` -- this provider filters
#      those out of get_historical_candles but keeps the final one in
#      get_latest_candle deliberately, since callers may want the live bar).

API docs: https://developer.oanda.com/rest-live-v20/introduction/
"""
from datetime import datetime

import httpx
import pandas as pd

from app.config import get_settings
from app.data.providers.base import MarketDataProvider

_TIMEFRAME_TO_OANDA_GRANULARITY = {
    "1D": "D",
    "4H": "H4",
    "1H": "H1",
    "15M": "M15",
    "5M": "M5",
    "1M": "M1",
    "1W": "W",
}


def _to_oanda_instrument(instrument: str) -> str:
    """'EUR/USD' -> 'EUR_USD'"""
    return instrument.replace("/", "_").upper()


class OandaProvider(MarketDataProvider):
    def __init__(self, api_key: str | None = None, account_id: str | None = None, environment: str = "practice"):
        settings = get_settings()
        self.api_key = api_key or settings.market_data_api_key
        self.account_id = account_id or getattr(settings, "oanda_account_id", "")
        # "practice" (demo) vs "live" -- deliberately defaults to practice so
        # a misconfigured deployment doesn't accidentally hit a live account.
        base = "api-fxpractice.oanda.com" if environment == "practice" else "api-fxtrade.oanda.com"
        self.base_url = f"https://{base}/v3"
        if not self.api_key:
            raise ValueError(
                "OandaProvider requires an API key. Set MARKET_DATA_API_KEY in .env "
                "and MARKET_DATA_PROVIDER=oanda."
            )

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

    async def get_historical_candles(
        self, instrument: str, timeframe: str, start: datetime, end: datetime
    ) -> pd.DataFrame:
        granularity = _TIMEFRAME_TO_OANDA_GRANULARITY.get(timeframe)
        if granularity is None:
            raise ValueError(f"Unsupported timeframe for OANDA: {timeframe}")

        url = f"{self.base_url}/instruments/{_to_oanda_instrument(instrument)}/candles"
        params = {
            "granularity": granularity,
            "from": start.isoformat() + "Z" if start.tzinfo is None else start.isoformat(),
            "to": end.isoformat() + "Z" if end.tzinfo is None else end.isoformat(),
            "price": "M",  # midpoint candles; use "B"/"A" for bid/ask separately if needed
        }
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(url, headers=self._headers(), params=params)
            response.raise_for_status()
            data = response.json()

        rows = []
        for candle in data.get("candles", []):
            if not candle.get("complete", False):
                continue  # skip the still-forming candle for historical queries
            mid = candle["mid"]
            rows.append(
                {
                    "timestamp": pd.Timestamp(candle["time"]),
                    "open": float(mid["o"]),
                    "high": float(mid["h"]),
                    "low": float(mid["l"]),
                    "close": float(mid["c"]),
                    "volume": float(candle.get("volume", 0)),
                }
            )

        if not rows:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

        df = pd.DataFrame(rows).set_index("timestamp")
        if df.index.tz is None:
            df.index = df.index.tz_localize("UTC")
        return df

    async def get_latest_candle(self, instrument: str, timeframe: str) -> pd.Series:
        granularity = _TIMEFRAME_TO_OANDA_GRANULARITY.get(timeframe)
        if granularity is None:
            raise ValueError(f"Unsupported timeframe for OANDA: {timeframe}")

        url = f"{self.base_url}/instruments/{_to_oanda_instrument(instrument)}/candles"
        params = {"granularity": granularity, "count": 1, "price": "M"}
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(url, headers=self._headers(), params=params)
            response.raise_for_status()
            data = response.json()

        candles = data.get("candles", [])
        if not candles:
            raise RuntimeError(f"No candle data returned for {instrument} at {timeframe}")
        candle = candles[-1]  # deliberately kept even if still-forming -- see module docstring
        mid = candle["mid"]
        return pd.Series(
            {
                "open": float(mid["o"]),
                "high": float(mid["h"]),
                "low": float(mid["l"]),
                "close": float(mid["c"]),
                "volume": float(candle.get("volume", 0)),
            },
            name=pd.Timestamp(candle["time"]),
        )

    async def get_current_price(self, instrument: str) -> dict:
        if not self.account_id:
            raise ValueError("OandaProvider.get_current_price requires OANDA_ACCOUNT_ID to be set.")

        url = f"{self.base_url}/accounts/{self.account_id}/pricing"
        params = {"instruments": _to_oanda_instrument(instrument)}
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(url, headers=self._headers(), params=params)
            response.raise_for_status()
            data = response.json()

        prices = data.get("prices", [])
        if not prices:
            raise RuntimeError(f"No pricing data returned for {instrument}")
        price = prices[0]
        return {
            "bid": float(price["bids"][0]["price"]),
            "ask": float(price["asks"][0]["price"]),
            "timestamp": pd.Timestamp(price["time"]).to_pydatetime(),
        }
