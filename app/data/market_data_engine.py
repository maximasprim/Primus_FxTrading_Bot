"""
Market Data Engine (spec §5, §6).

Central point through which every other engine fetches candle data. Enforces
validation before returning data, and provides a convenience method to fetch
all timeframes at once for multi-timeframe analysis.

Includes a short-lived, MODULE-LEVEL cache so repeated calls within a small
window (e.g. a dashboard polling every 15-30s across several instruments)
don't each trigger a fresh provider API call -- this matters a lot for
rate-limited providers like Twelve Data's free tier (8 requests/minute).
The cache is module-level (not on the MarketDataEngine instance) on purpose:
every route/service in this codebase does `MarketDataEngine()` fresh per
call, so an instance-level cache would be created and discarded every
single request and never actually cache anything across requests.
# NOTE: still per-process, in-memory only -- fine for a single backend
# worker, not shared across multiple worker processes. If you scale to
# multiple workers later, this would need to move to Redis.
"""
import time
from datetime import datetime, timedelta, timezone

import pandas as pd

from app.config import get_settings
from app.data.providers.base import MarketDataProvider
from app.data.providers.stub_provider import StubMarketDataProvider
from app.data.validators import DataQualityReport, validate_candles

ALL_TIMEFRAMES = ["1D", "4H", "1H", "15M", "5M"]
HIGHER_TIMEFRAMES = ["1W", "1D"]
INTERMEDIATE_TIMEFRAMES = ["4H", "1H"]
ENTRY_TIMEFRAMES = ["15M", "5M"]

# How long a cached result stays fresh before we call the provider again.
_CANDLE_CACHE_TTL_SECONDS = 60.0
_PRICE_CACHE_TTL_SECONDS = 15.0

# Module-level (shared across every MarketDataEngine instance in this
# process) -- every route/service does `MarketDataEngine()` fresh per call,
# so this can't live on `self` or it'd be wiped every request.
_candle_cache: dict[tuple[str, str], tuple[float, pd.DataFrame]] = {}
_price_cache: dict[str, tuple[float, dict]] = {}


class InvalidMarketDataError(RuntimeError):
    """Raised when candle data fails validation — callers must not proceed to analysis."""

    def __init__(self, report: DataQualityReport):
        self.report = report
        super().__init__(f"Market data failed validation: {report.issues}")


def _get_provider() -> MarketDataProvider:
    settings = get_settings()
    if settings.market_data_provider == "stub":
        return StubMarketDataProvider()
    if settings.market_data_provider == "oanda":
        from app.data.providers.oanda_provider import OandaProvider

        return OandaProvider(
            api_key=settings.market_data_api_key,
            account_id=settings.oanda_account_id,
            environment=settings.oanda_environment,
        )
    if settings.market_data_provider == "twelvedata":
        from app.data.providers.twelvedata_provider import TwelveDataProvider

        return TwelveDataProvider(api_key=settings.market_data_api_key)
    # TODO: register additional providers here as they're implemented.
    raise ValueError(f"Unknown market data provider: {settings.market_data_provider}")


class MarketDataEngine:
    def __init__(self, provider: MarketDataProvider | None = None):
        self.provider = provider or _get_provider()

    async def get_validated_candles(
        self,
        instrument: str,
        timeframe: str,
        lookback: timedelta,
    ) -> pd.DataFrame:
        """Fetch candles and raise InvalidMarketDataError if they fail validation.
        Callers (engines, signal generation) must never bypass this.

        Cached for _CANDLE_CACHE_TTL_SECONDS per (instrument, timeframe) --
        repeated calls within that window return the cached result instead
        of hitting the provider again.
        """
        cache_key = (instrument, timeframe)
        cached = _candle_cache.get(cache_key)
        if cached is not None:
            cached_at, cached_df = cached
            if time.monotonic() - cached_at < _CANDLE_CACHE_TTL_SECONDS:
                return cached_df

        end = datetime.now(timezone.utc)
        start = end - lookback
        df = await self.provider.get_historical_candles(instrument, timeframe, start, end)

        report = validate_candles(df, timeframe)
        if not report.is_valid:
            raise InvalidMarketDataError(report)

        _candle_cache[cache_key] = (time.monotonic(), df)
        return df

    async def get_multi_timeframe_snapshot(
        self,
        instrument: str,
        timeframes: list[str] | None = None,
        lookback_by_timeframe: dict[str, timedelta] | None = None,
    ) -> dict[str, pd.DataFrame]:
        """Fetch validated candles across several timeframes at once — the entry
        point for the top-down multi-timeframe approach in spec §6.
        """
        timeframes = timeframes or ALL_TIMEFRAMES
        default_lookback = {
            "1W": timedelta(weeks=104),
            "1D": timedelta(days=365),
            "4H": timedelta(days=90),
            "1H": timedelta(days=30),
            "15M": timedelta(days=10),
            "5M": timedelta(days=3),
            "1M": timedelta(hours=12),
        }
        lookback_by_timeframe = lookback_by_timeframe or {}

        snapshot: dict[str, pd.DataFrame] = {}
        for tf in timeframes:
            lookback = lookback_by_timeframe.get(tf, default_lookback.get(tf, timedelta(days=30)))
            snapshot[tf] = await self.get_validated_candles(instrument, tf, lookback)
        return snapshot

    async def get_current_price(self, instrument: str) -> dict:
        """Cached for _PRICE_CACHE_TTL_SECONDS per instrument."""
        cached = _price_cache.get(instrument)
        if cached is not None:
            cached_at, cached_price = cached
            if time.monotonic() - cached_at < _PRICE_CACHE_TTL_SECONDS:
                return cached_price

        price = await self.provider.get_current_price(instrument)
        _price_cache[instrument] = (time.monotonic(), price)
        return price
# """
# Market Data Engine (spec §5, §6).

# Central point through which every other engine fetches candle data. Enforces
# validation before returning data, and provides a convenience method to fetch
# all timeframes at once for multi-timeframe analysis.
# """
# import time
# from datetime import datetime, timedelta, timezone

# import pandas as pd

# from app.config import get_settings
# from app.data.providers.base import MarketDataProvider
# from app.data.providers.stub_provider import StubMarketDataProvider
# from app.data.validators import DataQualityReport, validate_candles

# ALL_TIMEFRAMES = ["1D", "4H", "1H", "15M", "5M"]
# HIGHER_TIMEFRAMES = ["1W", "1D"]
# INTERMEDIATE_TIMEFRAMES = ["4H", "1H"]
# ENTRY_TIMEFRAMES = ["15M", "5M"]


# class InvalidMarketDataError(RuntimeError):
#     """Raised when candle data fails validation — callers must not proceed to analysis."""

#     def __init__(self, report: DataQualityReport):
#         self.report = report
#         super().__init__(f"Market data failed validation: {report.issues}")


# def _get_provider() -> MarketDataProvider:
#     settings = get_settings()
#     if settings.market_data_provider == "stub":
#         return StubMarketDataProvider()
#     if settings.market_data_provider == "oanda":
#         from app.data.providers.oanda_provider import OandaProvider
    
#         return OandaProvider(
#             api_key=settings.market_data_api_key,
#             account_id=settings.oanda_account_id,
#             environment=settings.oanda_environment,
#         )
#     if settings.market_data_provider == "twelvedata":
#         from app.data.providers.twelvedata_provider import TwelveDataProvider

#         return TwelveDataProvider(api_key=settings.market_data_api_key)
#     # TODO: register additional providers here as they're implemented.
#     raise ValueError(f"Unknown market data provider: {settings.market_data_provider}")


# class MarketDataEngine:
#     def __init__(self, provider: MarketDataProvider | None = None):
#         self.provider = provider or _get_provider()

#     async def get_validated_candles(
#         self,
#         instrument: str,
#         timeframe: str,
#         lookback: timedelta,
#     ) -> pd.DataFrame:
#         """Fetch candles and raise InvalidMarketDataError if they fail validation.
#         Callers (engines, signal generation) must never bypass this.
#         """
#         end = datetime.now(timezone.utc)
#         start = end - lookback
#         df = await self.provider.get_historical_candles(instrument, timeframe, start, end)

#         report = validate_candles(df, timeframe)
#         if not report.is_valid:
#             raise InvalidMarketDataError(report)

#         return df

#     async def get_multi_timeframe_snapshot(
#         self,
#         instrument: str,
#         timeframes: list[str] | None = None,
#         lookback_by_timeframe: dict[str, timedelta] | None = None,
#     ) -> dict[str, pd.DataFrame]:
#         """Fetch validated candles across several timeframes at once — the entry
#         point for the top-down multi-timeframe approach in spec §6.
#         """
#         timeframes = timeframes or ALL_TIMEFRAMES
#         default_lookback = {
#             "1W": timedelta(weeks=104),
#             "1D": timedelta(days=365),
#             "4H": timedelta(days=90),
#             "1H": timedelta(days=30),
#             "15M": timedelta(days=10),
#             "5M": timedelta(days=3),
#             "1M": timedelta(hours=12),
#         }
#         lookback_by_timeframe = lookback_by_timeframe or {}

#         snapshot: dict[str, pd.DataFrame] = {}
#         for tf in timeframes:
#             lookback = lookback_by_timeframe.get(tf, default_lookback.get(tf, timedelta(days=30)))
#             snapshot[tf] = await self.get_validated_candles(instrument, tf, lookback)
#         return snapshot

#     async def get_current_price(self, instrument: str) -> dict:
#         return await self.provider.get_current_price(instrument)
