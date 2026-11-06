"""
Economic News & Fundamental Risk Engine (spec §9).

Blocks (or flags) new signal generation around high-impact news events for
the currencies involved in an instrument. Requires wiring a real economic
calendar provider - this module defines the interface and blocking logic;
`fetch_upcoming_news` is a stub.

# TODO: wire a real economic calendar (e.g. ForexFactory-style feed, Trading
# Economics API, or a paid calendar API) into `fetch_upcoming_news`.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta

from app.config import get_settings
from app.models.schemas import NewsEvent

_INSTRUMENT_CURRENCIES = {
    "EUR/USD": ["EUR", "USD"],
    "GBP/USD": ["GBP", "USD"],
    "XAU/USD": ["USD"],  # gold is heavily USD-sensitive; add "XAU"-specific events if available
    "USD/JPY": ["USD", "JPY"],
    "USD/CHF": ["USD", "CHF"],
    "AUD/USD": ["AUD", "USD"],
    "NZD/USD": ["NZD", "USD"],
    "USD/CAD": ["USD", "CAD"],
    "XAG/USD": ["USD"],
}


@dataclass
class NewsRiskAssessment:
    is_blocked: bool
    reason: str | None
    relevant_events: list[NewsEvent]


async def fetch_upcoming_news(currencies: list[str], window: timedelta) -> list[NewsEvent]:
    """# TODO: replace with a real economic calendar provider call.
    Currently returns an empty list so the rest of the pipeline is exercisable.
    """
    return []


async def assess_news_risk(instrument: str, at_time: datetime | None = None) -> NewsRiskAssessment:
    settings = get_settings()
    at_time = at_time or datetime.utcnow()
    currencies = _INSTRUMENT_CURRENCIES.get(instrument, [])

    window = timedelta(
        minutes=max(settings.news_block_minutes_before, settings.news_block_minutes_after)
    )
    events = await fetch_upcoming_news(currencies, window)

    for event in events:
        if event.impact.lower() != "high":
            continue
        before_block = at_time <= event.event_time <= at_time + timedelta(
            minutes=settings.news_block_minutes_before
        )
        after_block = event.event_time <= at_time <= event.event_time + timedelta(
            minutes=settings.news_block_minutes_after
        )
        if before_block or after_block:
            return NewsRiskAssessment(
                is_blocked=True,
                reason=f"Signal blocked due to high-impact economic event: {event.title} ({event.currency}).",
                relevant_events=events,
            )

    return NewsRiskAssessment(is_blocked=False, reason=None, relevant_events=events)
