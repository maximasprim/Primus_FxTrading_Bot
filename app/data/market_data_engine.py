"""
Market Data Engine (spec §5, §6).

Central point through which every other engine fetches candle data. Enforces
validation before returning data, and provides a convenience method to fetch
all timeframes at once for multi-timeframe analysis.

Includes a short-lived, MODULE-LEVEL cache -- for both successes AND
failures -- so repeated calls within a small window (e.g. a dashboard
polling every 15-30s across several instruments) don't each trigger a fresh
provider API call. This matters a lot for rate-limited providers like
Twelve Data's free tier (8 requests/minute):
  - On success: avoids re-fetching data that's still fresh.
  - On failure (e.g. 429 Too Many Requests): avoids immediately retrying
    and getting rate-limited again, which would otherwise prevent the
    provider's rate-limit window from ever clearing. Failures are
    re-raised from cache (fast, no network call) until the cooldown
    expires, at which point the next call tries the provider again for real.

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

# How long a cached SUCCESS stays fresh before we call the provider again.
_CANDLE_CACHE_TTL_SECONDS = 120.0 #60.0
_PRICE_CACHE_TTL_SECONDS = 30.0 #15.0

# How long a cached FAILURE (e.g. 429) blocks retries before we try the
# provider again. Deliberately shorter than a full rate-limit window so we
# recover promptly once it clears, but long enough that a burst of dashboard
# polls doesn't keep re-triggering the same rate limit.
_FAILURE_COOLDOWN_SECONDS = 60.0 #30.0

# Module-level (shared across every MarketDataEngine instance in this process).
# Each entry: (timestamp, value_or_exception, is_error)
_candle_cache: dict[tuple[str, str], tuple[float, object, bool]] = {}
_price_cache: dict[str, tuple[float, object, bool]] = {}


class InvalidMarketDataError(RuntimeError):
    """Raised when candle data fails validation -- callers must not proceed to analysis."""

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
    if settings.market_data_provider == "mt5":
        from app.data.providers.mt5_provider import MT5MarketDataProvider

        return MT5MarketDataProvider()
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

        Cached for _CANDLE_CACHE_TTL_SECONDS on success, or
        _FAILURE_COOLDOWN_SECONDS on failure -- see module docstring.
        """
        cache_key = (instrument, timeframe)
        cached = _candle_cache.get(cache_key)
        if cached is not None:
            cached_at, cached_value, is_error = cached
            local = get_settings().market_data_provider == "mt5"  # no rate limit -> keep data fresh
            ttl = _FAILURE_COOLDOWN_SECONDS if is_error else (3.0 if local else _CANDLE_CACHE_TTL_SECONDS)
            if time.monotonic() - cached_at < ttl:
                if is_error:
                    raise cached_value
                return cached_value

        end = datetime.now(timezone.utc)
        start = end - lookback
        try:
            df = await self.provider.get_historical_candles(instrument, timeframe, start, end)
        except Exception as exc:
            _candle_cache[cache_key] = (time.monotonic(), exc, True)
            raise

        report = validate_candles(df, timeframe)
        if not report.is_valid:
            error = InvalidMarketDataError(report)
            _candle_cache[cache_key] = (time.monotonic(), error, True)
            raise error

        _candle_cache[cache_key] = (time.monotonic(), df, False)
        return df

    async def get_multi_timeframe_snapshot(
        self,
        instrument: str,
        timeframes: list[str] | None = None,
        lookback_by_timeframe: dict[str, timedelta] | None = None,
    ) -> dict[str, pd.DataFrame]:
        """Fetch validated candles across several timeframes at once -- the entry
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
        """Cached for _PRICE_CACHE_TTL_SECONDS on success, or
        _FAILURE_COOLDOWN_SECONDS on failure -- see module docstring."""
        cached = _price_cache.get(instrument)
        if cached is not None:
            cached_at, cached_value, is_error = cached
            local = get_settings().market_data_provider == "mt5"
            ttl = _FAILURE_COOLDOWN_SECONDS if is_error else (1.0 if local else _PRICE_CACHE_TTL_SECONDS)
            if time.monotonic() - cached_at < ttl:
                if is_error:
                    raise cached_value
                return cached_value

        try:
            price = await self.provider.get_current_price(instrument)
        except Exception as exc:
            _price_cache[instrument] = (time.monotonic(), exc, True)
            raise

        _price_cache[instrument] = (time.monotonic(), price, False)
        return price