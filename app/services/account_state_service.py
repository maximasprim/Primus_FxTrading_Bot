"""
Account State Service.

Every risk gate, position-sizing calculation, and drawdown check in this
system runs against `AccountState` (app/core/risk.py). Several call sites
used to construct that with a hardcoded `equity=10_000.0` placeholder,
which meant those gates weren't actually protecting real capital. This
module builds the real thing from the broker's live balance and the
trade journal's history, and should be the ONLY way `AccountState` gets
constructed outside of tests.
"""
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.risk import AccountState
from app.execution.broker_interface import BrokerExecutionInterface
from app.models.db_models import TradeJournalEntry

_CLOSED_RESULTS = ("WIN", "LOSS", "BE")


async def build_account_state(db: AsyncSession, broker: BrokerExecutionInterface) -> AccountState:
    equity = await broker.get_account_balance()

    now = datetime.utcnow()
    start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    start_of_week = start_of_day - timedelta(days=start_of_day.weekday())

    closed_result = await db.execute(
        select(TradeJournalEntry)
        .where(TradeJournalEntry.result.in_(_CLOSED_RESULTS))
        .order_by(TradeJournalEntry.timestamp.desc())
    )
    closed_trades = list(closed_result.scalars().all())

    def _pnl_pct_since(cutoff: datetime) -> float:
        realized = sum((t.profit_loss or 0.0) for t in closed_trades if t.timestamp >= cutoff)
        # Back out today's/this week's realized P&L to get the baseline
        # equity the % move should be measured against.
        baseline = equity - realized
        if baseline <= 0:
            return 0.0
        return (realized / baseline) * 100

    daily_pnl_pct = _pnl_pct_since(start_of_day)
    weekly_pnl_pct = _pnl_pct_since(start_of_week)

    consecutive_losses = 0
    for trade in closed_trades:
        if trade.result == "LOSS":
            consecutive_losses += 1
        else:
            break

    trades_today_result = await db.execute(
        select(TradeJournalEntry).where(TradeJournalEntry.timestamp >= start_of_day)
    )
    trades_today = len(trades_today_result.scalars().all())

    return AccountState(
        equity=equity,
        daily_pnl_pct=daily_pnl_pct,
        weekly_pnl_pct=weekly_pnl_pct,
        consecutive_losses=consecutive_losses,
        trades_today=trades_today,
        last_reset_date=start_of_day.date(),
    )