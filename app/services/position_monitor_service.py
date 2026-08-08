"""
Position Monitoring Service.

Closes the gap flagged repeatedly in PHASES.md: nothing previously closed
out open trade_journal entries automatically, so Performance stayed empty
forever even after trades executed. This checks every OPEN entry against
the current price and closes it (WIN/LOSS) the moment stop-loss or
take-profit is actually touched -- producing a genuine track record instead
of an invented one.

Also records an equity snapshot on every close (app/services/
equity_tracking_service.py), so the real-drawdown safety check gets fed
real data over time instead of staying at "no history."

# NOTE: this is triggered on-demand (via API call, see
# app/api/routes/positions.py) rather than by a background scheduler --
# there's no Celery beat wired into this project yet. The frontend polls
# the check endpoint periodically, which is sufficient while the dashboard
# is open; a real scheduled job would be needed for it to run unattended.
"""
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.data.market_data_engine import MarketDataEngine
from app.models.db_models import TradeJournalEntry
from app.services.equity_tracking_service import record_equity_snapshot
from app.services.trade_journal_service import close_trade, get_open_trades


@dataclass
class ClosedTradeSummary:
    signal_id: str
    instrument: str
    direction: str
    result: str
    exit_price: float
    profit_loss: float | None
    r_multiple: float | None


@dataclass
class PositionCheckResult:
    checked: int
    closed: list[ClosedTradeSummary]
    still_open: int


def _check_hit(entry: TradeJournalEntry, current_price: float) -> str | None:
    """Returns "WIN", "LOSS", or None (still open) based on whether the
    current price has reached the stop-loss or take-profit level."""
    if entry.direction == "BUY":
        if current_price <= entry.stop_loss:
            return "LOSS"
        if entry.take_profit and current_price >= entry.take_profit:
            return "WIN"
    else:  # SELL
        if current_price >= entry.stop_loss:
            return "LOSS"
        if entry.take_profit and current_price <= entry.take_profit:
            return "WIN"
    return None


async def check_and_close_open_positions(db: AsyncSession, starting_equity: float = 10_000.0) -> PositionCheckResult:
    """Checks every OPEN trade_journal entry against the current price and
    closes any that have hit stop or target. Returns a summary of what
    happened this pass. Running equity is approximated as starting_equity
    plus the sum of all realized profit_loss so far -- a proper account
    balance integration (spec §12 BrokerExecutionInterface.get_account_balance)
    would replace this once a real broker adapter is in use.
    """
    market_data = MarketDataEngine()
    open_trades = await get_open_trades(db)

    closed: list[ClosedTradeSummary] = []
    for entry in open_trades:
        try:
            price_data = await market_data.get_current_price(entry.instrument)
        except Exception:  # noqa: BLE001 - a stale/unreachable price feed shouldn't crash the whole pass
            continue

        current_price = (price_data["bid"] + price_data["ask"]) / 2
        result = _check_hit(entry, current_price)
        if result is None:
            continue

        closed_entry = await close_trade(db, entry, exit_price=current_price, result=result)
        closed.append(
            ClosedTradeSummary(
                signal_id=closed_entry.signal_id,
                instrument=closed_entry.instrument,
                direction=closed_entry.direction,
                result=closed_entry.result,
                exit_price=current_price,
                profit_loss=closed_entry.profit_loss,
                r_multiple=closed_entry.r_multiple,
            )
        )

    if closed:
        all_time_realized_pnl = await _get_total_realized_pnl(db)
        await record_equity_snapshot(db, starting_equity + all_time_realized_pnl, reason="trade_closed")

    return PositionCheckResult(checked=len(open_trades), closed=closed, still_open=len(open_trades) - len(closed))


async def _get_total_realized_pnl(db: AsyncSession) -> float:
    """Sum of profit_loss across every closed (WIN/LOSS) trade ever -- used
    to compute the running equity snapshot correctly, not just this pass's
    closures."""
    result = await db.execute(
        select(TradeJournalEntry.profit_loss).where(TradeJournalEntry.result.in_(["WIN", "LOSS"]))
    )
    return sum(pnl for (pnl,) in result.all() if pnl is not None)