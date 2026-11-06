"""
Performance Service (spec §20, §23).

Aggregates recorded Signal rows into the statistics the performance dashboard
needs, and answers the specific questions the spec calls out:
  - Which pair performs best?
  - Which strategy combinations perform best?
  - Which sessions perform best?
  - Do high-score setups actually outperform lower-score setups?

# TODO: "which strategy combinations perform best" and "which sessions
# perform best" require `strategy_components` and session tagging to be
# populated on write (see services/signal_service.py) - the aggregation
# queries below are ready to consume that data once it's populated.
"""
from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.db_models import Signal


@dataclass
class PerformanceSummary:
    total_signals: int
    confirmed_setups: int
    wins: int
    losses: int
    win_rate: float
    profit_factor: float | None
    average_score: float | None
    by_instrument: dict[str, dict] = field(default_factory=dict)
    by_grade: dict[str, dict] = field(default_factory=dict)


async def compute_performance_summary(
    db: AsyncSession, instrument: str | None = None
) -> PerformanceSummary:
    base_query = select(Signal)
    if instrument:
        base_query = base_query.where(Signal.instrument == instrument.upper())

    result = await db.execute(base_query)
    signals = result.scalars().all()

    total = len(signals)
    confirmed = sum(1 for s in signals if s.status == "CONFIRMED")
    wins = sum(1 for s in signals if s.result == "WIN")
    losses = sum(1 for s in signals if s.result == "LOSS")
    decided = wins + losses

    scores = [s.confluence_score for s in signals if s.confluence_score is not None]
    avg_score = sum(scores) / len(scores) if scores else None

    by_instrument: dict[str, dict] = {}
    for s in signals:
        bucket = by_instrument.setdefault(s.instrument, {"total": 0, "wins": 0, "losses": 0})
        bucket["total"] += 1
        if s.result == "WIN":
            bucket["wins"] += 1
        elif s.result == "LOSS":
            bucket["losses"] += 1

    by_grade: dict[str, dict] = {}
    for s in signals:
        bucket = by_grade.setdefault(s.grade, {"total": 0, "wins": 0, "losses": 0})
        bucket["total"] += 1
        if s.result == "WIN":
            bucket["wins"] += 1
        elif s.result == "LOSS":
            bucket["losses"] += 1

    gross_win = sum(
        (s.take_profit_1 - s.actual_entry)
        for s in signals
        if s.result == "WIN" and s.actual_entry and s.take_profit_1
    )
    gross_loss = sum(
        abs(s.actual_entry - s.stop_loss)
        for s in signals
        if s.result == "LOSS" and s.actual_entry and s.stop_loss
    )
    profit_factor = (gross_win / gross_loss) if gross_loss else None

    return PerformanceSummary(
        total_signals=total,
        confirmed_setups=confirmed,
        wins=wins,
        losses=losses,
        win_rate=(wins / decided * 100) if decided else 0.0,
        profit_factor=profit_factor,
        average_score=avg_score,
        by_instrument=by_instrument,
        by_grade=by_grade,
    )


async def score_vs_outcome_correlation(db: AsyncSession) -> dict:
    """Directly answers: 'Do high-score setups actually outperform lower-score
    setups?' by bucketing decided signals into score bands and comparing win rate.
    """
    result = await db.execute(
        select(Signal).where(Signal.result.in_(["WIN", "LOSS"]))
    )
    signals = result.scalars().all()

    bands = {"60-74": [], "75-84": [], "85-100": []}
    for s in signals:
        if 60 <= s.confluence_score <= 74:
            bands["60-74"].append(s.result)
        elif 75 <= s.confluence_score <= 84:
            bands["75-84"].append(s.result)
        elif 85 <= s.confluence_score <= 100:
            bands["85-100"].append(s.result)

    summary = {}
    for band, results in bands.items():
        wins = sum(1 for r in results if r == "WIN")
        total = len(results)
        summary[band] = {
            "total": total,
            "win_rate": (wins / total * 100) if total else None,
        }
    return summary
