"""
Automatic Safety Shutdowns (execution-feature spec §10).

Default safety principle from the spec: "If the system is uncertain, the
system must not open a new trade." Every check here is a hard block, not a
warning - if any fires, auto-trading is paused/disabled via the
TradingModeManager, and the reason is returned for alerting.
"""
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.risk import AccountState
from app.core.trading_mode import trading_mode_manager
from app.execution.broker_interface import BrokerExecutionInterface
from app.services.equity_tracking_service import get_drawdown_status


@dataclass
class SafetyCheckResult:
    safe_to_trade: bool
    reason: str | None = None
    action: str | None = None  # "AUTO_TRADING_PAUSED" | "AUTO_TRADING_DISABLED" | "TRADING_DISABLED"


async def run_safety_checks(
    account: AccountState,
    broker: BrokerExecutionInterface,
    market_data_is_fresh: bool,
    db: AsyncSession | None = None,
) -> SafetyCheckResult:
    settings = get_settings()

    # Daily loss limit
    if account.daily_pnl_pct <= -abs(settings.max_daily_loss_pct):
        trading_mode_manager.stop_new_trades(changed_by="system:safety_shutdown")
        return SafetyCheckResult(
            False,
            f"Daily loss limit reached ({account.daily_pnl_pct:.2f}% <= -{settings.max_daily_loss_pct}%).",
            "AUTO_TRADING_PAUSED",
        )

    # Weekly loss limit
    if account.weekly_pnl_pct <= -abs(settings.max_weekly_loss_pct):
        trading_mode_manager.stop_new_trades(changed_by="system:safety_shutdown")
        return SafetyCheckResult(
            False,
            f"Weekly loss limit reached ({account.weekly_pnl_pct:.2f}% <= -{settings.max_weekly_loss_pct}%).",
            "AUTO_TRADING_DISABLED",
        )

    # Consecutive losses
    if account.consecutive_losses >= settings.max_consecutive_losses:
        trading_mode_manager.stop_new_trades(changed_by="system:safety_shutdown")
        return SafetyCheckResult(
            False,
            f"{account.consecutive_losses} consecutive losses reached the configured limit "
            f"({settings.max_consecutive_losses}). Manual review required before reactivation.",
            "AUTO_TRADING_PAUSED",
        )

    # Max drawdown -- computed against the real historical peak equity
    # (app/services/equity_tracking_service.py), not inferred from a single
    # current-equity value with no memory of what came before.
    if db is not None:
        drawdown = await get_drawdown_status(db, account.equity)
        if drawdown.drawdown_pct >= settings.max_drawdown_pct:
            trading_mode_manager.stop_new_trades(changed_by="system:safety_shutdown")
            return SafetyCheckResult(
                False,
                f"Maximum drawdown threshold reached: {drawdown.drawdown_pct:.2f}% down from peak equity "
                f"{drawdown.peak_equity:.2f} (limit: {settings.max_drawdown_pct}%).",
                "AUTO_TRADING_DISABLED",
            )
    # else: no DB session provided -- drawdown check is skipped (e.g. dry-run
    # callers/tests). All other checks still apply.

    # Market data freshness
    if not market_data_is_fresh:
        trading_mode_manager.stop_new_trades(changed_by="system:safety_shutdown")
        return SafetyCheckResult(False, "Market data is stale.", "TRADING_DISABLED")

    # Broker connection health
    healthy = await broker.is_connection_healthy()
    if not healthy:
        trading_mode_manager.stop_new_trades(changed_by="system:safety_shutdown")
        return SafetyCheckResult(False, "Broker connection unavailable.", "TRADING_DISABLED")

    return SafetyCheckResult(True)
