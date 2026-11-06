"""Engine behaviour on the simulator: limits, halts, safety refusals."""
import asyncio
from dataclasses import replace

import pytest

from app.scalper import engine as engine_mod
from app.scalper.config import ScalperConfigStore
from app.scalper.engine import HALTED, RUNNING, STOPPED, ScalperEngine, ScalperStartError
from app.scalper.models import OrderOutcome, SignalResult
from app.scalper.sim_gateway import SimGateway


class Clock:
    def __init__(self):
        self.t = 1_800_000_000.0

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


class Harness:
    def __init__(self, tmp_path, monkeypatch, **cfg):
        self.clock = Clock()
        self.gw = SimGateway(seed=5, clock=self.clock)
        self.store = ScalperConfigStore(tmp_path / "c.json")
        self.store.update({"loop_interval_ms": 5000, "cooldown_seconds": 0, "instruments": ["EUR/USD", "GBP/USD", "XAU/USD"],
                           "min_signal_score": 40, "max_hold_seconds": 0, "max_total_risk_pct": 10.0, "max_lot_per_trade": 5.0,
                           "exit_mode": "fixed", "take_profit_pips": 6, "stop_loss_pips": 8, **cfg})
        self.signal = SignalResult("BUY", 90, 10.0, ["forced"])
        monkeypatch.setattr(engine_mod, "compute_signal", lambda *a, **k: self.signal)
        self.eng = ScalperEngine(lambda c: self.gw, self.store, self.clock, tmp_path / "log.jsonl")

    async def started(self):
        await self.eng.start()
        await asyncio.sleep(0.05)  # let the first cycle run
        return self.eng

    async def cycle(self):
        await self.eng._cycle(self.store.get())

    def move(self, instrument, pips):
        m = self.gw._markets[instrument]
        m.mid += pips * m.pip


@pytest.mark.asyncio
async def test_never_exceeds_max_concurrent(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch, max_concurrent_positions=3, max_positions_per_instrument=3, entries_per_signal=5)
    eng = await h.started()
    for _ in range(5):
        await h.cycle()
    assert len(await h.gw.positions()) == 3
    await eng.stop(close_positions=True)


@pytest.mark.asyncio
async def test_entries_per_signal_opens_several_with_staggered_targets(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch, max_concurrent_positions=6, max_positions_per_instrument=3, entries_per_signal=3,
                instruments=["EUR/USD"])
    eng = await h.started()
    pos = sorted(await h.gw.positions(), key=lambda p: p.ticket)
    assert len(pos) == 3
    tp_dist = [abs(p.tp - p.price_open) for p in pos]
    assert tp_dist[0] < tp_dist[1] < tp_dist[2]
    assert all(p.sl and p.tp for p in pos)  # broker-side protection on every trade
    await eng.stop(close_positions=True)


@pytest.mark.asyncio
async def test_per_instrument_cap(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch, max_concurrent_positions=10, max_positions_per_instrument=2, entries_per_signal=5,
                instruments=["EUR/USD"])
    eng = await h.started()
    for _ in range(3):
        await h.cycle()
    assert len(await h.gw.positions()) == 2
    await eng.stop(close_positions=True)


@pytest.mark.asyncio
async def test_low_score_signal_is_ignored(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch, min_signal_score=70)
    h.signal = SignalResult("BUY", 60, 10.0, ["weak"])
    eng = await h.started()
    await h.cycle()
    assert await h.gw.positions() == []
    await eng.stop()


@pytest.mark.asyncio
async def test_session_profit_target_halts_and_flattens(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch, session_profit_target_money=10.0, max_concurrent_positions=1,
                instruments=["EUR/USD"], fixed_lot=1.0)
    eng = await h.started()
    h.move("EUR/USD", 3)  # +3 pips on 1 lot ~ +$30 (below the 6-pip TP so it stays open)
    await h.cycle()
    await asyncio.sleep(0.05)
    assert eng.state == HALTED and "profit target" in eng.halt_reason
    assert await h.gw.positions() == []  # flatten_on_halt


@pytest.mark.asyncio
async def test_session_loss_limit_halts(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch, session_max_loss_money=15.0, max_concurrent_positions=1,
                instruments=["EUR/USD"], fixed_lot=1.0, stop_loss_pips=50)
    eng = await h.started()
    h.move("EUR/USD", -3)  # about -$30 floating
    await h.cycle()
    await asyncio.sleep(0.05)
    assert eng.state == HALTED and "loss limit" in eng.halt_reason
    assert await h.gw.positions() == []


@pytest.mark.asyncio
async def test_basket_take_profit_closes_all_but_keeps_running(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch, basket_take_profit_money=20.0, max_concurrent_positions=2,
                max_positions_per_instrument=2, entries_per_signal=2, instruments=["EUR/USD"], fixed_lot=1.0,
                take_profit_pips=50, stop_loss_pips=50)
    eng = await h.started()
    assert len(await h.gw.positions()) == 2
    h.signal = SignalResult(None, 0, 10.0, [])  # stop re-entering so we can see the basket close
    h.move("EUR/USD", 2)  # 2 trades x 2 pips x $10 = ~ +$40
    await h.cycle()
    assert await h.gw.positions() == []
    assert eng.state == RUNNING
    await eng.stop()


@pytest.mark.asyncio
async def test_tiny_risk_budget_skips_instead_of_rounding_up(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch, lot_mode="risk_pct", risk_per_trade_pct=0.05, instruments=["EUR/USD"], stop_loss_pips=100)
    h.gw._balance = 1_000.0  # 0.05% = $0.50 risk; 100-pip stop => 0.0005 lot < 0.01 minimum
    eng = await h.started()
    await h.cycle()
    assert await h.gw.positions() == []
    assert "below the broker minimum" in eng.signals["EUR/USD"]["blocked"]
    await eng.stop()


@pytest.mark.asyncio
async def test_total_open_risk_cap(tmp_path, monkeypatch):
    # 1 lot with a 8-pip stop risks ~$80; cap = 0.5% of $10,000 = $50 -> blocked
    h = Harness(tmp_path, monkeypatch, fixed_lot=1.0, max_total_risk_pct=0.5, instruments=["EUR/USD"])
    eng = await h.started()
    await h.cycle()
    assert await h.gw.positions() == []
    assert "exceed cap" in eng.signals["EUR/USD"]["blocked"]
    await eng.stop()


@pytest.mark.asyncio
async def test_spread_too_wide_blocks(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch, max_spread_pips=0.3, instruments=["EUR/USD"])
    eng = await h.started()
    await h.cycle()
    assert await h.gw.positions() == []
    assert "spread" in eng.signals["EUR/USD"]["blocked"]
    await eng.stop()


@pytest.mark.asyncio
async def test_target_must_beat_spread(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch, take_profit_pips=1.5, min_tp_to_spread_ratio=3.0, instruments=["EUR/USD"])
    eng = await h.started()
    await h.cycle()
    assert await h.gw.positions() == []
    assert "spread" in eng.signals["EUR/USD"]["blocked"]
    await eng.stop()


@pytest.mark.asyncio
async def test_emergency_stop_blocks_new_entries(tmp_path, monkeypatch):
    from app.core.trading_mode import trading_mode_manager
    h = Harness(tmp_path, monkeypatch)
    trading_mode_manager.stop_new_trades("test")
    try:
        eng = await h.started()
        await h.cycle()
        assert await h.gw.positions() == []
        await eng.stop()
    finally:
        trading_mode_manager.clear_emergency_stop("test")


@pytest.mark.asyncio
async def test_time_stop_and_trade_profit_target(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch, max_hold_seconds=60, trade_profit_target_money=15.0, instruments=["EUR/USD"],
                max_concurrent_positions=2, max_positions_per_instrument=2, entries_per_signal=2, fixed_lot=1.0,
                take_profit_pips=50, stop_loss_pips=50)
    eng = await h.started()
    pos = sorted(await h.gw.positions(), key=lambda p: p.ticket)
    assert len(pos) == 2
    h.signal = SignalResult(None, 0, 10.0, [])
    h.move("EUR/USD", 3)  # ~+$22 each after the spread, above the $15 target
    await h.cycle()
    assert await h.gw.positions() == []
    await eng.stop()


@pytest.mark.asyncio
async def test_refuses_real_money_account_unless_allowed(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch)
    real = SimGateway(seed=1, clock=h.clock)
    orig = real.account

    async def live_account():
        return replace(await orig(), is_demo=False)

    real.account = live_account
    eng = ScalperEngine(lambda c: real, h.store, h.clock, tmp_path / "l.jsonl")
    with pytest.raises(ScalperStartError, match="REAL-money"):
        await eng.start()
    h.store.update({"allow_live_account": True})
    await eng.start()
    assert eng.state == RUNNING
    await eng.stop()


@pytest.mark.asyncio
async def test_refuses_when_algo_trading_off(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch)
    orig = h.gw.account

    async def no_algo():
        return replace(await orig(), algo_trading_enabled=False)

    h.gw.account = no_algo
    with pytest.raises(ScalperStartError, match="Algo Trading"):
        await h.eng.start()
    assert h.eng.state == STOPPED


@pytest.mark.asyncio
async def test_netting_account_never_stacks(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch, max_concurrent_positions=5, max_positions_per_instrument=5, entries_per_signal=5,
                instruments=["EUR/USD"])
    orig = h.gw.account

    async def netting():
        return replace(await orig(), hedging=False)

    h.gw.account = netting
    eng = await h.started()
    for _ in range(3):
        await h.cycle()
    assert len(await h.gw.positions()) == 1
    await eng.stop(close_positions=True)


@pytest.mark.asyncio
async def test_only_scalper_positions_are_closed_by_stop(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch, instruments=["EUR/USD"])
    eng = await h.started()
    assert len(await h.gw.positions()) >= 1
    await eng.stop(close_positions=True)
    assert await h.gw.positions() == []
    assert eng.state == STOPPED


@pytest.mark.asyncio
async def test_trailing_stop_ratchets_only_forward(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch, instruments=["EUR/USD"], trailing_enabled=True, trailing_start_pips=2,
                trailing_distance_pips=1.5, trailing_step_pips=0.5, take_profit_pips=100, stop_loss_pips=20,
                max_concurrent_positions=1)
    eng = await h.started()
    p0 = (await h.gw.positions())[0]
    h.signal = SignalResult(None, 0, 10.0, [])
    h.move("EUR/USD", 5)
    h.clock.advance(2)
    await h.cycle()
    p1 = (await h.gw.positions())[0]
    assert p1.sl > p0.sl
    h.move("EUR/USD", -0.5)  # small pullback (stop not hit) must not lower the stop
    h.clock.advance(2)
    await h.cycle()
    assert (await h.gw.positions())[0].sl == p1.sl
    h.move("EUR/USD", -3)  # a real reversal is allowed to hit the trailed stop, locking in profit
    await h.cycle()
    assert await h.gw.positions() == []
    assert sum(d.profit for d in await h.gw.deals_since(0)) > 0
    await eng.stop(close_positions=True)


@pytest.mark.asyncio
async def test_max_lot_per_trade_caps_volume(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch, fixed_lot=2.0, max_lot_per_trade=0.5, instruments=["EUR/USD"], max_concurrent_positions=1)
    eng = await h.started()
    assert [p.volume for p in await h.gw.positions()] == [0.5]
    await eng.stop(close_positions=True)


# ---------------------------------------------------------------- rejected orders: back off, then stop
class RejectingGateway(SimGateway):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.attempts = 0

    async def send_market(self, instrument, side, volume, sl, tp):
        self.attempts += 1
        return OrderOutcome(False, reason="order_send returned None: (-2, 'Unnamed arguments not allowed')")


@pytest.mark.asyncio
async def test_rejected_orders_back_off_instead_of_retrying_every_cycle(tmp_path, monkeypatch):
    """Reported: a rejected order was retried every 0.5s, thousands of times."""
    h = Harness(tmp_path, monkeypatch, instruments=["EUR/USD"], max_concurrent_positions=1)
    h.gw = RejectingGateway(seed=5, clock=h.clock)
    h.eng = ScalperEngine(lambda c: h.gw, h.store, h.clock, tmp_path / "log.jsonl")
    eng = await h.started()
    assert h.gw.attempts == 1
    for _ in range(20):                       # 20 more cycles within the 5s back-off window
        h.clock.advance(0.2)
        await h.cycle()
    assert h.gw.attempts == 1                 # no hammering
    assert "backing off" in eng.signals["EUR/USD"]["blocked"]
    h.clock.advance(6)                        # back-off over -> exactly one more try
    await h.cycle()
    assert h.gw.attempts == 2
    rejects = lambda: [e for e in eng.events if "rejected" in e["message"]]
    assert len(rejects()) == 1                # identical rejections are logged once per 30s, not once per attempt
    h.clock.advance(40)                       # ...but a persisting problem is logged again later
    await h.cycle()
    assert h.gw.attempts == 3 and len(rejects()) == 2
    await eng.stop()


@pytest.mark.asyncio
async def test_bot_stops_itself_after_repeated_rejections(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch, instruments=["EUR/USD"], max_concurrent_positions=1)
    h.gw = RejectingGateway(seed=5, clock=h.clock)
    h.eng = ScalperEngine(lambda c: h.gw, h.store, h.clock, tmp_path / "log.jsonl")
    eng = await h.started()
    for _ in range(12):
        h.clock.advance(200)                  # longer than the maximum back-off each time
        await h.cycle()
        await asyncio.sleep(0.02)
        if eng.state != RUNNING:
            break
    assert eng.state == HALTED
    assert "rejected 8 orders in a row" in eng.halt_reason and "Unnamed arguments" in eng.halt_reason
    assert h.gw.attempts == 8 and await h.gw.positions() == []


@pytest.mark.asyncio
async def test_one_success_resets_the_rejection_streak(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch, instruments=["EUR/USD"], max_concurrent_positions=1)
    eng = await h.started()
    eng._reject_streak = 7
    eng._inst_fails["EUR/USD"] = 3
    for p in await h.gw.positions():
        await h.gw.close(p.ticket)
    h.clock.advance(300)
    await h.cycle()                           # SimGateway accepts the order
    assert eng._reject_streak == 0 and eng._inst_fails["EUR/USD"] == 0
    await eng.stop(close_positions=True)
