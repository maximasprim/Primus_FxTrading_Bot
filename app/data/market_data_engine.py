"""
Market Data Engine (spec §5, §6).

Central point through which every other engine fetches candle data. Enforces
validation before returning data, and provides a convenience method to fetch
all timeframes at once for multi-timeframe analysis.
"""
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
        """
        end = datetime.now(timezone.utc)
        start = end - lookback
        df = await self.provider.get_historical_candles(instrument, timeframe, start, end)

        report = validate_candles(df, timeframe)
        if not report.is_valid:
            raise InvalidMarketDataError(report)

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
        return await self.provider.get_current_price(instrument)
