"""
Forex/XAU market open/closed status.

Forex (and XAU/USD, traded like forex via CFDs) runs ~24 hours a day from
Sunday 17:00 through Friday 17:00, America/New_York time -- that's the
convention brokers use since it lines up with the interbank session
handover, and using a real IANA timezone here means DST transitions are
handled automatically rather than hardcoding a UTC offset that would be
wrong for half the year.

This is informational only (the dashboard's market-status indicator) --
it does not gate order placement. Holiday handling is intentionally
simple: a fixed MM-DD list from settings.market_holidays_mmdd. Broker
holiday calendars vary and shift year to year, so treat the default list
as a starting point to edit, not a complete/accurate calendar.
"""
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from app.config import get_settings

NY_TZ = ZoneInfo("America/New_York")


@dataclass
class MarketStatus:
    open: bool
    reason: str | None
    ny_time: str
    holidays: list[str]


def get_market_status(now: datetime | None = None) -> MarketStatus:
    settings = get_settings()
    now_ny = (now or datetime.now(NY_TZ)).astimezone(NY_TZ)
    weekday = now_ny.weekday()  # Monday=0 ... Sunday=6
    hour = now_ny.hour

    is_weekend_closed = (
        weekday == 5  # Saturday: always closed
        or (weekday == 6 and hour < 17)  # Sunday before 17:00 ET: still closed
        or (weekday == 4 and hour >= 17)  # Friday from 17:00 ET: closed for the weekend
    )

    holidays = sorted({d.strip() for d in settings.market_holidays_mmdd.split(",") if d.strip()})
    mmdd = now_ny.strftime("%m-%d")

    if mmdd in holidays:
        return MarketStatus(open=False, reason=f"Holiday closure ({mmdd})", ny_time=now_ny.isoformat(), holidays=holidays)
    if is_weekend_closed:
        return MarketStatus(
            open=False,
            reason="Weekend - forex/XAU markets are closed Friday 17:00 to Sunday 17:00 (America/New_York)",
            ny_time=now_ny.isoformat(),
            holidays=holidays,
        )
    return MarketStatus(open=True, reason=None, ny_time=now_ny.isoformat(), holidays=holidays)
