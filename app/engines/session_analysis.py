"""
Session Analysis Engine (spec §8).

Defines standard session windows in UTC and provides session-range and
session-liquidity helpers. Adjust boundaries for DST as needed in production
(this uses fixed UTC hours for simplicity).
"""
from dataclasses import dataclass
from datetime import time
from enum import Enum

import pandas as pd


class Session(str, Enum):
    ASIAN = "asian"
    LONDON = "london"
    NEW_YORK = "new_york"
    LONDON_NY_OVERLAP = "london_ny_overlap"
    CLOSED = "closed"


# UTC session windows (approximate; does not account for DST shifts)
_SESSION_WINDOWS: dict[Session, tuple[time, time]] = {
    Session.ASIAN: (time(0, 0), time(8, 0)),
    Session.LONDON: (time(8, 0), time(16, 0)),
    Session.NEW_YORK: (time(13, 0), time(21, 0)),
    Session.LONDON_NY_OVERLAP: (time(13, 0), time(16, 0)),
}


@dataclass
class SessionRange:
    session: Session
    high: float
    low: float
    start: pd.Timestamp
    end: pd.Timestamp


def current_session(now_utc: pd.Timestamp) -> Session:
    t = now_utc.time()
    if _SESSION_WINDOWS[Session.LONDON_NY_OVERLAP][0] <= t < _SESSION_WINDOWS[Session.LONDON_NY_OVERLAP][1]:
        return Session.LONDON_NY_OVERLAP
    if _SESSION_WINDOWS[Session.LONDON][0] <= t < _SESSION_WINDOWS[Session.LONDON][1]:
        return Session.LONDON
    if _SESSION_WINDOWS[Session.NEW_YORK][0] <= t < _SESSION_WINDOWS[Session.NEW_YORK][1]:
        return Session.NEW_YORK
    if _SESSION_WINDOWS[Session.ASIAN][0] <= t < _SESSION_WINDOWS[Session.ASIAN][1]:
        return Session.ASIAN
    return Session.CLOSED


def session_range(df: pd.DataFrame, session: Session, on_date: pd.Timestamp) -> SessionRange | None:
    """Compute the high/low formed during a given session on a given date.
    `df` should be intraday data (e.g. 5M/15M) covering that date.
    """
    if session not in _SESSION_WINDOWS:
        return None
    start_t, end_t = _SESSION_WINDOWS[session]
    start = pd.Timestamp.combine(on_date.date(), start_t, tz="UTC")
    end = pd.Timestamp.combine(on_date.date(), end_t, tz="UTC")

    window = df[(df.index >= start) & (df.index < end)]
    if window.empty:
        return None

    return SessionRange(
        session=session, high=float(window["high"].max()), low=float(window["low"].min()),
        start=start, end=end,
    )
