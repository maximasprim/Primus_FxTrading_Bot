"""
Backtesting Engine (spec Sec.19).

Structural guarantee against look-ahead bias: the simulation loop only ever
hands the engines a DataFrame slice up to and including the current bar
(`df.iloc[: i + 1]`) -- never future bars. Preserve this invariant when extending.

Cost model (spec Sec.19 "spread, commission, slippage"): every simulated fill
is adjusted by spread/2 + slippage against the trader (buys fill higher,
sells fill lower), in both directions (entry AND exit), plus a fixed
commission-per-lot cost expressed in pip-equivalent terms via
`dollar_per_pip_per_lot` (a standard-lot convention -- override per
instrument if you need precision beyond this scaffold's single-lot
assumption).

# TODO still open: walk-forward validation currently only *splits* the date
# range (see `walk_forward_splits`) -- running run_backtest() once per split
# and aggregating out-of-sample results is left to the caller. Full metrics
# (Sharpe, Sortino, recovery factor, MFE/MAE per trade) are also not yet
# computed -- win rate, profit factor, expectancy, and max drawdown are.
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import pandas as pd

from app.core.pip_utils import get_pip_size
from app.data.market_data_engine import MarketDataEngine
from app.engines.market_structure import analyze_market_structure
from app.models.schemas import Direction


@dataclass
class BacktestConfig:
    instrument: str
    timeframe: str
    start: datetime
    end: datetime
    minimum_entry_score: int | None = None
    spread_pips: float = 1.5
    commission_per_lot: float = 7.0
    slippage_pips: float = 0.5
    dollar_per_pip_per_lot: float = 10.0  # standard-lot convention; ~$1 for XAU minis, etc.


@dataclass
class SimulatedTrade:
    entry_time: pd.Timestamp
    direction: str
    raw_entry_price: float
    entry_time_filled_price: float  # after spread/slippage
    exit_time: pd.Timestamp | None = None
    raw_exit_price: float | None = None
    filled_exit_price: float | None = None  # after spread/slippage
    result: str | None = None  # "WIN" | "LOSS" | "OPEN"
    pnl_pips: float | None = None
    pnl_after_costs_pips: float | None = None


@dataclass
class BacktestResult:
    instrument: str
    total_trades: int
    wins: int
    losses: int
    win_rate: float
    profit_factor: float | None
    average_win_pips: float | None
    average_loss_pips: float | None
    expectancy_pips: float | None
    max_drawdown_pct: float
    total_commission_pips_equivalent: float
    trades: list[SimulatedTrade] = field(default_factory=list)


def _apply_entry_costs(direction: str, mid_price: float, pip_size: float, config: BacktestConfig) -> float:
    """Buys fill at ask (higher), sells fill at bid (lower) -- spread/2 either
    side of mid -- plus adverse slippage, both working against the trader."""
    half_spread = (config.spread_pips / 2) * pip_size
    slippage = config.slippage_pips * pip_size
    if direction == "BUY":
        return mid_price + half_spread + slippage
    return mid_price - half_spread - slippage


def _apply_exit_costs(direction: str, mid_price: float, pip_size: float, config: BacktestConfig) -> float:
    """Closing a BUY = selling (fills at bid, lower); closing a SELL = buying
    back (fills at ask, higher) -- same spread/slippage logic, reversed."""
    half_spread = (config.spread_pips / 2) * pip_size
    slippage = config.slippage_pips * pip_size
    if direction == "BUY":
        return mid_price - half_spread - slippage
    return mid_price + half_spread + slippage


def walk_forward_splits(
    start: datetime, end: datetime, train_days: int, test_days: int
) -> list[tuple[datetime, datetime, datetime, datetime]]:
    """Spec Sec.19 -- walk-forward / out-of-sample validation. Returns a list of
    (train_start, train_end, test_start, test_end) tuples marching forward
    through the date range. test_start always equals train_end, so a fold's
    test period never overlaps data the same fold trained on (no look-ahead
    across folds, matching the same invariant the bar-by-bar loop enforces
    within a single run).
    """
    splits: list[tuple[datetime, datetime, datetime, datetime]] = []
    cursor = start
    while True:
        train_start = cursor
        train_end = train_start + timedelta(days=train_days)
        test_start = train_end
        test_end = test_start + timedelta(days=test_days)
        if test_end > end:
            break
        splits.append((train_start, train_end, test_start, test_end))
        cursor = test_start
    return splits


async def run_backtest(config: BacktestConfig) -> BacktestResult:
    engine = MarketDataEngine()
    lookback = config.end - config.start
    df = await engine.get_validated_candles(config.instrument, config.timeframe, lookback)

    # Normalize to tz-aware UTC before filtering -- candle data from the
    # market data engine is always tz-aware, but config.start/end may be
    # naive (e.g. from an API request without a timezone suffix).
    start = pd.Timestamp(config.start)
    end = pd.Timestamp(config.end)
    if start.tzinfo is None:
        start = start.tz_localize("UTC")
    if end.tzinfo is None:
        end = end.tz_localize("UTC")
    df = df[(df.index >= start) & (df.index <= end)]

    pip_size = get_pip_size(config.instrument)
    commission_pips_equivalent = config.commission_per_lot / config.dollar_per_pip_per_lot

    trades: list[SimulatedTrade] = []
    equity_curve: list[float] = [1.0]
    open_trade: SimulatedTrade | None = None

    # Walk forward bar-by-bar; only ever see data up to index i (no look-ahead).
    min_bars_for_structure = 20
    for i in range(min_bars_for_structure, len(df)):
        window = df.iloc[: i + 1]  # <-- no look-ahead: only past + current bar
        current = window.iloc[-1]

        if open_trade is None:
            structure = analyze_market_structure(window.tail(100))
            direction = None
            if structure.bias.value.endswith("Bullish"):
                direction = Direction.BUY
            elif structure.bias.value.endswith("Bearish"):
                direction = Direction.SELL

            if direction:
                mid_price = float(current["close"])
                filled_entry = _apply_entry_costs(direction.value, mid_price, pip_size, config)
                open_trade = SimulatedTrade(
                    entry_time=window.index[-1],
                    direction=direction.value,
                    raw_entry_price=mid_price,
                    entry_time_filled_price=filled_entry,
                )
        else:
            # Simplified exit: close after a fixed holding period. Replace
            # with real stop/target logic once entry_zone/stop_loss/
            # take_profit are computed per-bar (see signal_service.py's
            # equivalent live-analysis logic for the pattern to mirror).
            holding_period = window.index[-1] - open_trade.entry_time
            if holding_period >= timedelta(hours=24):
                mid_price = float(current["close"])
                filled_exit = _apply_exit_costs(open_trade.direction, mid_price, pip_size, config)

                open_trade.exit_time = window.index[-1]
                open_trade.raw_exit_price = mid_price
                open_trade.filled_exit_price = filled_exit

                raw_pnl = (
                    (mid_price - open_trade.raw_entry_price)
                    if open_trade.direction == "BUY"
                    else (open_trade.raw_entry_price - mid_price)
                )
                filled_pnl = (
                    (filled_exit - open_trade.entry_time_filled_price)
                    if open_trade.direction == "BUY"
                    else (open_trade.entry_time_filled_price - filled_exit)
                )
                pnl_after_commission_pips = (filled_pnl / pip_size) - commission_pips_equivalent

                open_trade.pnl_pips = raw_pnl / pip_size
                open_trade.pnl_after_costs_pips = pnl_after_commission_pips
                open_trade.result = "WIN" if pnl_after_commission_pips > 0 else "LOSS"

                equity_curve.append(
                    equity_curve[-1] * (1 + (pnl_after_commission_pips * pip_size) / open_trade.raw_entry_price)
                )
                trades.append(open_trade)
                open_trade = None

    wins = sum(1 for t in trades if t.result == "WIN")
    losses = sum(1 for t in trades if t.result == "LOSS")
    total = wins + losses

    win_pips = [t.pnl_after_costs_pips for t in trades if t.result == "WIN" and t.pnl_after_costs_pips is not None]
    loss_pips = [
        abs(t.pnl_after_costs_pips) for t in trades if t.result == "LOSS" and t.pnl_after_costs_pips is not None
    ]

    gross_win = sum(win_pips)
    gross_loss = sum(loss_pips)
    profit_factor = (gross_win / gross_loss) if gross_loss > 0 else None

    avg_win = (gross_win / len(win_pips)) if win_pips else None
    avg_loss = (gross_loss / len(loss_pips)) if loss_pips else None

    win_rate = (wins / total) if total else 0.0
    expectancy = None
    if avg_win is not None and avg_loss is not None:
        expectancy = (win_rate * avg_win) - ((1 - win_rate) * avg_loss)

    peak = equity_curve[0]
    max_dd = 0.0
    for value in equity_curve:
        peak = max(peak, value)
        drawdown = (peak - value) / peak
        max_dd = max(max_dd, drawdown)

    return BacktestResult(
        instrument=config.instrument,
        total_trades=total,
        wins=wins,
        losses=losses,
        win_rate=win_rate * 100,
        profit_factor=profit_factor,
        average_win_pips=round(avg_win, 2) if avg_win is not None else None,
        average_loss_pips=round(avg_loss, 2) if avg_loss is not None else None,
        expectancy_pips=round(expectancy, 2) if expectancy is not None else None,
        max_drawdown_pct=max_dd * 100,
        total_commission_pips_equivalent=round(commission_pips_equivalent * total, 2),
        trades=trades,
    )
