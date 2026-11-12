"""Scheduled sessions, session plans (duration / profit target) and trades keeping their own exit rules."""
import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app.scalper.engine import RUNNING, STOPPED, ScalperEngine, ScalperStartError
from app.scalper.models import SessionPlan
from app.scalper.schedule import GRACE_SECONDS, ScheduleConfig, ScheduleSlot, ScheduleStore, Scheduler, next_run
from tests.test_scalper_engine import Harness

NAI = ZoneInfo("Africa/Nairobi")                       # UTC+3, no DST


def ts(y, m, d, hh, mm, ss=0, zone=NAI):
    return datetime(y, m, d, hh, mm, ss, tzinfo=zone).timestamp()


class FakeEngine:
    def __init__(self, fail=None):
        self.state, self.started_with, self.fail = "STOPPED", [], fail

    async def start(self, plan=None):
        if self.fail:
            raise ScalperStartError(self.fail)
        self.started_with.append(plan)
        self.state = "RUNNING"


class FakePresets:
    def __init__(self, fail=False):
        self.applied, self.fail = [], fail

    def apply(self, pid):
        if self.fail:
            raise KeyError(pid)
        self.applied.append(pid)


def make(tmp_path, *, engine=None, presets=None, open_=True, **slot):
    store = ScheduleStore(tmp_path / "s.json")
    store.replace({"enabled": True, "timezone": "Africa/Nairobi",
                   "slots": [{"id": "a", "name": "Afternoon", "time": "15:30", "days": [0, 1, 2, 3, 4],
                              "duration_minutes": 90, "profit_target_money": 5.0, **slot}]})
    eng = engine or FakeEngine()
    return Scheduler(store, eng, presets or FakePresets(), market_open=lambda t: (open_, None if open_ else "Weekend")), store, eng


MON = (2026, 11, 9)       # a Monday
SAT = (2026, 11, 14)


# ------------------------------------------------------------------ schedule rules
@pytest.mark.asyncio
async def test_starts_at_the_set_time_with_a_fixed_end_and_target(tmp_path):
    sch, store, eng = make(tmp_path)
    assert await sch.tick(ts(*MON, 15, 29, 59)) == []                  # not yet
    assert await sch.tick(ts(*MON, 15, 30, 5)) == ["Afternoon: started"]
    plan = eng.started_with[0]
    assert plan.ends_at == ts(*MON, 17, 0) and plan.profit_target_money == 5.0 and plan.source == "schedule"
    assert store.get().slots[0].last_run_date == "2026-11-09" and "Started at 15:30" in store.get().slots[0].last_result


@pytest.mark.asyncio
async def test_runs_only_once_per_day_and_again_the_next_day(tmp_path):
    sch, store, eng = make(tmp_path)
    await sch.tick(ts(*MON, 15, 30, 5))
    eng.state = "STOPPED"
    assert await sch.tick(ts(*MON, 15, 31)) == []
    assert len(eng.started_with) == 1
    assert await sch.tick(ts(2026, 11, 10, 15, 30, 2)) == ["Afternoon: started"]


@pytest.mark.asyncio
async def test_does_not_run_on_unselected_days_or_when_disabled(tmp_path):
    sch, store, eng = make(tmp_path)
    assert await sch.tick(ts(*SAT, 15, 30, 5)) == [] and not eng.started_with
    sch2, _, eng2 = make(tmp_path / "x", enabled=False)
    assert await sch2.tick(ts(*MON, 15, 30, 5)) == [] and not eng2.started_with
    store.replace({**store.get().model_dump(), "enabled": False})
    assert await sch.tick(ts(*MON, 15, 30, 5)) == []


@pytest.mark.asyncio
async def test_skips_the_day_when_the_market_is_closed(tmp_path):
    sch, store, eng = make(tmp_path, open_=False)
    assert await sch.tick(ts(*MON, 15, 30, 5)) == ["Afternoon: skipped (market closed)"]
    assert not eng.started_with and "market closed" in store.get().slots[0].last_result
    assert await sch.tick(ts(*MON, 15, 31)) == []                       # not retried the same day


@pytest.mark.asyncio
async def test_market_check_can_be_turned_off_for_a_slot(tmp_path):
    sch, store, eng = make(tmp_path, open_=False, require_market_open=False)
    assert await sch.tick(ts(*MON, 15, 30, 5)) == ["Afternoon: started"]


@pytest.mark.asyncio
async def test_a_late_server_start_is_not_resumed_after_the_grace_window(tmp_path):
    sch, store, eng = make(tmp_path)
    assert await sch.tick(ts(*MON, 15, 30) + GRACE_SECONDS + 5) == ["Afternoon: missed"]
    assert not eng.started_with and store.get().slots[0].last_run_date == "2026-11-09"


@pytest.mark.asyncio
async def test_a_restart_inside_the_grace_window_still_starts_but_ends_on_time(tmp_path):
    sch, store, eng = make(tmp_path)
    await sch.tick(ts(*MON, 15, 36))
    assert eng.started_with[0].ends_at == ts(*MON, 17, 0)               # end time is on the clock, not "90 min from now"


@pytest.mark.asyncio
async def test_failed_start_is_retried_until_the_grace_window_ends(tmp_path):
    eng = FakeEngine(fail="MT5 not connected")
    sch, store, _ = make(tmp_path, engine=eng)
    assert "could not start" in (await sch.tick(ts(*MON, 15, 30, 5)))[0]
    assert store.get().slots[0].last_run_date is None
    eng.fail = None
    assert await sch.tick(ts(*MON, 15, 32)) == ["Afternoon: started"]


@pytest.mark.asyncio
async def test_skips_if_the_scalper_is_already_running(tmp_path):
    eng = FakeEngine(); eng.state = "RUNNING"
    sch, store, _ = make(tmp_path, engine=eng)
    assert await sch.tick(ts(*MON, 15, 30, 5)) == ["Afternoon: skipped (already running)"]
    assert not eng.started_with


@pytest.mark.asyncio
async def test_applies_the_chosen_preset_first_and_refuses_to_start_if_it_fails(tmp_path):
    pre = FakePresets()
    sch, store, eng = make(tmp_path, presets=pre, preset_id="builtin-2")
    await sch.tick(ts(*MON, 15, 30, 5))
    assert pre.applied == ["builtin-2"] and eng.started_with
    bad = FakePresets(fail=True)
    sch2, store2, eng2 = make(tmp_path / "y", presets=bad, preset_id="gone")
    assert await sch2.tick(ts(*MON, 15, 30, 5)) == ["Afternoon: preset problem"]
    assert not eng2.started_with and "Not started" in store2.get().slots[0].last_result


def test_timezone_and_dst_are_respected():
    cfg = ScheduleConfig.model_validate({"enabled": True, "timezone": "America/New_York",
                                         "slots": [{"id": "a", "time": "09:30", "days": [0]}]})
    slot = cfg.slots[0]
    summer = ts(2026, 7, 6, 8, 0, zone=ZoneInfo("America/New_York"))       # EDT
    winter = ts(2026, 12, 7, 8, 0, zone=ZoneInfo("America/New_York"))      # EST
    assert next_run(cfg, slot, summer)["ts"] == ts(2026, 7, 6, 9, 30, zone=ZoneInfo("America/New_York"))
    assert next_run(cfg, slot, winter)["ts"] == ts(2026, 12, 7, 9, 30, zone=ZoneInfo("America/New_York"))
    utc = ZoneInfo("UTC")
    assert datetime.fromtimestamp(next_run(cfg, slot, summer)["ts"], utc).hour == 13      # 09:30 EDT = 13:30 UTC
    assert datetime.fromtimestamp(next_run(cfg, slot, winter)["ts"], utc).hour == 14      # 09:30 EST = 14:30 UTC


def test_schedule_validation(tmp_path):
    store = ScheduleStore(tmp_path / "s.json")
    for bad in ({"timezone": "Mars/Base"}, {"slots": [{"time": "25:00"}]}, {"slots": [{"time": "9am"}]},
                {"slots": [{"days": []}]}, {"slots": [{"days": [7]}]}, {"slots": [{"duration_minutes": 5000}]},
                {"slots": [{"id": "a"}, {"id": "a"}]}, {"slots": [{}] * 7}):
        with pytest.raises(Exception):
            store.replace({"enabled": True, **bad})
    assert not store.get().enabled           # nothing invalid was saved
    store.replace({"enabled": True, "slots": [{"time": "7:5"}]})
    assert store.get().slots[0].time == "07:05"


def test_dashboard_cannot_overwrite_the_run_history_of_a_slot(tmp_path):
    sch, store, eng = make(tmp_path)
    store.mark("a", "2026-11-09", "Started at 15:30")
    store.replace({"enabled": True, "slots": [{"id": "a", "time": "16:00", "last_run_date": None, "last_result": "forged"}]})
    s = store.get().slots[0]
    assert s.time == "16:00" and s.last_run_date == "2026-11-09" and s.last_result == "Started at 15:30"


def test_schedule_persists_and_is_off_when_the_file_is_corrupt(tmp_path):
    sch, store, eng = make(tmp_path)
    assert ScheduleStore(store.path).get().slots[0].name == "Afternoon"
    store.path.write_text("garbage")
    assert ScheduleStore(store.path).get().enabled is False


# ------------------------------------------------------------------ session plan inside the real engine
@pytest.mark.asyncio
async def test_session_ends_at_the_set_time_and_closes_its_trades(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch, instruments=["EUR/USD"], max_concurrent_positions=1, cooldown_seconds=600)
    now = h.clock()
    await h.eng.start(SessionPlan(label="Afternoon", ends_at=now + 60, close_positions_at_end=True, source="schedule"))
    await asyncio.sleep(0.05)
    assert h.eng.state == RUNNING and len(await h.gw.positions()) == 1 and h.eng.status()["plan"]["label"] == "Afternoon"
    h.clock.advance(61)
    await h.cycle(); await asyncio.sleep(0.1)
    assert h.eng.state == STOPPED and not await h.gw.positions()
    assert "time is up" in h.eng.session_end_reason and h.eng.halt_reason is None and h.eng.status()["plan"] is None


@pytest.mark.asyncio
async def test_session_can_leave_trades_open_at_the_end(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch, instruments=["EUR/USD"], max_concurrent_positions=1, cooldown_seconds=600)
    await h.eng.start(SessionPlan(ends_at=h.clock() + 30, close_positions_at_end=False))
    await asyncio.sleep(0.05)
    h.clock.advance(31)
    await h.cycle(); await asyncio.sleep(0.1)
    assert h.eng.state == STOPPED and len(await h.gw.positions()) == 1      # still protected by its own SL/TP
    await h.gw.close((await h.gw.positions())[0].ticket)


@pytest.mark.asyncio
async def test_session_profit_target_ends_the_session_as_a_normal_finish(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch, instruments=["EUR/USD"], max_concurrent_positions=1, cooldown_seconds=600)
    await h.eng.start(SessionPlan(profit_target_money=0.2))
    await asyncio.sleep(0.05)
    h.move("EUR/USD", 4)                                   # +4 pips at the default 0.01 lot is about +0.40
    await h.cycle(); await asyncio.sleep(0.1)
    assert h.eng.state == STOPPED and "profit target reached" in h.eng.session_end_reason
    assert not await h.gw.positions()


@pytest.mark.asyncio
async def test_without_a_plan_nothing_changes(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch, instruments=["EUR/USD"], max_concurrent_positions=1, cooldown_seconds=600)
    await h.eng.start()
    await asyncio.sleep(0.05)
    h.clock.advance(100000)
    await h.cycle(); await asyncio.sleep(0.05)
    assert h.eng.state == RUNNING and h.eng.status()["plan"] is None
    await h.eng.stop(close_positions=True)


# ------------------------------------------------------------------ a trade keeps the rules it was opened with
@pytest.mark.asyncio
async def test_open_trade_keeps_its_own_time_limit_when_settings_change(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch, instruments=["EUR/USD"], max_concurrent_positions=1, cooldown_seconds=600, max_hold_seconds=1000)
    eng = await h.started()
    h.store.update({"max_hold_seconds": 1})                # a preset switch that would close everything instantly
    h.clock.advance(5)
    await h.cycle()
    assert len(await h.gw.positions()) == 1                # the trade still follows its own 1000s limit
    await eng.stop(close_positions=True)


@pytest.mark.asyncio
async def test_new_trades_follow_the_new_rules_and_the_lock_can_be_switched_off(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch, instruments=["EUR/USD"], max_concurrent_positions=1, cooldown_seconds=600,
                max_hold_seconds=1000, lock_rules_per_trade=False)
    eng = await h.started()
    h.store.update({"max_hold_seconds": 1})
    h.clock.advance(5)
    await h.cycle(); await h.cycle()
    assert not await h.gw.positions()                      # old behaviour: live settings apply to open trades
    await eng.stop(close_positions=True)


@pytest.mark.asyncio
async def test_rules_survive_a_restart_through_the_history(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch, instruments=["EUR/USD"], max_concurrent_positions=1, cooldown_seconds=600, max_hold_seconds=1000)
    eng = await h.started()
    await eng.stop(close_positions=False)
    h.store.update({"max_hold_seconds": 1})
    eng2 = ScalperEngine(lambda c: h.gw, h.store, h.clock, tmp_path / "log.jsonl")
    await eng2.start(); await asyncio.sleep(0.05)
    h.clock.advance(5)
    await eng2._cycle(h.store.get())
    assert len(await h.gw.positions()) == 1                # recovered the 1000s rule from the history file
    await eng2.stop(close_positions=True)
