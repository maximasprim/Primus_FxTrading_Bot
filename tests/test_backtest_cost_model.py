"""Tests for app/services/backtest_service.py cost model and walk-forward splitting."""
from datetime import datetime

from app.core.pip_utils import get_pip_size
from app.services.backtest_service import BacktestConfig, _apply_entry_costs, _apply_exit_costs, walk_forward_splits


def test_buy_entry_costs_more_than_mid():
    config = BacktestConfig(instrument="EUR/USD", timeframe="1H", start=datetime(2026, 1, 1), end=datetime(2026, 2, 1))
    pip_size = get_pip_size("EUR/USD")
    filled = _apply_entry_costs("BUY", 1.0800, pip_size, config)
    assert filled > 1.0800


def test_sell_entry_costs_less_than_mid():
    config = BacktestConfig(instrument="EUR/USD", timeframe="1H", start=datetime(2026, 1, 1), end=datetime(2026, 2, 1))
    pip_size = get_pip_size("EUR/USD")
    filled = _apply_entry_costs("SELL", 1.0800, pip_size, config)
    assert filled < 1.0800


def test_exit_costs_work_against_trader_both_directions():
    config = BacktestConfig(instrument="EUR/USD", timeframe="1H", start=datetime(2026, 1, 1), end=datetime(2026, 2, 1))
    pip_size = get_pip_size("EUR/USD")
    # Closing a BUY = selling -> fills at bid, lower than mid.
    assert _apply_exit_costs("BUY", 1.0800, pip_size, config) < 1.0800
    # Closing a SELL = buying back -> fills at ask, higher than mid.
    assert _apply_exit_costs("SELL", 1.0800, pip_size, config) > 1.0800


def test_pip_size_conventions():
    assert get_pip_size("EUR/USD") == 0.0001
    assert get_pip_size("USD/JPY") == 0.01
    assert get_pip_size("XAU/USD") == 0.1


def test_walk_forward_splits_do_not_overlap_within_a_fold():
    splits = walk_forward_splits(datetime(2026, 1, 1), datetime(2026, 3, 1), train_days=20, test_days=10)
    assert len(splits) > 0
    for train_start, train_end, test_start, test_end in splits:
        assert train_end == test_start  # test period starts exactly where training ends
        assert train_start < train_end < test_end


def test_walk_forward_splits_march_forward():
    splits = walk_forward_splits(datetime(2026, 1, 1), datetime(2026, 3, 1), train_days=20, test_days=10)
    if len(splits) >= 2:
        assert splits[1][0] == splits[0][2]  # next fold's train_start == previous fold's test_start
