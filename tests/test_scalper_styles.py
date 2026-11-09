"""Entry styles: each one fires on the pattern it is built for, stays out otherwise, and plugs into the engine."""
import asyncio

import numpy as np
import pandas as pd
import pytest
from pydantic import ValidationError

from app.scalper import engine as engine_mod
from app.scalper.config import ScalperConfig, ScalperConfigStore
from app.scalper.engine import ScalperEngine
from app.scalper.models import SignalResult
from app.scalper.sim_gateway import SimGateway
from app.scalper.styles import STYLE_FUNCS, STYLE_INFO, breakout_signal, range_reversal_signal, signal_for_style, spike_fade_signal
from app.scalper.signals import compute_signal

PIP = 0.0001


def frame(closes, wick=0.00003, opens=None):
    c = np.asarray(closes, dtype=float)
    o = np.r_[c[0], c[:-1]] if opens is None else np.asarray(opens, dtype=float)
    return pd.DataFrame({"open": o, "high": np.maximum(o, c) + wick, "low": np.minimum(o, c) - wick, "close": c})


def chop(n=60, amp=1.0, base=1.1000):
    """A tight sideways market: alternating +/- amp pips."""
    return [base + (amp * PIP if i % 2 else -amp * PIP) for i in range(n)]


def sine_range(n=70, amp=10.0, period=20, phase_end="bottom"):
    t = np.arange(n)
    # choose the phase so the LAST bar sits at the bottom (-1) or top (+1) of the cycle
    shift = (n - 1) - (0.75 if phase_end == "bottom" else 0.25) * period
    return 1.1000 + amp * PIP * np.sin(2 * np.pi * (t - shift) / period)


# ---------------------------------------------------------------- breakout
def test_breakout_buys_a_break_above_the_range_and_sells_below():
    base = chop(70)
    up = frame(base + [1.1011])
    up.iloc[-1, up.columns.get_loc("open")] = 1.1001
    r = breakout_signal(up, 0.8, PIP)
    assert r.side == "BUY" and r.score >= 50 and any("broke" in x for x in r.reasons)
    dn = frame(base + [1.0989])
    dn.iloc[-1, dn.columns.get_loc("open")] = 1.0999
    r = breakout_signal(dn, 0.8, PIP)
    assert r.side == "SELL" and r.score >= 50


def test_breakout_rewards_a_squeeze_before_the_break():
    loose = frame(chop(70) + [1.1011])
    tight = frame(chop(60) + [1.1000 + (0.2 if i % 2 else -0.2) * PIP for i in range(10)] + [1.1011])
    assert breakout_signal(tight, 0.8, PIP).score > breakout_signal(loose, 0.8, PIP).score


def test_breakout_stays_out_inside_the_range():
    r = breakout_signal(frame(chop(80)), 0.8, PIP)
    assert r.side is None and "inside" in r.reasons[0]


# ---------------------------------------------------------------- spike fade
def spike(direction):
    closes = chop(60) + [1.1000, 1.1000 + direction * 4 * PIP, 1.1000 + direction * 8 * PIP, 1.1000 + direction * 12 * PIP]
    df = frame(closes)
    last_open = 1.1000 + direction * 10 * PIP
    wick_extreme = 1.1000 + direction * 16 * PIP
    df.iloc[-1, df.columns.get_loc("open")] = last_open
    df.iloc[-1, df.columns.get_loc("high")] = max(closes[-1], wick_extreme, last_open) if direction > 0 else max(last_open, closes[-1]) + 0.00003
    df.iloc[-1, df.columns.get_loc("low")] = min(closes[-1], last_open) - 0.00003 if direction > 0 else min(closes[-1], wick_extreme)
    return df


def test_spike_fade_goes_against_the_spike():
    r = spike_fade_signal(spike(+1), 0.8, PIP)
    assert r.side == "SELL" and r.score >= 50 and any("spike" in x for x in r.reasons)
    r = spike_fade_signal(spike(-1), 0.8, PIP)
    assert r.side == "BUY" and r.score >= 50


def test_spike_fade_ignores_ordinary_moves():
    r = spike_fade_signal(frame(chop(80)), 0.8, PIP)
    assert r.side is None and "no spike" in r.reasons[0]


# ---------------------------------------------------------------- range reversal
def test_range_reversal_buys_the_bottom_and_sells_the_top_of_a_sideways_range():
    r = range_reversal_signal(frame(sine_range(phase_end="bottom")), 0.8, PIP)
    assert r.side == "BUY" and r.score >= 50 and any("bottom" in x for x in r.reasons)
    r = range_reversal_signal(frame(sine_range(phase_end="top")), 0.8, PIP)
    assert r.side == "SELL" and r.score >= 50 and any("top" in x for x in r.reasons)


def test_range_reversal_does_nothing_in_a_trend_or_the_middle():
    trend = frame(1.1000 + np.arange(70) * 1.5 * PIP)
    r = range_reversal_signal(trend, 0.8, PIP)
    assert r.side is None and ("trending" in r.reasons[0] or "left the range" in r.reasons[0])
    mid = sine_range(phase_end="bottom")
    mid = np.r_[mid[:-1], 1.1000]                         # last bar mid-range
    assert range_reversal_signal(frame(mid), 0.8, PIP).side is None


# ---------------------------------------------------------------- shared guards
@pytest.mark.parametrize("name", ["momentum", "breakout", "spike_fade", "range_reversal"])
def test_every_style_refuses_short_history_and_quiet_markets(name):
    fn = STYLE_FUNCS[name]
    assert fn(frame(chop(20)), 0.8, PIP).side is None
    assert fn(None, 0.8, PIP).side is None
    quiet = fn(frame([1.1 + 0.000001 * (i % 2) for i in range(100)], wick=0.0000005), 3.0, PIP)
    assert quiet.side is None
    r = fn(frame(chop(80)), 0.8, PIP)
    assert isinstance(r, SignalResult) and 0 <= r.score <= 100


@pytest.mark.parametrize("seed", range(8))
@pytest.mark.parametrize("name", ["breakout", "spike_fade", "range_reversal"])
def test_scores_stay_in_range_and_sides_are_valid_on_random_data(name, seed):
    rng = np.random.default_rng(seed)
    c = 1.1 + np.cumsum(rng.normal(0, 2e-4, 150))
    r = STYLE_FUNCS[name](frame(c, wick=1e-4), 0.8, PIP)
    assert r.side in (None, "BUY", "SELL") and 0 <= r.score <= 100


def test_registry_and_config():
    assert set(STYLE_FUNCS) == set(STYLE_INFO) and STYLE_FUNCS["momentum"] is compute_signal
    assert all(info["label"] and info["hint"] and 20 <= info["suggested_min_score"] <= 95 for info in STYLE_INFO.values())
    assert signal_for_style("breakout") is breakout_signal
    with pytest.raises(ValueError):
        signal_for_style("nope")
    assert ScalperConfig().entry_style == "momentum"               # default unchanged: existing users see no difference
    assert ScalperConfig(entry_style="spike_fade").entry_style == "spike_fade"
    with pytest.raises(ValidationError):
        ScalperConfig(entry_style="nope")


# ---------------------------------------------------------------- engine integration
@pytest.mark.asyncio
async def test_engine_uses_the_configured_style(tmp_path, monkeypatch):
    class Clock:
        t = 1_800_000_000.0
        def __call__(self): return self.t
    clock = Clock()
    store = ScalperConfigStore(tmp_path / "c.json")
    store.update({"loop_interval_ms": 5000, "cooldown_seconds": 0, "instruments": ["EUR/USD"], "min_signal_score": 40, "max_hold_seconds": 0,
                  "max_total_risk_pct": 10.0, "max_lot_per_trade": 5.0, "exit_mode": "fixed", "take_profit_pips": 6, "stop_loss_pips": 8,
                  "max_concurrent_positions": 1, "entry_style": "spike_fade"})
    seen = {"momentum": 0, "spike": 0}
    monkeypatch.setattr(engine_mod, "compute_signal", lambda *a: (seen.__setitem__("momentum", seen["momentum"] + 1), SignalResult("BUY", 90, 10.0, ["m"]))[1])
    monkeypatch.setitem(STYLE_FUNCS, "spike_fade", lambda *a: (seen.__setitem__("spike", seen["spike"] + 1), SignalResult("SELL", 90, 10.0, ["f"]))[1])
    gw = SimGateway(seed=5, clock=clock)
    eng = ScalperEngine(lambda c: gw, store, clock, tmp_path / "l.jsonl")
    await eng.start()
    await asyncio.sleep(0.05)
    assert seen["spike"] >= 1 and seen["momentum"] == 0
    assert [p.side for p in await gw.positions()] == ["SELL"]       # the configured style decided the direction
    await eng.stop(close_positions=True)
    store.update({"entry_style": "momentum"})                       # switching back changes behaviour live
    await eng.start()
    await asyncio.sleep(0.05)
    assert seen["momentum"] >= 1
    await eng.stop(close_positions=True)


@pytest.mark.asyncio
async def test_baseline_control_wraps_whichever_style_is_tested(monkeypatch):
    from app.scalper.backtest.data import synthetic
    from app.scalper.backtest.replay_gateway import Assumptions
    from app.scalper.backtest.runner import run_variant
    calls = {"n": 0}
    real = STYLE_FUNCS["range_reversal"]
    def counting(c, s, p):
        calls["n"] += 1
        return real(c, s, p)
    monkeypatch.setitem(STYLE_FUNCS, "range_reversal", counting)
    cfg = ScalperConfig(entry_style="range_reversal", min_signal_score=40, instruments=["EUR/USD"], session_max_loss_money=1e9,
                        session_max_loss_pct=0, max_consecutive_losses=0)
    d = synthetic(["EUR/USD"], 1, seed=3, mean_reverting=True)
    res = await run_variant(d, cfg, Assumptions(), "baseline", 3)
    assert calls["n"] > 100 and res["variant"] == "baseline"


# ---------------------------------------------------------------- slow, opt-in: do the styles find real patterns and ONLY real patterns?
import os

slow = pytest.mark.skipif(not os.environ.get("RUN_SLOW_TESTS"), reason="set RUN_SLOW_TESTS=1 (a few minutes)")

# (style, planted pattern, expected verdict). A style must win where its pattern exists, and lose on noise and on the wrong pattern.
CONTROLS = [
    ("range_reversal", "mean_reverting", "promising"), ("spike_fade", "mean_reverting", "promising"), ("breakout", "trending", "promising"),
    ("momentum", "mean_reverting", "not_viable"),
    ("range_reversal", "random", "not_viable"), ("spike_fade", "random", "not_viable"), ("breakout", "random", "not_viable"),
]


@slow
@pytest.mark.asyncio
@pytest.mark.parametrize("style,pattern,expected", CONTROLS)
async def test_styles_find_planted_patterns_and_nothing_in_noise(style, pattern, expected):
    from app.scalper.backtest.data import synthetic
    from app.scalper.backtest.metrics import build_verdict, summarize
    from app.scalper.backtest.replay_gateway import Assumptions
    from app.scalper.backtest.runner import run_variant
    d = synthetic(["EUR/USD"], 4, seed=11, trending=pattern == "trending", mean_reverting=pattern == "mean_reverting")
    cfg = ScalperConfig(entry_style=style, min_signal_score=60, session_max_loss_money=500, session_max_loss_pct=0, max_consecutive_losses=0,
                        max_concurrent_positions=4, max_positions_per_instrument=2, instruments=["EUR/USD"])
    sums = {v: summarize(await run_variant(d, cfg, Assumptions(slippage_pips=0.2, path_seed=11), v, 11)) for v in ("strategy", "baseline", "shuffle", "stress")}
    assert build_verdict(sums)["overall"] == expected, build_verdict(sums)


@slow
@pytest.mark.asyncio
@pytest.mark.parametrize("style", ["breakout", "spike_fade", "range_reversal"])
async def test_replay_never_flatters_a_style(style):
    """Zero-cost random walk with a KNOWN intrabar path: the OHLC-reconstructed replay must not do better than the truth.
    (Measured: breakout -0.047, spike_fade -0.015, range_reversal -0.017 per trade: pessimistic, never flattering.)"""
    from app.scalper.backtest.data import synthetic
    from app.scalper.backtest.replay_gateway import Assumptions
    from app.scalper.backtest.runner import run_variant
    cfg = ScalperConfig(entry_style=style, min_signal_score=60, cooldown_seconds=30, max_hold_seconds=600, exit_mode="atr", session_max_loss_money=1e9,
                        session_max_loss_pct=0, max_consecutive_losses=0, max_concurrent_positions=4, max_positions_per_instrument=2, instruments=["EUR/USD"])
    zero = Assumptions(spread_pips=0.0, slippage_pips=0.0)
    true_n, recon_n = [], []
    for seed in range(4, 10):
        d, p = synthetic(["EUR/USD"], 1, seed=seed, return_paths=True)
        true_n += [t["net"] for t in (await run_variant(d, cfg, zero, "strategy", 3, true_paths=p))["trades"]]
        recon_n += [t["net"] for t in (await run_variant(d, cfg, zero, "strategy", 3))["trades"]]
    assert len(true_n) > 400 and len(recon_n) > 400
    assert np.mean(recon_n) - np.mean(true_n) < 0.05      # the reconstruction must not make a strategy look better than reality
