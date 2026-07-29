"""
Market data validation (spec §5).

The system must never generate a live signal using stale or invalid market data —
these checks are meant to be called before any candle DataFrame is handed to the
analysis engines.
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import pandas as pd

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


@dataclass
class DataQualityReport:
    is_valid: bool
    issues: list[str] = field(default_factory=list)

    def add(self, issue: str) -> None:
        self.issues.append(issue)
        self.is_valid = False


def validate_candles(
    df: pd.DataFrame,
    timeframe: str,
    max_staleness_multiplier: float = 3.0,
) -> DataQualityReport:
    """Run the checks called out in spec §5: freshness, missing/duplicate candles,
    invalid prices, data gaps, timezone consistency.
    """
    report = DataQualityReport(is_valid=True)

    if df.empty:
        report.add("DataFrame is empty.")
        return report

    required_cols = {"open", "high", "low", "close"}
    missing_cols = required_cols - set(df.columns)
    if missing_cols:
        report.add(f"Missing required columns: {sorted(missing_cols)}")
        return report

    # Timezone consistency
    if df.index.tz is None:
        report.add("Index is not timezone-aware; expected UTC-localized timestamps.")

    # Duplicate candles
    duplicate_count = int(df.index.duplicated().sum())
    if duplicate_count:
        report.add(f"Found {duplicate_count} duplicate timestamp(s).")

    # Invalid prices
    invalid_price_mask = (
        (df["low"] > df["high"])
        | (df[["open", "high", "low", "close"]] <= 0).any(axis=1)
        | df[["open", "high", "low", "close"]].isna().any(axis=1)
    )
    invalid_count = int(invalid_price_mask.sum())
    if invalid_count:
        report.add(f"Found {invalid_count} candle(s) with invalid OHLC values.")

    # Missing candles / gaps
    minutes = _TIMEFRAME_MINUTES.get(timeframe)
    if minutes and len(df) > 1:
        expected_delta = timedelta(minutes=minutes)
        actual_deltas = df.index.to_series().diff().dropna()
        gap_count = int((actual_deltas > expected_delta * 1.5).sum())
        if gap_count:
            report.add(f"Found {gap_count} gap(s) larger than 1.5x the expected interval.")

    # Freshness
    if minutes:
        now = datetime.now(timezone.utc)
        last_ts = df.index[-1]
        if last_ts.tzinfo is None:
            last_ts = last_ts.tz_localize("UTC")
        staleness = now - last_ts
        max_allowed = timedelta(minutes=minutes * max_staleness_multiplier)
        if staleness > max_allowed:
            report.add(
                f"Latest candle is stale: {staleness} old, "
                f"max allowed is {max_allowed} for timeframe {timeframe}."
            )

    return report
