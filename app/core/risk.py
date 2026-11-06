"""
Risk Management Engine (spec §17).

This module is the hard gate between "a high-confluence setup exists" and
"the user is alerted to it." It must never be bypassed, including in paper
trading - the whole point is to build the discipline into the system itself.

Explicitly disallowed by spec and NOT implemented here (by design):
  - martingale / doubling position size after a loss
  - moving stop-loss further away to avoid a loss
  - unlimited signal generation
"""
from dataclasses import dataclass
from datetime import date

from app.config import get_settings


@dataclass
class AccountState:
    """Minimal running state needed to evaluate risk gates.
    In production this should be populated from the paper-trading /
    signal-tracking ledger (see services/performance_service.py), not
    held in memory.
    """

    equity: float
    daily_pnl_pct: float = 0.0
    weekly_pnl_pct: float = 0.0
    consecutive_losses: int = 0
    trades_today: int = 0
    last_reset_date: date | None = None


@dataclass
class RiskCheckResult:
    allowed: bool
    reason: str | None = None
    position_size_units: float | None = None


def position_size_from_risk(
    equity: float,
    risk_per_trade_pct: float,
    entry_price: float,
    stop_loss_price: float,
    pip_value_per_unit: float = 1.0,
) -> float:
    """Fixed-percentage risk sizing: risk a fixed % of equity, sized to the
    distance between entry and stop.
    """
    risk_amount = equity * (risk_per_trade_pct / 100)
    stop_distance = abs(entry_price - stop_loss_price)
    if stop_distance <= 0:
        return 0.0
    return risk_amount / (stop_distance * pip_value_per_unit)


def check_risk_gates(
    account: AccountState,
    entry_price: float,
    stop_loss_price: float,
    take_profit_price: float,
) -> RiskCheckResult:
    """Run every hard-stop rule from spec §17 before a signal is allowed
    to progress to a confirmed alert. Any single failing rule blocks the trade.
    """
    settings = get_settings()

    if account.daily_pnl_pct <= -abs(settings.max_daily_loss_pct):
        return RiskCheckResult(False, "Blocked: maximum daily loss limit reached.")

    if account.weekly_pnl_pct <= -abs(settings.max_weekly_loss_pct):
        return RiskCheckResult(False, "Blocked: maximum weekly loss limit reached.")

    if account.consecutive_losses >= settings.max_consecutive_losses:
        return RiskCheckResult(
            False,
            f"Blocked: {account.consecutive_losses} consecutive losses reached "
            f"the configured limit ({settings.max_consecutive_losses}).",
        )

    stop_distance = abs(entry_price - stop_loss_price)
    reward_distance = abs(take_profit_price - entry_price)
    if stop_distance <= 0:
        return RiskCheckResult(False, "Blocked: invalid stop-loss distance.")

    rr = reward_distance / stop_distance
    if rr < settings.min_risk_reward:
        return RiskCheckResult(
            False,
            f"Blocked: risk-to-reward {rr:.2f} is below the minimum "
            f"({settings.min_risk_reward}).",
        )

    size = position_size_from_risk(
        account.equity, settings.risk_per_trade_pct, entry_price, stop_loss_price
    )
    return RiskCheckResult(True, None, size)
