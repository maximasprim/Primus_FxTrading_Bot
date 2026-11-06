"""The replay gateway: no look-ahead, pessimistic fills, cost arithmetic, and the measured replay bias."""
import asyncio

import numpy as np
import pandas as pd
import pytest

from app.scalper.backtest.data import synthetic
from app.scalper.backtest.replay_gateway import BAR_STEPS, EVAL_STEPS, Assumptions, ReplayGateway, _bridge_paths

T0 = pd.Timestamp("2025-01-06 00:00", tz="UTC")


def bars(rows, inst="EUR/USD"):
    """rows: (open, high, low, close) per minute."""
    idx = pd.date_range(T0, periods=len(rows), freq="1min")
    return pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=idx)


def flat_path(o, h, l, c, first="down"):
    """An explicit 60-step path: open -> first extreme -> second extreme -> close."""
    e1, e2 = (l, h) if first == "down" else (h, l)
    return np.concatenate([np.linspace(o, e1, 20), np.linspace(e1, e2, 20)[1:], np.linspace(e2, c, 22)[1:]])[:BAR_STEPS]


def make(rows, paths=None, **a):
    data = {"EUR/USD": bars(rows)}
    tp = {"EUR/USD": np.array(paths)} if paths is not None else None
    return ReplayGateway(data, Assumptions(spread_pips=a.pop("spread_pips", 0.0), slippage_pips=a.pop("slippage_pips", 0.0), **a), tp)


def step(gw, i, phases=(0, 1, 2, 3)):
    gw.begin_bar(i)
    for p in phases:
        gw.set_phase(p)
    gw.end_bar()


# ---------------------------------------------------------------- path reconstruction
def test_bridge_reproduces_real_ohlc_exactly():
    d = synthetic(["EUR/USD", "XAU/USD"], 2, seed=4)
    for df in d.values():
        o, h, l, c = (df[k].to_numpy() for k in ("open", "high", "low", "close"))
        y = _bridge_paths(o, h, l, c, np.random.default_rng(0))
        assert np.array_equal(y[:, 0], o) and np.array_equal(y[:, -1], c)
        assert np.allclose(y.max(axis=1), h) and np.allclose(y.min(axis=1), l)
        assert not np.isnan(y).any()


def test_bridge_handles_degenerate_bars():
    o = np.array([1.1, 1.1, 1.1, 1.1, 1.1]); c = np.array([1.1, 1.1, 1.1002, 1.0998, 1.1])
    h = np.array([1.1, 1.1003, 1.1002, 1.1, 1.1004]); l = np.array([1.1, 1.1, 1.1, 1.0998, 1.0996])
    y = _bridge_paths(o, h, l, c, np.random.default_rng(1))
    assert not np.isnan(y).any()
    assert np.allclose(y.max(axis=1), h) and np.allclose(y.min(axis=1), l)
    assert np.array_equal(y[:, 0], o) and np.array_equal(y[:, -1], c)


def test_timeline_is_independent_of_pandas_datetime_resolution():
    """pandas 2 stores ns, pandas 3 stores us. A resolution-dependent conversion once collapsed 4,320 bars to 260."""
    df = synthetic(["EUR/USD"], 1, seed=1)["EUR/USD"]
    a = ReplayGateway({"EUR/USD": df.set_axis(df.index.as_unit("ns"))})
    b = ReplayGateway({"EUR/USD": df.set_axis(df.index.as_unit("us"))})
    assert len(a.timeline) == len(df) == len(b.timeline)
    assert np.array_equal(a.timeline, b.timeline)
    assert a.timeline[0] == int(T0.timestamp())


# ---------------------------------------------------------------- look-ahead
@pytest.mark.asyncio
async def test_no_lookahead_candles_only_show_what_has_happened():
    df = synthetic(["EUR/USD"], 1, seed=2)["EUR/USD"]
    gw = ReplayGateway({"EUR/USD": df})
    o, h, l, c = (df[k].to_numpy() for k in ("open", "high", "low", "close"))
    for i in range(70, 110):
        gw.begin_bar(i)
        seen_high = -np.inf
        for phase in range(4):
            gw.set_phase(phase)
            cs = await gw.candles("EUR/USD", 120)
            last = cs.iloc[-1]
            assert np.array_equal(cs["close"].to_numpy()[:-1], c[i - 119:i]) if i >= 119 else True
            assert last["open"] == o[i]
            assert last["high"] <= h[i] + 1e-12 and last["low"] >= l[i] - 1e-12
            assert last["high"] >= seen_high            # the running high never shrinks
            seen_high = last["high"]
            if phase == 0:
                assert last["high"] == last["low"] == last["close"] == o[i]  # at the open, nothing else is known yet
        gw.end_bar()
        # the future high/low must NOT be revealed before the bar plays out: at the last evaluation (t=45s)
        # it's still allowed to be short of the full range
        assert gw.series["EUR/USD"].path[i].max() == pytest.approx(h[i])


def test_engine_runs_at_fixed_times_not_at_the_extremes():
    """Evaluating only at a bar's own high/low silently penalised momentum entries (-0.07/trade measured)."""
    assert EVAL_STEPS == (0, 15, 30, 45)
    df = synthetic(["EUR/USD"], 1, seed=3)["EUR/USD"]
    gw = ReplayGateway({"EUR/USD": df})
    path = gw.series["EUR/USD"].path
    at_extreme = np.mean([(abs(path[:, s] - df["high"].to_numpy()) < 1e-12) | (abs(path[:, s] - df["low"].to_numpy()) < 1e-12) for s in EVAL_STEPS[1:]])
    assert at_extreme < 0.12  # random extreme times: only a small share of evaluations land exactly on one


# ---------------------------------------------------------------- fills
@pytest.mark.asyncio
async def test_stop_wins_when_a_trade_open_at_bar_start_could_hit_both():
    rows = [(1.1000, 1.1001, 1.0999, 1.1000), (1.1000, 1.1020, 1.0980, 1.1000)]
    paths = [flat_path(*rows[0]), flat_path(*rows[1], first="up")]  # this path would reach the TARGET first
    gw = make(rows, paths)
    step(gw, 0, phases=(0,))
    out = await gw.send_market("EUR/USD", "BUY", 0.01, sl=1.0990, tp=1.1010)
    assert out.ok
    gw.set_phase(1); gw.set_phase(2); gw.set_phase(3); gw.end_bar()
    gw.begin_bar(1)
    assert [t["reason"] for t in gw.trades] == ["stop-loss"] and gw.trades[0]["net"] < 0


@pytest.mark.asyncio
async def test_target_fills_at_the_level_when_only_it_is_reachable():
    rows = [(1.1000, 1.1001, 1.0999, 1.1000), (1.1000, 1.1015, 1.0996, 1.1010)]
    gw = make(rows, [flat_path(*rows[0]), flat_path(*rows[1], first="down")])
    step(gw, 0, phases=(0,))
    await gw.send_market("EUR/USD", "BUY", 0.01, sl=1.0990, tp=1.1010)
    gw.set_phase(1); gw.set_phase(2); gw.set_phase(3); gw.end_bar()
    gw.begin_bar(1)
    for p in range(4):
        gw.set_phase(p)
    gw.end_bar()
    t = gw.trades[0]
    assert t["reason"] == "take-profit" and t["exit"] == pytest.approx(1.1010) and t["net"] == pytest.approx(1.00)  # 10 pips x $0.10/pip at 0.01 lot


@pytest.mark.asyncio
async def test_gap_through_a_stop_fills_at_the_worse_open():
    rows = [(1.1000, 1.1001, 1.0999, 1.1000), (1.0950, 1.0955, 1.0948, 1.0950)]
    gw = make(rows, [flat_path(*rows[0]), flat_path(*rows[1])])
    step(gw, 0, phases=(0,))
    await gw.send_market("EUR/USD", "BUY", 0.01, sl=1.0990, tp=1.1100)
    gw.set_phase(1); gw.set_phase(2); gw.set_phase(3); gw.end_bar()
    gw.begin_bar(1)
    t = gw.trades[0]
    assert t["reason"] == "stop-loss" and t["exit"] == pytest.approx(1.0950) and t["exit"] < 1.0990  # not at the 1.0990 stop


@pytest.mark.asyncio
async def test_costs_spread_slippage_commission():
    rows = [(1.1000, 1.1000, 1.1000, 1.1000)] * 2
    gw = make(rows, [np.full(BAR_STEPS, 1.1)] * 2, spread_pips=1.0, slippage_pips=0.5, commission_per_lot=7.0)
    step(gw, 0, phases=(0,))
    out = await gw.send_market("EUR/USD", "BUY", 0.01, sl=1.0900, tp=1.1100)
    assert out.price == pytest.approx(1.1 + 0.00005 + 0.00005)              # ask + 0.5 pip slippage
    await gw.close(out.ticket, reason="manual")
    t = gw.trades[0]
    assert t["exit"] == pytest.approx(1.1 - 0.00005 - 0.00005)               # bid - 0.5 pip slippage
    assert t["net"] == pytest.approx(-2.0 * 0.10 - 0.07)                     # 2 pips x $0.10 + $0.07 commission
    assert t["gross"] == pytest.approx(0.0, abs=1e-9)                        # price didn't move: every cent lost is a cost
    assert t["cost"] == pytest.approx(0.27)                                  # spread 0.10 + slippage 0.10 + commission 0.07
    assert gw.balance == pytest.approx(10_000 - 0.27)


@pytest.mark.asyncio
async def test_cost_multiplier_scales_every_cost():
    rows = [(1.1, 1.1, 1.1, 1.1)] * 2
    nets = []
    for m in (1.0, 2.0):
        gw = make(rows, [np.full(BAR_STEPS, 1.1)] * 2, spread_pips=1.0, slippage_pips=0.5, commission_per_lot=7.0, cost_multiplier=m)
        step(gw, 0, phases=(0,))
        o = await gw.send_market("EUR/USD", "BUY", 0.01, sl=1.09, tp=1.11)
        await gw.close(o.ticket)
        nets.append(gw.trades[0]["net"])
    assert nets[1] == pytest.approx(2 * nets[0])


@pytest.mark.asyncio
async def test_money_conversion_for_jpy_and_gold():
    rows = [(150.0, 150.0, 150.0, 150.0)] * 2
    gw = ReplayGateway({"USD/JPY": bars(rows)}, Assumptions(spread_pips=0.0, slippage_pips=0.0), {"USD/JPY": np.full((2, BAR_STEPS), 150.0)})
    assert await gw.loss_money("USD/JPY", "BUY", 1.0, 150.00, 149.99) == pytest.approx(0.01 * 100_000 / 150.0)   # 1 pip = ~$6.67
    gw = ReplayGateway({"XAU/USD": bars([(2350.0,) * 4] * 2)}, Assumptions(spread_pips=0.0), {"XAU/USD": np.full((2, BAR_STEPS), 2350.0)})
    assert await gw.loss_money("XAU/USD", "BUY", 1.0, 2350.0, 2349.9) == pytest.approx(10.0)                     # 1 pip (0.10) = $10/lot
    assert await gw.loss_money("EUR/USD" if False else "XAU/USD", "SELL", 0.01, 2350.0, 2351.0) == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_pair_with_no_candle_this_minute_is_treated_as_closed():
    idx_a = pd.date_range(T0, periods=3, freq="1min")
    a = pd.DataFrame([(1.1, 1.1, 1.1, 1.1)] * 3, columns=["open", "high", "low", "close"], index=idx_a)
    b = a.iloc[[0, 2]].copy()  # gold-style break: no bar at minute 1
    gw = ReplayGateway({"EUR/USD": a, "XAU/USD": b.assign(open=2350.0, high=2350.0, low=2350.0, close=2350.0)})
    gw.begin_bar(1)
    gw.set_phase(0)
    assert await gw.tick("EUR/USD") is not None and await gw.tick("XAU/USD") is None


# ---------------------------------------------------------------- measured replay bias
@pytest.mark.asyncio
async def test_replay_bias_stays_small():
    """On data whose TRUE intrabar path is known, in a zero-cost world a momentum strategy must average ~0 per trade.
    The OHLC-reconstructed replay may be a little pessimistic (measured ~-0.03/trade) -- pin that it stays small."""
    from app.scalper.backtest.runner import run_variant
    from app.scalper.config import ScalperConfig
    cfg = ScalperConfig(min_signal_score=65, cooldown_seconds=30, max_hold_seconds=600, exit_mode="atr", session_max_loss_money=1e9,
                        session_max_loss_pct=0, max_consecutive_losses=0, max_concurrent_positions=4, max_positions_per_instrument=2,
                        instruments=["EUR/USD"])
    zero = Assumptions(spread_pips=0.0, slippage_pips=0.0)
    true_n, recon_n = [], []
    for seed in (4, 5, 6):
        d, p = synthetic(["EUR/USD"], 1, seed=seed, return_paths=True)
        true_n += [t["net"] for t in (await run_variant(d, cfg, zero, "strategy", 3, true_paths=p))["trades"]]
        recon_n += [t["net"] for t in (await run_variant(d, cfg, zero, "strategy", 3))["trades"]]
    true_n, recon_n = np.array(true_n), np.array(recon_n)
    assert len(true_n) > 300 and len(recon_n) > 300
    assert abs(true_n.mean()) < 3.5 * true_n.std(ddof=1) / np.sqrt(len(true_n))     # machinery unbiased on the true path
    assert recon_n.mean() > -0.10                                                    # reconstruction bias stays small


# ---------------------------------------------------------------- broker-measured pip value (cent accounts etc.)
@pytest.mark.asyncio
async def test_measured_pip_value_overrides_the_standard_contract_assumption():
    rows = [(1.1, 1.1, 1.1, 1.1)] * 2
    std = make(rows, [np.full(BAR_STEPS, 1.1)] * 2)
    cent = ReplayGateway({"EUR/USD": bars(rows)}, Assumptions(spread_pips=0.0, slippage_pips=0.0, pip_values={"EUR/USD": 0.1}),
                         {"EUR/USD": np.full((2, BAR_STEPS), 1.1)})
    assert await std.loss_money("EUR/USD", "BUY", 1.0, 1.1000, 1.0990) == pytest.approx(100.0)   # 10 pips x $10
    assert await cent.loss_money("EUR/USD", "BUY", 1.0, 1.1000, 1.0990) == pytest.approx(1.0)    # 10 pips x 0.1


@pytest.mark.asyncio
async def test_cent_backtest_is_exactly_one_hundredth_of_the_standard_one():
    """Same trades, same prices: only the broker's value of a pip changes. P&L and costs must scale by exactly that factor."""
    from app.scalper.backtest.runner import run_variant
    from app.scalper.config import ScalperConfig
    d = synthetic(["EUR/USD"], 1, seed=6)
    cfg = ScalperConfig(min_signal_score=65, cooldown_seconds=30, max_hold_seconds=600, exit_mode="atr", session_max_loss_money=1e9,
                        session_max_loss_pct=0, max_consecutive_losses=0, max_concurrent_positions=2, max_positions_per_instrument=2,
                        max_total_risk_pct=10.0, instruments=["EUR/USD"])
    a = await run_variant(d, cfg, Assumptions(start_balance=1e6), "strategy", 3)
    b = await run_variant(d, cfg, Assumptions(start_balance=1e6, pip_values={"EUR/USD": 0.1}), "strategy", 3)
    assert len(a["trades"]) == len(b["trades"]) > 20
    assert [t["entry"] for t in a["trades"]] == [t["entry"] for t in b["trades"]]
    assert sum(t["net"] for t in b["trades"]) == pytest.approx(sum(t["net"] for t in a["trades"]) / 100, rel=1e-9)
    assert sum(t["cost"] for t in b["trades"]) == pytest.approx(sum(t["cost"] for t in a["trades"]) / 100, rel=1e-9)
