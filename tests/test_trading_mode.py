"""Tests for app/core/trading_mode.py -- verifies the safety invariants from
execution-feature spec Sec.2, Sec.5, Sec.7-9, Sec.21."""
import pytest

from app.core.trading_mode import TradingModeManager
from app.models.execution_schemas import GlobalTradingMode, InstrumentTradingMode


def test_defaults_are_safe():
    """Spec Sec.21: system must start with auto-trading OFF."""
    mgr = TradingModeManager()
    assert mgr.state.auto_trading_enabled is False
    assert mgr.state.global_mode in (GlobalTradingMode.ALERT_MODE, GlobalTradingMode.ANALYSIS_ONLY)


@pytest.mark.asyncio
async def test_two_step_activation_required():
    """Spec Sec.8: enabling auto-trading requires request + confirm."""
    mgr = TradingModeManager()
    await mgr.set_instrument_mode("EUR/USD", InstrumentTradingMode.AUTO)

    # Not enabled yet -- no request made.
    assert mgr.is_auto_execution_allowed("EUR/USD") is False

    mgr.request_enable_auto_trading(requested_by="alice")
    # Still not enabled -- step 1 only requests, doesn't activate.
    assert mgr.state.auto_trading_enabled is False

    confirmed = mgr.confirm_enable_auto_trading(confirmed_by="alice")
    assert confirmed is True
    assert mgr.state.auto_trading_enabled is True
    assert mgr.is_auto_execution_allowed("EUR/USD") is True


def test_confirm_by_different_actor_fails():
    mgr = TradingModeManager()
    mgr.request_enable_auto_trading(requested_by="alice")
    confirmed = mgr.confirm_enable_auto_trading(confirmed_by="mallory")
    assert confirmed is False
    assert mgr.state.auto_trading_enabled is False


@pytest.mark.asyncio
async def test_emergency_stop_blocks_auto_execution_but_not_flagged_as_mode_change():
    mgr = TradingModeManager()
    await mgr.set_instrument_mode("EUR/USD", InstrumentTradingMode.AUTO)
    mgr.request_enable_auto_trading(requested_by="alice")
    mgr.confirm_enable_auto_trading(confirmed_by="alice")
    assert mgr.is_auto_execution_allowed("EUR/USD") is True

    mgr.stop_new_trades(changed_by="system:safety_shutdown")
    assert mgr.is_auto_execution_allowed("EUR/USD") is False
    assert mgr.state.emergency_stop_active is True
    assert mgr.state.auto_trading_enabled is False


def test_instrument_not_set_to_auto_never_auto_executes():
    mgr = TradingModeManager()
    mgr.request_enable_auto_trading(requested_by="alice")
    mgr.confirm_enable_auto_trading(confirmed_by="alice")
    # GBP/USD was never explicitly set to AUTO -> defaults to ALERT_ONLY.
    assert mgr.is_auto_execution_allowed("GBP/USD") is False


@pytest.mark.asyncio
async def test_manual_approval_mode_flags_approval_required():
    mgr = TradingModeManager()
    await mgr.set_global_mode(GlobalTradingMode.MANUAL_APPROVAL, changed_by="alice")
    assert mgr.is_approval_required("EUR/USD") is True
    assert mgr.is_auto_execution_allowed("EUR/USD") is False


@pytest.mark.asyncio
async def test_switching_away_from_full_auto_disables_auto_trading():
    mgr = TradingModeManager()
    await mgr.set_instrument_mode("EUR/USD", InstrumentTradingMode.AUTO)
    mgr.request_enable_auto_trading(requested_by="alice")
    mgr.confirm_enable_auto_trading(confirmed_by="alice")
    assert mgr.state.auto_trading_enabled is True

    await mgr.set_global_mode(GlobalTradingMode.ALERT_MODE, changed_by="alice")
    assert mgr.state.auto_trading_enabled is False
