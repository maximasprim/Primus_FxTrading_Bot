"""Trade history: journal joining, plain-English summaries, statistics, and the engine's recording of every trade."""
import asyncio
import json

import pytest

from app.scalper.engine import RUNNING, STOPPED, ScalperEngine
from app.scalper.journal import TradeJournal, infer_exit_reason, summarize, filter_trades, to_csv
from app.scalper.models import SessionPlan
from tests.test_scalper_engine import Harness


# ------------------------------------------------------------------ journal on its own
def _open(j, ticket="1", t=1000.0, side="BUY", **kw):
    j.record_open(ticket=ticket, opened_at=t, instrument="EUR/USD", side=side, volume=0.01, entry=1.1000, sl=1.0990,
                  tp=1.1020, pip_size=0.0001, spread_pips=0.8, score=75, reasons=["x"], entry_style="momentum",
                  preset="Custom", preset_id=None, session_id=1.0, account="1", demo=True, currency="USD", **kw)


def test_open_then_close_joins_into_one_plain_english_trade(tmp_path):
    j = TradeJournal(tmp_path / "h.jsonl")
    _open(j)
    [t] = j.trades(now=1100.0)
    assert t["status"] == "open" and "Still open" in t["summary"]
    assert j.record_close("1", closed_at=1252.0, exit=1.1020, pnl=0.20, pnl_estimated=False, reason="take_profit")
    [t] = j.trades(now=1300.0)
    assert t["status"] == "closed" and t["result"] == "win" and t["pips"] == 20.0
    assert t["duration_seconds"] == 252.0
    assert "Bought 0.01 lots of EUR/USD at 1.1" in t["summary"] and "Won 0.20 USD" in t["summary"] and "4m 12s" in t["summary"]


def test_close_is_written_once_and_orphan_closes_are_ignored(tmp_path):
    j = TradeJournal(tmp_path / "h.jsonl")
    _open(j)
    assert j.record_close("1", closed_at=2000.0, exit=1.099, pnl=-0.1, reason="stop_loss")
    assert not j.record_close("1", closed_at=2001.0, exit=1.0, pnl=-9, reason="broker")      # duplicate
    assert not j.record_close("999", closed_at=2000.0, exit=1.0, pnl=1, reason="broker")     # never opened here
    assert j.trades()[0]["pnl"] == -0.1


def test_a_reused_ticket_number_does_not_merge_two_trades(tmp_path):
    """The simulator restarts its ticket counter; two different trades with ticket '1000' must stay two trades."""
    j = TradeJournal(tmp_path / "h.jsonl")
    _open(j, "1000", t=1000.0)
    j.record_close("1000", closed_at=1100.0, exit=1.101, pnl=0.1, reason="take_profit")
    _open(j, "1000", t=5000.0)
    j2 = TradeJournal(tmp_path / "h.jsonl")                       # a fresh process reading the same file
    trades = j2.trades(now=5100.0)
    assert [t["status"] for t in trades] == ["open", "closed"]
    assert j2.record_close("1000", closed_at=5200.0, exit=1.099, pnl=-0.1, reason="stop_loss")
    assert [t["status"] for t in j2.trades(now=5300.0)] == ["closed", "closed"]


def test_a_half_written_last_line_does_not_break_reading(tmp_path):
    p = tmp_path / "h.jsonl"
    j = TradeJournal(p)
    _open(j)
    with p.open("a") as fh:
        fh.write('{"type": "close", "ticket": "1", "clo')       # crash mid-write
    assert len(TradeJournal(p).trades()) == 1


def test_trade_open_for_days_without_a_close_is_shown_as_not_recorded(tmp_path):
    j = TradeJournal(tmp_path / "h.jsonl")
    _open(j, t=1000.0)
    [t] = j.trades(now=1000.0 + 5 * 86400)
    assert t["status"] == "unknown" and "no record" in t["summary"]


@pytest.mark.parametrize("side,exit_px,sl,tp,expect", [
    ("BUY", 1.1020, 1.0990, 1.1020, "take_profit"),
    ("BUY", 1.0990, 1.0990, 1.1020, "stop_loss"),
    ("SELL", 1.0980, 1.1010, 1.0980, "take_profit"),
    ("BUY", 1.1005, 1.0990, 1.1020, "broker"),     # somewhere in the middle: closed by hand / broker
    ("BUY", None, 1.0990, 1.1020, "broker"),       # no exit price known
])
def test_exit_reason_is_inferred_from_where_the_trade_ended(side, exit_px, sl, tp, expect):
    assert infer_exit_reason(side, 1.1000 if side == "BUY" else 1.1000, sl, tp, exit_px, 0.0001) == expect


def test_stop_that_was_moved_in_profit_is_called_a_trailing_stop():
    assert infer_exit_reason("BUY", 1.1000, 1.1008, 1.1020, 1.1008, 0.0001, original_sl=1.0990) == "trailing_stop"


def test_summary_statistics_and_filters(tmp_path):
    j = TradeJournal(tmp_path / "h.jsonl")
    for i, (pnl, reason) in enumerate([(0.3, "take_profit"), (-0.2, "stop_loss"), (-0.2, "stop_loss"), (0.5, "take_profit")]):
        _open(j, str(i), t=1000.0 + i * 100)
        j.record_close(str(i), closed_at=1050.0 + i * 100, exit=1.1, pnl=pnl, reason=reason)
    trades = j.trades(now=2000.0)
    s = summarize(trades)
    assert s["closed"] == 4 and s["wins"] == 2 and s["losses"] == 2 and s["win_rate"] == 50.0
    assert s["net"] == 0.4 and s["profit_factor"] == 2.0 and s["longest_losing_streak"] == 2
    assert {r["reason"]: r["trades"] for r in s["by_reason"]} == {"take_profit": 2, "stop_loss": 2}
    assert len(filter_trades(trades, result="loss")) == 2
    assert len(filter_trades(trades, reason="take_profit")) == 2
    assert len(filter_trades(trades, since=1150.0)) == 2
    csv_text = to_csv(trades)
    assert csv_text.splitlines()[0].startswith("opened_at_utc,closed_at_utc,ticket") and len(csv_text.splitlines()) == 5


# ------------------------------------------------------------------ the engine records what it does
def _history(tmp_path):
    return TradeJournal(tmp_path / "scalper_history.jsonl").trades()


@pytest.mark.asyncio
async def test_engine_records_open_and_take_profit_close(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch, instruments=["EUR/USD"], max_concurrent_positions=1, cooldown_seconds=600)
    eng = await h.started()
    [t] = _history(tmp_path)
    assert t["status"] == "open" and t["instrument"] == "EUR/USD" and t["score"] == 90 and t["preset"] == "Custom"
    assert t["rules"]["max_hold_seconds"] == 0 and t["planned_tp_pips"] == 6
    h.move("EUR/USD", 7)                       # past the 6-pip target
    await h.cycle(); await h.cycle()
    [t] = _history(tmp_path)
    assert t["status"] == "closed" and t["exit_reason"] == "take_profit" and t["result"] == "win" and t["pnl"] > 0
    assert t["pips"] == pytest.approx(6.0, abs=0.6)
    await eng.stop(close_positions=True)


@pytest.mark.asyncio
async def test_engine_records_stop_loss(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch, instruments=["EUR/USD"], max_concurrent_positions=1, cooldown_seconds=600)
    eng = await h.started()
    h.move("EUR/USD", -9)                      # past the 8-pip stop
    await h.cycle(); await h.cycle()
    [t] = _history(tmp_path)
    assert t["exit_reason"] == "stop_loss" and t["result"] == "loss"
    await eng.stop(close_positions=True)


@pytest.mark.asyncio
async def test_stop_and_close_all_is_recorded_with_real_pnl(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch, instruments=["EUR/USD"], max_concurrent_positions=1, cooldown_seconds=600)
    eng = await h.started()
    h.move("EUR/USD", 2)
    await eng.stop(close_positions=True)
    [t] = _history(tmp_path)
    assert t["status"] == "closed" and t["exit_reason"] == "user_stop" and t["pnl"] > 0 and not t["pnl_estimated"]
    assert "Closed when you pressed Stop" in t["summary"]


@pytest.mark.asyncio
async def test_time_stop_is_recorded_as_such(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch, instruments=["EUR/USD"], max_concurrent_positions=1, cooldown_seconds=600, max_hold_seconds=1)
    eng = await h.started()
    h.clock.advance(2)       # just past the limit (a longer jump lets the simulator's random walk hit TP/SL first)
    await h.cycle(); await h.cycle()
    [t] = _history(tmp_path)
    assert t["exit_reason"] == "time_stop"
    await eng.stop(close_positions=True)


@pytest.mark.asyncio
async def test_a_trade_that_closed_while_the_bot_was_off_is_filled_in_on_next_start(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch, instruments=["EUR/USD"], max_concurrent_positions=1, cooldown_seconds=600)
    eng = await h.started()
    await eng.stop(close_positions=False)      # trade stays open at the broker with its own SL/TP
    assert _history(tmp_path)[0]["status"] == "open"
    h.clock.advance(300)
    h.move("EUR/USD", 7)                       # take-profit gets hit while the bot is off
    await h.gw.account()
    eng2 = ScalperEngine(lambda c: h.gw, h.store, h.clock, tmp_path / "log.jsonl")
    await eng2.start()
    await asyncio.sleep(0.05)
    t = [x for x in _history(tmp_path) if x["status"] == "closed"]
    assert t and t[-1]["exit_reason"] == "take_profit"
    await eng2.stop(close_positions=True)


@pytest.mark.asyncio
async def test_a_broken_history_file_never_stops_trading(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch, instruments=["EUR/USD"], max_concurrent_positions=1, cooldown_seconds=600)
    h.eng.journal = TradeJournal(tmp_path)         # a DIRECTORY, so every write fails
    eng = await h.started()
    assert eng.state == RUNNING and len(await h.gw.positions()) == 1
    await eng.stop(close_positions=True)


def test_prices_are_shown_at_the_pairs_precision(tmp_path):
    j = TradeJournal(tmp_path / "h.jsonl")
    j.record_open(ticket="1", opened_at=1000.0, instrument="EUR/USD", side="SELL", volume=0.01, entry=1.0795019983688134,
                  sl=1.08111111111, tp=1.07777777777, pip_size=0.0001, currency="USD")
    j.record_close("1", closed_at=1100.0, exit=1.07777777777, pnl=0.2, reason="take_profit")
    [t] = j.trades(now=1200.0)
    assert t["entry"] == 1.0795 and t["sl"] == 1.08111 and t["exit"] == 1.07778 and "at 1.0795." in t["summary"]
    gold = TradeJournal(tmp_path / "g.jsonl")
    gold.record_open(ticket="2", opened_at=1.0, instrument="XAU/USD", side="BUY", volume=0.01, entry=4123.456789, pip_size=0.1)
    assert gold.trades(now=2.0)[0]["entry"] == 4123.46
