"""
Trade Execution Service (execution-feature spec §1, §3, §11, §14-15, §17).

This is the ONLY module authorized to call a broker adapter's `place_order`.
The AI/analysis layer (signal_service) never touches this directly — routes
call signal_service first, then hand the resulting TradeSetup here.

Implements the full pipeline from spec §3:

    Market Data -> Market Analysis -> Multi-Timeframe Confirmation ->
    Strategy Consortium -> Confluence Score -> Entry Confirmation ->
    News Risk -> Risk Management -> Account Exposure -> Correlation ->
    Daily Loss Limit -> Consecutive Loss -> Broker Connection ->
    Auto-Trading Mode -> Duplicate Trade -> Final Validation -> PLACE TRADE

If ANY stage fails, execution stops immediately and a clear reason is
returned — this function must never partially execute.
"""
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.correlation import OpenPosition, check_correlated_exposure_warning
from app.core.risk import AccountState, check_risk_gates
from app.core.trading_mode import trading_mode_manager
from app.data.market_data_engine import MarketDataEngine
from app.engines.news_risk import assess_news_risk
from app.execution.adapters.paper_adapter import PaperTradingAdapter
from app.execution.approval_service import approval_service
from app.execution.broker_interface import BrokerExecutionInterface, OrderRequest
from app.execution.duplicate_protection import duplicate_guard, generate_signal_id
from app.execution.safety_shutdown import run_safety_checks
from app.services.equity_tracking_service import record_equity_snapshot
from app.models.execution_schemas import ExecutionResult, ExecutionSource, ExecutionValidationFailure
from app.models.schemas import Direction, SetupStatus, TradeSetup
from app.services.trade_journal_service import get_open_signal_ids, record_trade


def _get_broker() -> BrokerExecutionInterface:
    settings = get_settings()
    if settings.broker_adapter == "paper":
        return PaperTradingAdapter()
    if settings.broker_adapter == "mt5":
        raise NotImplementedError(
            "MT5Adapter (app/execution/adapters/mt5_adapter.py) is a stub. "
            "Configure real credentials and finish it before selecting it."
        )
    raise ValueError(f"Unknown broker adapter: {settings.broker_adapter}")


def _fail(stage: str, reason: str, signal_id: str) -> ExecutionResult:
    """Used for routine pipeline-gating rejections (score too low, news risk,
    risk management, correlation, safety shutdown, mode not enabled, etc.).
    Deliberately does NOT mark the signal ID as processed: no trade was
    attempted, so a later analysis run for the same instrument/day/timeframe
    must be allowed to try again once conditions change. Only an actual
    execution attempt (success or broker-level rejection) permanently
    consumes a signal ID — see the end of process_setup_for_execution."""
    return ExecutionResult(
        signal_id=signal_id,
        executed=False,
        execution_source=ExecutionSource.AUTOMATIC,
        failure=ExecutionValidationFailure(stage=stage, reason=reason),
    )


async def process_setup_for_execution(
    setup: TradeSetup,
    db: AsyncSession,
    account: AccountState,
    existing_positions: list[OpenPosition] | None = None,
) -> ExecutionResult:
    """Run a confirmed TradeSetup through the full pipeline. Returns an
    ExecutionResult describing whether a trade was placed, or, if not,
    exactly which stage blocked it. This function is also the entry point
    for MANUAL_APPROVAL mode (it creates an approval request instead of
    executing) and for instruments not enabled for auto-trading (it simply
    does not execute — the caller is expected to have already alerted).
    """
    settings = get_settings()
    existing_positions = existing_positions or []
    signal_id = generate_signal_id(setup)

    # --- Duplicate Trade Check ---
    if await duplicate_guard.is_duplicate(signal_id):
        return _fail("duplicate_trade_check", f"Signal {signal_id} already processed.", signal_id)

    open_signal_ids = await get_open_signal_ids(db, setup.instrument)
    if duplicate_guard.has_active_trade_for_instrument(setup.instrument, open_signal_ids):
        return _fail(
            "duplicate_trade_check",
            f"Another trade is already open for {setup.instrument}.",
            signal_id,
        )

    # --- Entry Confirmation / basic setup validity ---
    if setup.status != SetupStatus.CONFIRMED or setup.direction not in (Direction.BUY, Direction.SELL):
        return _fail("entry_confirmation", "Setup is not a confirmed BUY/SELL.", signal_id)
    if setup.entry_zone is None or setup.stop_loss is None or setup.take_profit_1 is None:
        return _fail("entry_confirmation", "Setup is missing entry/stop/target levels.", signal_id)

    # --- Confluence Score (stricter auto-execution bar, spec §4) ---
    if setup.confluence_score < settings.auto_execution_minimum_score:
        return _fail(
            "confluence_score",
            f"Score {setup.confluence_score} below auto-execution minimum "
            f"({settings.auto_execution_minimum_score}).",
            signal_id,
        )
    if not setup.risk_reward or setup.risk_reward < settings.auto_execution_min_risk_reward:
        return _fail(
            "risk_reward",
            f"R:R {setup.risk_reward} below auto-execution minimum ({settings.auto_execution_min_risk_reward}).",
            signal_id,
        )

    # --- News Risk Check ---
    news = await assess_news_risk(setup.instrument)
    if news.is_blocked:
        return _fail("news_risk", news.reason or "Blocked by high-impact news.", signal_id)

    # --- Risk Management Validation (daily/weekly loss, consecutive losses, R:R, sizing) ---
    entry_mid = (setup.entry_zone.min + setup.entry_zone.max) / 2
    risk_check = check_risk_gates(account, entry_mid, setup.stop_loss, setup.take_profit_1)
    if not risk_check.allowed:
        return _fail("risk_management", risk_check.reason or "Risk gate failed.", signal_id)

    # --- Account Exposure / Correlation Check ---
    correlation_warning = check_correlated_exposure_warning(
        existing_positions, setup.instrument, setup.direction.value
    )
    if correlation_warning:
        return _fail("correlation_check", correlation_warning, signal_id)

    # --- Broker Connection Check + Market Data Freshness (safety shutdown battery) ---
    broker = _get_broker()
    market_data_engine = MarketDataEngine()
    try:
        await market_data_engine.get_current_price(setup.instrument)
        data_fresh = True
    except Exception:  # noqa: BLE001
        data_fresh = False

    safety = await run_safety_checks(account, broker, market_data_is_fresh=data_fresh, db=db)
    if not safety.safe_to_trade:
        return _fail("safety_shutdown", safety.reason or "Safety check failed.", signal_id)

    # --- Auto-Trading Mode Check ---
    if trading_mode_manager.is_approval_required(setup.instrument):
        approval_request = approval_service.create_request(signal_id, setup, settings.risk_per_trade_pct)
        return ExecutionResult(
            signal_id=signal_id,
            executed=False,
            execution_source=ExecutionSource.APPROVED,
            failure=ExecutionValidationFailure(
                stage="auto_trading_mode",
                reason=f"Manual approval required. Request created, expires at {approval_request.expires_at.isoformat()}.",
            ),
        )

    if not trading_mode_manager.is_auto_execution_allowed(setup.instrument):
        return _fail(
            "auto_trading_mode",
            f"Auto-trading is not enabled for {setup.instrument} (or globally).",
            signal_id,
        )

    # --- Final Execution Validation: has price moved too far from the setup? ---
    current_price = await market_data_engine.get_current_price(setup.instrument)
    mid = (current_price["bid"] + current_price["ask"]) / 2
    drift_pct = abs(mid - entry_mid) / entry_mid
    if drift_pct > 0.0015:  # ~0.15% — re-evaluate rather than blindly execute stale levels
        return _fail(
            "final_execution_validation",
            f"Price moved {drift_pct * 100:.2f}% from original entry ({entry_mid} -> {mid}). Re-evaluate setup.",
            signal_id,
        )

    # --- PLACE TRADE ---
    account_balance = await broker.get_account_balance()
    await record_equity_snapshot(db, account_balance, reason="pre_trade_snapshot")
    position_size = await broker.calculate_position_size(
        account_balance, settings.risk_per_trade_pct, entry_mid, setup.stop_loss
    )

    order = OrderRequest(
        signal_id=signal_id,
        instrument=setup.instrument,
        direction=setup.direction.value,
        entry_price=entry_mid,
        stop_loss=setup.stop_loss,
        take_profit=setup.take_profit_1,
        position_size=position_size,
        max_slippage_pips=settings.max_allowed_slippage_pips,
    )
    order_result = await broker.place_order(order)

    if not order_result.success:
        await duplicate_guard.mark_processed(signal_id, executed=False)
        return ExecutionResult(
            signal_id=signal_id,
            executed=False,
            execution_source=ExecutionSource.AUTOMATIC,
            failure=ExecutionValidationFailure(stage="broker_execution", reason=order_result.reason or "Broker rejected order."),
        )

    await record_trade(
        db=db,
        signal_id=signal_id,
        setup=setup,
        entry_price=order_result.filled_price or entry_mid,
        position_size=position_size,
        risk_pct=settings.risk_per_trade_pct,
        execution_source=ExecutionSource.AUTOMATIC,
        broker_order_id=order_result.broker_order_id,
    )
    await duplicate_guard.mark_processed(signal_id, executed=True)

    return ExecutionResult(
        signal_id=signal_id,
        executed=True,
        execution_source=ExecutionSource.AUTOMATIC,
        broker_order_id=order_result.broker_order_id,
        position_size=position_size,
    )
