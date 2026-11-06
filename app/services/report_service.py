"""
Daily & Weekly Trading Reports (notification-feature spec §13).

Builds the report content from the trade journal + performance service.
Delivery (as a DAILY_SUMMARY / WEEKLY_REPORT notification event) is the
caller's responsibility - see app/api/routes/notifications.py for the
trigger endpoints, or wire a scheduled Celery task to call these directly.
"""
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.trading_mode import trading_mode_manager
from app.models.db_models import Signal, TradeJournalEntry


@dataclass
class DailyReport:
    date: str
    signals_generated: int
    a_plus_setups: int
    trades_executed: int
    winning_trades: int
    losing_trades: int
    net_result_r: float
    best_instrument: str | None
    most_effective_strategy: str | None
    auto_trading_status: str


@dataclass
class WeeklyReport:
    week_start: str
    week_end: str
    total_signals: int
    confirmed_setups: int
    executed_trades: int
    win_rate: float
    profit_factor: float | None
    average_r: float | None
    max_drawdown_pct: float | None
    by_instrument: dict[str, dict] = field(default_factory=dict)
    by_session: dict[str, dict] = field(default_factory=dict)
    by_strategy: dict[str, dict] = field(default_factory=dict)


async def generate_daily_report(db: AsyncSession, on_date: datetime | None = None) -> DailyReport:
    on_date = on_date or datetime.utcnow()
    day_start = on_date.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + timedelta(days=1)

    signals_result = await db.execute(
        select(Signal).where(Signal.timestamp >= day_start, Signal.timestamp < day_end)
    )
    signals = signals_result.scalars().all()

    trades_result = await db.execute(
        select(TradeJournalEntry).where(
            TradeJournalEntry.timestamp >= day_start, TradeJournalEntry.timestamp < day_end
        )
    )
    trades = trades_result.scalars().all()

    wins = sum(1 for t in trades if t.result == "WIN")
    losses = sum(1 for t in trades if t.result == "LOSS")
    net_r = sum(t.r_multiple for t in trades if t.r_multiple is not None)

    instrument_counts = Counter(t.instrument for t in trades if t.result == "WIN")
    best_instrument = instrument_counts.most_common(1)[0][0] if instrument_counts else None

    strategy_counts = Counter(t.strategy_combination for t in trades if t.strategy_combination)
    most_effective_strategy = strategy_counts.most_common(1)[0][0] if strategy_counts else None

    return DailyReport(
        date=day_start.strftime("%d %B %Y"),
        signals_generated=len(signals),
        a_plus_setups=sum(1 for s in signals if s.grade == "A+"),
        trades_executed=len(trades),
        winning_trades=wins,
        losing_trades=losses,
        net_result_r=round(net_r, 2),
        best_instrument=best_instrument,
        most_effective_strategy=most_effective_strategy,
        auto_trading_status="Active" if trading_mode_manager.state.auto_trading_enabled else "Inactive",
    )


async def generate_weekly_report(db: AsyncSession, week_ending: datetime | None = None) -> WeeklyReport:
    week_ending = week_ending or datetime.utcnow()
    week_start = week_ending - timedelta(days=7)

    signals_result = await db.execute(
        select(Signal).where(Signal.timestamp >= week_start, Signal.timestamp < week_ending)
    )
    signals = signals_result.scalars().all()

    trades_result = await db.execute(
        select(TradeJournalEntry).where(
            TradeJournalEntry.timestamp >= week_start, TradeJournalEntry.timestamp < week_ending
        )
    )
    trades = trades_result.scalars().all()

    decided = [t for t in trades if t.result in ("WIN", "LOSS")]
    wins = sum(1 for t in decided if t.result == "WIN")
    win_rate = (wins / len(decided) * 100) if decided else 0.0

    gross_win = sum(t.profit_loss for t in decided if t.result == "WIN" and t.profit_loss)
    gross_loss = sum(abs(t.profit_loss) for t in decided if t.result == "LOSS" and t.profit_loss)
    profit_factor = (gross_win / gross_loss) if gross_loss else None

    r_multiples = [t.r_multiple for t in decided if t.r_multiple is not None]
    average_r = (sum(r_multiples) / len(r_multiples)) if r_multiples else None

    by_instrument: dict[str, dict] = {}
    for t in trades:
        bucket = by_instrument.setdefault(t.instrument, {"total": 0, "wins": 0, "losses": 0})
        bucket["total"] += 1
        if t.result == "WIN":
            bucket["wins"] += 1
        elif t.result == "LOSS":
            bucket["losses"] += 1

    by_strategy: dict[str, dict] = {}
    for t in trades:
        if not t.strategy_combination:
            continue
        bucket = by_strategy.setdefault(t.strategy_combination, {"total": 0, "wins": 0, "losses": 0})
        bucket["total"] += 1
        if t.result == "WIN":
            bucket["wins"] += 1
        elif t.result == "LOSS":
            bucket["losses"] += 1

    return WeeklyReport(
        week_start=week_start.strftime("%d %B %Y"),
        week_end=week_ending.strftime("%d %B %Y"),
        total_signals=len(signals),
        confirmed_setups=sum(1 for s in signals if s.status == "CONFIRMED"),
        executed_trades=len(trades),
        win_rate=round(win_rate, 1),
        profit_factor=round(profit_factor, 2) if profit_factor else None,
        average_r=round(average_r, 2) if average_r else None,
        # TODO: real running peak-equity tracking needed for an accurate
        # weekly max-drawdown figure; not derivable from trade_journal alone.
        max_drawdown_pct=None,
        by_instrument=by_instrument,
        by_session={},  # TODO: needs session tagging on TradeJournalEntry at write time
        by_strategy=by_strategy,
    )


def render_daily_report_text(report: DailyReport) -> str:
    lines = [
        "DAILY TRADING REPORT", "",
        f"Date: {report.date}", "",
        f"Signals Generated: {report.signals_generated}",
        f"A+ Setups: {report.a_plus_setups}",
        f"Trades Executed: {report.trades_executed}",
        f"Winning Trades: {report.winning_trades}",
        f"Losing Trades: {report.losing_trades}",
        f"Net Result: {report.net_result_r:+.2f}R", "",
        f"Best Instrument: {report.best_instrument or 'N/A'}",
        f"Most Effective Strategy Combination: {report.most_effective_strategy or 'N/A'}", "",
        f"Auto-Trading Status: {report.auto_trading_status}",
    ]
    return "\n".join(lines)


def render_weekly_report_text(report: WeeklyReport) -> str:
    lines = [
        "WEEKLY TRADING REPORT", "",
        f"Period: {report.week_start} - {report.week_end}", "",
        f"Total Signals: {report.total_signals}",
        f"Confirmed Setups: {report.confirmed_setups}",
        f"Executed Trades: {report.executed_trades}",
        f"Win Rate: {report.win_rate}%",
        f"Profit Factor: {report.profit_factor if report.profit_factor is not None else 'N/A'}",
        f"Average R: {report.average_r if report.average_r is not None else 'N/A'}", "",
        "Performance by Instrument:",
    ]
    for instrument, stats in report.by_instrument.items():
        lines.append(f"  {instrument}: {stats['wins']}W / {stats['losses']}L (of {stats['total']})")
    if report.by_strategy:
        lines.append("")
        lines.append("Performance by Strategy Combination:")
        for strategy, stats in report.by_strategy.items():
            lines.append(f"  {strategy}: {stats['wins']}W / {stats['losses']}L (of {stats['total']})")
    return "\n".join(lines)
