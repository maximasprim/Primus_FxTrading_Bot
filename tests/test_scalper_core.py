"""Config rules, session guard and signal scoring."""
import json

import numpy as np
import pandas as pd
import pytest
from pydantic import ValidationError

from app.scalper.config import ScalperConfig, ScalperConfigStore
from app.scalper.guard import SessionGuard, consecutive_losses
from app.scalper.models import Deal
from app.scalper.signals import compute_signal


# ---------------- config ----------------
def test_defaults_are_conservative():
    c = ScalperConfig()
    assert c.backend == "simulation"
    assert c.allow_live_account is False
    assert c.session_max_loss_money > 0 or c.session_max_loss_pct > 0


def test_loss_limit_is_mandatory():
    with pytest.raises(ValidationError):
        ScalperConfig(session_max_loss_money=0, session_max_loss_pct=0)


@pytest.mark.parametrize("field,value", [
    ("max_concurrent_positions", 0), ("max_concurrent_positions", 21), ("entries_per_signal", 6),
    ("risk_per_trade_pct", 5.0), ("max_total_risk_pct", 50.0), ("min_signal_score", 5), ("fixed_lot", 0.0),
])
def test_hard_bounds_hold(field, value):
    with pytest.raises(ValidationError):
        ScalperConfig(**{field: value})


def test_unknown_field_and_instrument_rejected():
    with pytest.raises(ValidationError):
        ScalperConfig(made_up=1)
    with pytest.raises(ValidationError):
        ScalperConfig(instruments=["DOGE/USD"])


def test_instruments_normalised_and_deduped():
    assert ScalperConfig(instruments=["eur/usd", "EUR-USD", "XAU/USD"]).instruments == ["EUR/USD", "XAU/USD"]


def test_per_instrument_cap_cannot_exceed_total():
    c = ScalperConfig(max_concurrent_positions=2, max_positions_per_instrument=8)
    assert c.max_positions_per_instrument == 2


def test_store_persists_and_survives_garbage(tmp_path):
    store = ScalperConfigStore(tmp_path / "c.json")
    store.update({"max_concurrent_positions": 7})
    assert ScalperConfigStore(tmp_path / "c.json").get().max_concurrent_positions == 7
    (tmp_path / "c.json").write_text("{not json")
    assert ScalperConfigStore(tmp_path / "c.json").get().max_concurrent_positions == 3  # safe defaults


def test_failed_update_changes_nothing(tmp_path):
    store = ScalperConfigStore(tmp_path / "c.json")
    with pytest.raises(ValidationError):
        store.update({"max_concurrent_positions": 999})
    assert store.get().max_concurrent_positions == 3


# ---------------- guard ----------------
def D(p, t=0):
    return Deal("1", "EUR/USD", p, t)


def test_consecutive_losses_counts_trailing_only():
    assert consecutive_losses([D(-1), D(2), D(-1), D(-1)]) == 2
    assert consecutive_losses([D(-1), D(-1), D(3)]) == 0


def test_halt_rules():
    g = SessionGuard(10_000, 0)
    cfg = ScalperConfig(session_max_loss_money=50, session_max_loss_pct=0, session_profit_target_money=100,
                        max_consecutive_losses=3)
    assert g.halt_reason(cfg, 10, []) is None
    assert "loss limit" in g.halt_reason(cfg, -50, [])
    assert "profit target" in g.halt_reason(cfg, 100, [])
    assert "in a row" in g.halt_reason(cfg, -5, [D(-1), D(-1), D(-1)])
    pct = ScalperConfig(session_max_loss_money=0, session_max_loss_pct=1.0)
    assert g.halt_reason(pct, -99, []) is None
    assert "loss limit" in g.halt_reason(pct, -100, [])


def test_entry_blocks():
    g = SessionGuard(10_000, 0)
    cfg = ScalperConfig(max_concurrent_positions=3, max_positions_per_instrument=2, cooldown_seconds=30, max_trades_per_hour=2)
    assert g.entry_block(cfg, "EUR/USD", 3, 0, 0) and "concurrent" in g.entry_block(cfg, "EUR/USD", 3, 0, 0)
    assert "positions on EUR/USD" in g.entry_block(cfg, "EUR/USD", 2, 2, 0)
    assert "netting" in g.entry_block(cfg, "EUR/USD", 1, 1, 0, hedging=False)
    g.record_entry("EUR/USD", 100)
    assert "cooldown" in g.entry_block(cfg, "EUR/USD", 1, 0, 110)
    assert g.entry_block(cfg, "GBP/USD", 1, 0, 110) is None  # cooldown is per instrument
    g.record_entry("GBP/USD", 111)
    assert "trades/hour" in g.entry_block(cfg, "XAU/USD", 0, 0, 120)
    assert g.entry_block(cfg, "XAU/USD", 0, 0, 100 + 3601) is None  # hour window rolls


# ---------------- signals ----------------
def _bars(drift, n=120, vol=0.0002, seed=3):
    rng = np.random.default_rng(seed)
    c = 1.1 + np.cumsum(rng.normal(drift, vol, n))
    o = np.r_[c[0], c[:-1]]
    h = np.maximum(o, c) + abs(rng.normal(0, vol / 3, n))
    l = np.minimum(o, c) - abs(rng.normal(0, vol / 3, n))
    return pd.DataFrame({"open": o, "high": h, "low": l, "close": c})


def test_signal_direction_follows_strong_trend():
    up = compute_signal(_bars(0.00015), 0.8, 0.0001)
    dn = compute_signal(_bars(-0.00015), 0.8, 0.0001)
    assert up.side == "BUY" and dn.side == "SELL"
    assert 0 <= up.score <= 100


def test_signal_refuses_when_market_too_quiet_for_spread():
    r = compute_signal(_bars(0.00015, vol=0.00002), 3.0, 0.0001)
    assert r.side is None and "too quiet" in r.reasons[0]


def test_signal_needs_history():
    assert compute_signal(_bars(0.0001, n=20), 0.8, 0.0001).side is None
