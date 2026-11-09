"""The locked test period: Explore can't see it, Confirm uses it once, and the state can't be gamed by accident."""
import asyncio
import csv
import json
import time

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from app.scalper.backtest import holdout as H
from app.scalper.backtest import jobs as bt_jobs
from app.scalper.backtest.data import DataError, synthetic
from app.scalper.backtest.replay_gateway import Assumptions
from app.scalper.backtest.runner import run_variant
from app.scalper.config import ScalperConfig


@pytest.fixture
def state(tmp_path, monkeypatch):
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "scalper_state_dir", str(tmp_path))
    return tmp_path


# ---------------------------------------------------------------- state store
def test_pick_split_locks_the_requested_share():
    t = np.arange(1000) * 60
    assert H.pick_split(t, 0.3) == float(t[700])
    assert H.pick_split(t[::-1], 0.3) == float(t[700])             # order of the input doesn't matter
    with pytest.raises(H.HoldoutError):
        H.pick_split(t, 0.9)
    with pytest.raises(H.HoldoutError, match="Not enough history"):
        H.pick_split(np.arange(100) * 60, 0.3)


def test_lock_lifecycle_confirm_is_single_use_and_reset_keeps_a_record(state):
    assert H.get() is None and H.public_view() == {"locked": False, "history": []}
    lock = H.create(1_000_000.0, 0.3, "synthetic", ["EUR/USD"], (0.0, 2e6))
    assert H.get()["lock_id"] == 1 and H.public_view()["locked"]
    H.record_explore(1, "job-a", "hash1", {"net": -5.0}, "not_viable")
    assert H.get()["last_explore"]["config_hash"] == "hash1"
    assert H.record_confirm(1, "job-b", "hash1", {"net": 3.0, "net_pct": 3.0}, "inconclusive")
    assert H.record_confirm(1, "job-b", "hash1", {"net": 3.0, "net_pct": 3.0}, "inconclusive")   # same job again: idempotent
    assert not H.record_confirm(1, "job-c", "hash2", {"net": 99.0}, "promising")                  # a different job can't re-spend it
    assert H.get()["confirmed"]["job_id"] == "job-b"
    assert not H.record_confirm(7, "job-x", "h", {}, "x")                                          # stale lock id ignored
    H.reset()
    assert H.get() is None
    hist = H.public_view()["history"]
    assert len(hist) == 1 and hist[0]["was_confirmed"] and hist[0]["confirmed_net_pct"] == 3.0
    assert H.create(2e6, 0.3, "synthetic", ["EUR/USD"], (0, 3e6))["lock_id"] == 2                 # ids keep counting
    H.reset(); H.reset()                                                                           # resetting nothing is harmless
    assert len(H.public_view()["history"]) == 2


def test_corrupt_state_file_is_treated_as_no_lock(state):
    (state / "backtests").mkdir()
    (state / "backtests" / "holdout.json").write_text("{not json")
    assert H.get() is None
    H.create(1.0, 0.3, "x", [], (0, 1))
    assert H.get()["lock_id"] == 1


# ---------------------------------------------------------------- data slicing
def frames():
    return synthetic(["EUR/USD"], 3, seed=4)


def test_explore_data_ends_before_the_split_and_confirm_keeps_only_a_warmup_before_it():
    d = frames()
    idx = d["EUR/USD"].index
    split = float(idx[2000].timestamp())
    info = {"mode": "explore", "split_ts": split}
    ex, trade_from = bt_jobs.apply_holdout(d, info)
    assert trade_from is None and ex["EUR/USD"].index.max() < pd.Timestamp.fromtimestamp(split, tz="UTC") and len(ex["EUR/USD"]) == 2000
    co, trade_from = bt_jobs.apply_holdout(d, {**info, "mode": "confirm"})
    assert trade_from == split and len(co["EUR/USD"]) == (len(idx) - 2000) + 200          # locked part + 200-bar warm-up
    assert co["EUR/USD"].index[200] == idx[2000]                                           # the split falls right after the warm-up
    same, none = bt_jobs.apply_holdout(d, {"mode": "off", "split_ts": split})
    assert same is d and none is None and bt_jobs.apply_holdout(d, None) == (d, None)


def test_slicing_refuses_periods_too_short_to_test():
    d = frames()
    idx = d["EUR/USD"].index
    with pytest.raises(DataError, match="before the locked period"):
        bt_jobs.apply_holdout(d, {"mode": "explore", "split_ts": float(idx[100].timestamp())})
    with pytest.raises(DataError, match="inside the locked period"):
        bt_jobs.apply_holdout(d, {"mode": "confirm", "split_ts": float(idx[-50].timestamp())})


@pytest.mark.asyncio
async def test_runner_warms_up_but_does_not_trade_before_trade_from():
    cfg = ScalperConfig(min_signal_score=65, cooldown_seconds=30, max_hold_seconds=600, exit_mode="atr", session_max_loss_money=1e9,
                        session_max_loss_pct=0, max_consecutive_losses=0, instruments=["EUR/USD"])
    d = synthetic(["EUR/USD"], 1, seed=9)
    mid = float(d["EUR/USD"].index[700].timestamp())
    full = await run_variant(d, cfg, Assumptions(), "strategy", 5)
    late = await run_variant(d, cfg, Assumptions(), "strategy", 5, trade_from_ts=mid)
    assert min(t["t_open"] for t in full["trades"]) < mid
    assert late["trades"] and min(t["t_open"] for t in late["trades"]) >= mid


# ---------------------------------------------------------------- the whole flow over HTTP
KEY = "k" * 32
H_ = {"X-API-Key": KEY}


@pytest.fixture
def api(tmp_path, monkeypatch):
    from app.config import get_settings
    from app.main import app
    from app.scalper.config import scalper_config_store
    s = get_settings()
    monkeypatch.setattr(s, "api_secret_key", KEY)
    monkeypatch.setattr(s, "api_auth_enabled", True)
    monkeypatch.setattr(s, "scalper_state_dir", str(tmp_path))
    monkeypatch.setattr(scalper_config_store, "_path", tmp_path / "c.json")
    monkeypatch.setattr(scalper_config_store, "_loaded", False)
    scalper_config_store.reset()
    scalper_config_store.update({"instruments": ["EUR/USD"], "min_signal_score": 60, "session_max_loss_money": 1e6, "max_consecutive_losses": 0})
    return TestClient(app), tmp_path


def wait_done(c, job_id, timeout=120):
    end = time.time() + timeout
    while time.time() < end:
        st = c.get(f"/api/scalper/backtest/{job_id}", headers=H_).json()
        if st["state"] != "running":
            return st
        time.sleep(0.4)
    raise AssertionError("job did not finish")


def trades_of(tmp, job_id):
    with open(bt_jobs.jobs_root() / job_id / "trades_strategy.csv") as fh:
        return [{k: float(v) if k in ("t_open", "t_close") else v for k, v in row.items()} for row in csv.DictReader(fh)]


def test_explore_cannot_see_the_locked_period_and_confirm_works_exactly_once(api):
    c, tmp = api
    body = {"source": "synthetic", "days": 3, "variants": ["strategy"], "holdout_fraction": 0.34}

    # this also proves "/backtest/holdout" isn't swallowed by "/backtest/{job_id}"
    r = c.get("/api/scalper/backtest/holdout", headers=H_)
    assert r.status_code == 200 and r.json()["locked"] is False

    r = c.post("/api/scalper/backtest", headers=H_, json={**body, "holdout": "confirm"})
    assert r.status_code == 400 and "no locked test period" in r.json()["detail"].lower()

    # --- EXPLORE: creates the lock, and the engine only ever sees data before it
    r = c.post("/api/scalper/backtest", headers=H_, json={**body, "holdout": "explore"})
    assert r.status_code == 200 and r.json()["holdout"]["mode"] == "explore"
    ex = wait_done(c, r.json()["job_id"])
    assert ex["state"] == "done", ex
    lock = c.get("/api/scalper/backtest/holdout", headers=H_).json()
    assert lock["locked"] and lock["confirmed"] is None and lock["last_explore"]["job_id"] == ex["job_id"]
    split = lock["split_ts"]
    ex_trades = trades_of(tmp, ex["job_id"])
    assert len(ex_trades) > 0
    assert max(t["t_close"] for t in ex_trades) < split and max(t["t_open"] for t in ex_trades) < split     # NO PEEKING
    assert ex["result"]["holdout"]["mode"] == "explore"
    assert ex["result"]["verdict"]["points"][0]["text"].startswith("EXPLORE RUN")
    assert max(p[0] for p in ex["result"]["summaries"]["strategy"]["equity"]) < split

    # --- CONFIRM: only the locked period is traded; allowed once
    r = c.post("/api/scalper/backtest", headers=H_, json={**body, "holdout": "confirm"})
    assert r.status_code == 200 and r.json()["holdout"]["warnings"] == []            # same settings as explored: no warning
    cf = wait_done(c, r.json()["job_id"])
    assert cf["state"] == "done", cf
    cf_trades = trades_of(tmp, cf["job_id"])
    assert len(cf_trades) > 0 and min(t["t_open"] for t in cf_trades) >= split       # nothing traded during the warm-up
    res = cf["result"]
    assert res["holdout"]["mode"] == "confirm" and res["holdout"]["explore"]["n"] == len(ex_trades)
    assert res["verdict"]["points"][0]["text"].startswith("OUT-OF-SAMPLE CONFIRMATION")
    lock = c.get("/api/scalper/backtest/holdout", headers=H_).json()
    assert lock["confirmed"]["job_id"] == cf["job_id"] and lock["confirmed"]["summary"]["n"] == len(cf_trades)

    # --- a second look is refused
    r = c.post("/api/scalper/backtest", headers=H_, json={**body, "holdout": "confirm"})
    assert r.status_code == 409 and "already used" in r.json()["detail"]
    assert not bt_jobs.running_job_id()
    # exploring is still fine (and still can't see the locked period)
    assert c.get("/api/scalper/backtest/holdout", headers=H_).json()["split_ts"] == split

    # --- reset: the old period counts as seen, history says so, a new lock gets a new id
    r = c.post("/api/scalper/backtest/holdout/reset", headers=H_)
    assert r.status_code == 200 and r.json()["locked"] is False
    hist = r.json()["history"]
    assert len(hist) == 1 and hist[0]["was_confirmed"] is True and hist[0]["lock_id"] == 1


def test_holdout_endpoints_need_the_api_key(api):
    c, _ = api
    assert c.get("/api/scalper/backtest/holdout").status_code == 401
    assert c.post("/api/scalper/backtest/holdout/reset").status_code == 401


def test_off_mode_uses_everything_and_creates_no_lock(api):
    c, tmp = api
    r = c.post("/api/scalper/backtest", headers=H_, json={"source": "synthetic", "days": 1, "variants": ["strategy"], "holdout": "off"})
    assert r.status_code == 200 and r.json()["holdout"] is None
    st = wait_done(c, r.json()["job_id"])
    assert st["state"] == "done" and "holdout" not in st["result"]
    assert c.get("/api/scalper/backtest/holdout", headers=H_).json()["locked"] is False


@pytest.mark.asyncio
async def test_resolve_reuses_the_lock_and_warns_when_confirming_different_settings(state):
    from app.scalper.config import ScalperConfig as C
    base = C(instruments=["EUR/USD"], session_max_loss_money=1e6, max_consecutive_losses=0)

    async def job(holdout, **over):
        req = bt_jobs.BacktestRequest(source="synthetic", days=3, variants=["strategy"], holdout=holdout, config_overrides=over)
        jid, cfg = bt_jobs.create_job(req, base)
        return jid, await bt_jobs.resolve_holdout(jid, req, cfg)

    j1, a = await job("explore")
    j2, b = await job("explore")
    assert a["split_ts"] == b["split_ts"] and a["lock_id"] == b["lock_id"] == 1      # explore runs share one lock
    H.record_explore(1, j1, bt_jobs.read_json(bt_jobs.job_dir(j1) / "request.json")["config_hash"], {"n": 1}, "x")
    _, same = await job("confirm")
    assert same["warnings"] == []
    _, other = await job("confirm", min_signal_score=70)
    assert any("differ" in w for w in other["warnings"])
    # the request.json the workers read carries the lock
    assert bt_jobs.read_json(bt_jobs.job_dir(j1) / "request.json")["holdout"]["mode"] == "explore"
    H.reset()
    _, c = await job("explore")
    assert c["lock_id"] == 2


@pytest.mark.asyncio
async def test_confirming_without_ever_exploring_is_flagged(state):
    base = ScalperConfig(instruments=["EUR/USD"], session_max_loss_money=1e6, max_consecutive_losses=0)
    j0 = bt_jobs.BacktestRequest(source="synthetic", days=3, variants=["strategy"], holdout="explore")
    jid, cfg = bt_jobs.create_job(j0, base)
    await bt_jobs.resolve_holdout(jid, j0, cfg)                       # creates the lock, but nothing was ever explored on it
    req = bt_jobs.BacktestRequest(source="synthetic", days=3, variants=["strategy"], holdout="confirm")
    jid2, cfg2 = bt_jobs.create_job(req, base)
    info = await bt_jobs.resolve_holdout(jid2, req, cfg2)
    assert any("never explored" in w for w in info["warnings"])
