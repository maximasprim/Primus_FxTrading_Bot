"""Backtest data loaders, metrics/verdict, runner behaviour and the job lifecycle."""
import asyncio
import os
import time

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from app.scalper.backtest import jobs as bt_jobs
from app.scalper.backtest.data import DataError, fetch_mt5_history, find_csv, limit_days, load_csv, load_dir, shuffle_bars, synthetic
from app.scalper.backtest.metrics import build_verdict, summarize
from app.scalper.backtest.replay_gateway import Assumptions
from app.scalper.backtest.runner import run_variant
from app.scalper.config import ScalperConfig


# ---------------------------------------------------------------- data loaders
def write_mt5_export(path, n=400, spread=lambda i: 8 + i % 3, sep="\t"):
    rows = [sep.join(["<DATE>", "<TIME>", "<OPEN>", "<HIGH>", "<LOW>", "<CLOSE>", "<TICKVOL>", "<VOL>", "<SPREAD>"])]
    c = 1.1
    for i in range(n):
        h, m = divmod(i, 60)
        c += np.random.default_rng(i).normal(0, 1e-4)
        rows.append(sep.join([f"2025.01.06", f"{h:02d}:{m:02d}:00", f"{c:.5f}", f"{c + 1e-4:.5f}", f"{c - 1e-4:.5f}", f"{c:.5f}", "10", "0", str(spread(i))]))
    path.write_text("\n".join(rows))


def test_mt5_history_center_export_keeps_real_spreads_and_converts_broker_time(tmp_path):
    """Regression: spreads once came through as NaN (index misalignment), silently replacing real costs with defaults."""
    p = tmp_path / "EURUSD.csv"
    write_mt5_export(p)
    df = load_csv(p, utc_offset_hours=2)
    assert df["spread_pips"].notna().all() and df["spread_pips"].iloc[:3].tolist() == [0.8, 0.9, 1.0]
    assert str(df.index[0]) == "2025-01-05 22:00:00+00:00"   # broker UTC+2 -> UTC


@pytest.mark.parametrize("sep", [",", ";", "\t"])
def test_generic_csv_delimiters_and_columns(tmp_path, sep):
    idx = pd.date_range("2025-01-06", periods=300, freq="1min")
    pd.DataFrame({"Time": idx.strftime("%Y-%m-%d %H:%M:%S"), "Open": 1.1, "High": 1.1002, "Low": 1.0998, "Close": 1.1}).to_csv(tmp_path / "x.csv", sep=sep, index=False)
    assert len(load_csv(tmp_path / "x.csv")) == 300


def test_epoch_timestamps_and_dukascopy_dates(tmp_path):
    idx = pd.date_range("2025-01-06", periods=300, freq="1min", tz="UTC")
    pd.DataFrame({"time": (idx.asi8 // idx.asi8[0] * 0 + (idx - pd.Timestamp("1970-01-01", tz="UTC")).total_seconds().astype(int)),
                  "open": 1.1, "high": 1.1002, "low": 1.0998, "close": 1.1}).to_csv(tmp_path / "e.csv", index=False)
    assert load_csv(tmp_path / "e.csv").index[0] == idx[0]
    pd.DataFrame({"Gmt time": idx.strftime("%d.%m.%Y %H:%M:%S.000"), "Open": 1.1, "High": 1.1002, "Low": 1.0998, "Close": 1.1}).to_csv(tmp_path / "d.csv", index=False)
    assert load_csv(tmp_path / "d.csv").index[0] == idx[0]


@pytest.mark.parametrize("content,msg", [
    ("a,b\n1,2\n", "missing column"),
    ("time,open,high,low,close\n2025-01-06 00:00,1,2,0,1\n", "usable rows"),
])
def test_bad_csv_gives_a_readable_error(tmp_path, content, msg):
    (tmp_path / "b.csv").write_text(content)
    with pytest.raises(DataError, match=msg):
        load_csv(tmp_path / "b.csv")


def test_rejects_non_minute_data_and_swapped_columns(tmp_path):
    idx = pd.date_range("2025-01-06", periods=400, freq="1h")
    pd.DataFrame({"time": idx.astype(str), "open": 1.1, "high": 1.1002, "low": 1.0998, "close": 1.1}).to_csv(tmp_path / "h.csv", index=False)
    with pytest.raises(DataError, match="1-minute"):
        load_csv(tmp_path / "h.csv")
    idx = pd.date_range("2025-01-06", periods=400, freq="1min")
    pd.DataFrame({"time": idx.astype(str), "open": 1.1, "high": 1.0998, "low": 1.1002, "close": 1.1}).to_csv(tmp_path / "s.csv", index=False)
    with pytest.raises(DataError, match="high < low"):
        load_csv(tmp_path / "s.csv")


def test_find_csv_and_limit_days(tmp_path):
    write_mt5_export(tmp_path / "EURUSD_M1.csv")
    assert find_csv(tmp_path, "EUR/USD").name == "EURUSD_M1.csv"
    with pytest.raises(DataError, match="No CSV for GBP/USD"):
        find_csv(tmp_path, "GBP/USD")
    with pytest.raises(DataError, match="Folder not found"):
        find_csv(tmp_path / "nope", "EUR/USD")
    df = synthetic(["EUR/USD"], 5, seed=1)["EUR/USD"]
    assert limit_days(df, 2).index.normalize().nunique() == 2


def test_shuffle_keeps_candle_shapes_but_destroys_order():
    d = synthetic(["EUR/USD"], 2, seed=2)["EUR/USD"]
    s = shuffle_bars(d, 7)
    assert len(s) == len(d) and (s.high >= s.low).all()
    assert ((s.open <= s.high + 1e-12) & (s.open >= s.low - 1e-12) & (s.close <= s.high + 1e-12) & (s.close >= s.low - 1e-12)).all()
    assert np.allclose(np.sort((d.high - d.low).to_numpy()), np.sort((s.high - s.low).to_numpy()))
    assert not np.array_equal(d.close.to_numpy(), s.close.to_numpy())
    assert shuffle_bars(d, 7).equals(s) and not shuffle_bars(d, 8).equals(s)  # deterministic per seed


@pytest.mark.asyncio
async def test_mt5_history_fetch_converts_time_and_records_real_spread(tmp_path):
    from tests.test_mt5_gateway import make
    fake, client, _ = make(offset=7200)
    # 2 trading days: always spans more than one UTC day, so the count doesn't depend on what time the test runs
    meta = await fetch_mt5_history(client, ["EUR/USD"], "m", 2, tmp_path)
    assert meta["EUR/USD"]["bars"] >= 200
    df = load_dir(tmp_path, ["EUR/USD"])["EUR/USD"]
    assert df["spread_pips"].iloc[0] == pytest.approx(0.8)           # 8 points * 0.00001 / 0.0001
    assert abs(df.index[-1].timestamp() - time.time()) < 300          # broker UTC+2 converted back to UTC


# ---------------------------------------------------------------- metrics + verdict
def run_of(nets, variant="strategy", equity=None, bal=10_000.0, halts=()):
    trades = [{"t_open": 1_700_000_000 + i * 400, "t_close": 1_700_000_000 + i * 400 + 120, "net": float(v), "gross": float(v) + 0.1, "cost": 0.1,
               "instrument": "EUR/USD", "reason": "take-profit" if v > 0 else "stop-loss", "score": 70 + (i % 25), "pips": 1.0, "pips_net": 1.0} for i, v in enumerate(nets)]
    eq = equity or [(1_700_000_000 + i * 60, bal + float(np.sum(nets[:i]))) for i in range(len(nets))]
    return {"variant": variant, "trades": trades, "equity": eq, "start_balance": bal, "start": "a", "end": "b", "bars": len(nets),
            "instruments": ["EUR/USD"], "halts": list(halts), "errors": [], "assumptions": {}}


def test_summary_numbers_are_right():
    s = summarize(run_of([2, 2, -1, -1, -1, 3]))
    assert s["n"] == 6 and s["net"] == 4.0 and s["win_rate"] == pytest.approx(50.0)
    assert s["profit_factor"] == pytest.approx(7 / 3, abs=0.01) and s["longest_losing_streak"] == 3
    assert s["avg_win"] == pytest.approx(7 / 3, abs=0.01) and s["avg_loss"] == -1.0
    assert s["by_exit_reason"]["stop-loss"]["n"] == 3 and s["in_sample"]["n"] + s["out_of_sample"]["n"] == 6
    assert summarize(run_of([]))["n"] == 0


def test_max_drawdown_from_equity_curve():
    eq = [(i, v) for i, v in enumerate([10_000, 10_100, 10_050, 9_900, 10_300, 10_250])]
    s = summarize(run_of([1, 1], equity=eq))
    assert s["max_drawdown"] == 200.0 and s["max_drawdown_pct"] == pytest.approx(200 / 10_100 * 100, abs=0.01)


def good_run(n=400, mean=0.5, seed=1):
    return list(np.random.default_rng(seed).normal(mean, 1.0, n))


def sums(**runs):
    return {k: summarize(run_of(v, k)) for k, v in runs.items()}


def test_verdict_needs_enough_trades():
    v = build_verdict(sums(strategy=[1.0] * 10))
    assert v["overall"] == "inconclusive" and "too few" in v["points"][0]["text"].lower()


def test_verdict_losing_strategy_is_not_viable():
    v = build_verdict(sums(strategy=good_run(mean=-0.3)))
    assert v["overall"] == "not_viable"


def test_verdict_promising_only_when_everything_agrees():
    ok = sums(strategy=good_run(mean=0.5), baseline=good_run(mean=-0.2, seed=2), shuffle=good_run(mean=-0.2, seed=3), stress=good_run(mean=0.2, seed=4))
    assert build_verdict(ok)["overall"] == "promising"
    # profitable but no better than scrambled prices -> not promising
    no_skill = sums(strategy=good_run(mean=0.5), baseline=good_run(mean=-0.2, seed=2), shuffle=good_run(mean=0.5, seed=3), stress=good_run(mean=0.2, seed=4))
    v = build_verdict(no_skill)
    assert v["overall"] != "promising" and any("scrambled" in p["text"] for p in v["points"])
    # profit vanishes when costs double
    fragile = sums(strategy=good_run(mean=0.5), baseline=good_run(mean=-0.2, seed=2), shuffle=good_run(mean=-0.2, seed=3), stress=good_run(mean=-0.1, seed=4))
    v = build_verdict(fragile)
    assert v["overall"] != "promising" and any("costs are 2" in p["text"] for p in v["points"])
    # small sample can never be 'promising'
    assert build_verdict(sums(strategy=good_run(n=80, mean=0.5)))["overall"] != "promising"


def test_verdict_always_carries_the_caution():
    for runs in (sums(strategy=good_run(mean=0.5)), sums(strategy=[1.0] * 5)):
        assert "demo-account" in build_verdict(runs)["points"][-1]["text"]


# ---------------------------------------------------------------- runner
CFG = dict(min_signal_score=65, cooldown_seconds=30, max_hold_seconds=600, exit_mode="atr", session_max_loss_money=1e9, session_max_loss_pct=0,
           max_consecutive_losses=0, instruments=["EUR/USD"])


@pytest.mark.asyncio
async def test_same_seed_gives_identical_results_and_caps_hold():
    d = synthetic(["EUR/USD"], 1, seed=9)
    cfg = ScalperConfig(**CFG, max_concurrent_positions=2, max_positions_per_instrument=2)
    a = await run_variant(d, cfg, Assumptions(), "strategy", 5)
    b = await run_variant(d, cfg, Assumptions(), "strategy", 5)
    assert [t["net"] for t in a["trades"]] == [t["net"] for t in b["trades"]] and len(a["trades"]) > 20
    # never more than the configured number of simultaneous trades
    events = sorted([(t["t_open"], 1) for t in a["trades"]] + [(t["t_close"], -1) for t in a["trades"]], key=lambda e: (e[0], e[1]))
    open_now = peak = 0
    for _, delta in events:
        open_now += delta
        peak = max(peak, open_now)
    assert peak <= 2
    assert max(t["t_close"] - t["t_open"] for t in a["trades"]) <= 600 + 61   # time stop (+ one evaluation step)
    assert all(t["score"] is not None and t["score"] >= 65 for t in a["trades"])


@pytest.mark.asyncio
async def test_session_loss_limit_halts_and_day_mode_restarts_next_day():
    d = synthetic(["EUR/USD"], 3, seed=9)
    cfg = ScalperConfig(**{**CFG, "session_max_loss_money": 3.0, "max_concurrent_positions": 4, "max_positions_per_instrument": 4, "entries_per_signal": 2})
    day = await run_variant(d, cfg, Assumptions(), "strategy", 5, session_mode="day")
    run = await run_variant(d, cfg, Assumptions(), "strategy", 5, session_mode="run")
    assert len(day["halts"]) >= 2 and "loss limit" in day["halts"][0]["reason"]
    assert len(run["halts"]) == 1 and run["equity"][-1][0] < day["equity"][-1][0]  # 'run' mode stops at the first halt
    assert len(run["trades"]) < len(day["trades"])


@pytest.mark.asyncio
async def test_baseline_and_shuffle_variants_change_what_they_should():
    d = synthetic(["EUR/USD"], 1, seed=9)
    cfg = ScalperConfig(**CFG, max_concurrent_positions=2, max_positions_per_instrument=2)
    strat = await run_variant(d, cfg, Assumptions(), "strategy", 5)
    base = await run_variant(d, cfg, Assumptions(), "baseline", 5)
    shuf = await run_variant(d, cfg, Assumptions(), "shuffle", 5)
    stress = await run_variant(d, cfg, Assumptions(), "stress", 5)
    sides = lambda r: [t["side"] for t in r["trades"]]
    assert abs(len(base["trades"]) - len(strat["trades"])) < 0.3 * len(strat["trades"])  # similar entry timing...
    assert sides(base) != sides(strat)                                                    # ...but different directions
    assert [t["entry"] for t in shuf["trades"]] != [t["entry"] for t in strat["trades"]]
    assert stress["assumptions"]["cost_multiplier"] == 2.0 and strat["assumptions"]["cost_multiplier"] == 1.0
    with pytest.raises(ValueError):
        await run_variant(d, cfg, Assumptions(), "nonsense")


# ---------------------------------------------------------------- jobs via the HTTP API
@pytest.fixture
def api(tmp_path, monkeypatch):
    from app.config import get_settings
    from app.main import app
    from app.scalper.config import scalper_config_store
    s = get_settings()
    monkeypatch.setattr(s, "api_secret_key", "k" * 32)
    monkeypatch.setattr(s, "api_auth_enabled", True)
    monkeypatch.setattr(s, "scalper_state_dir", str(tmp_path))
    monkeypatch.setattr(scalper_config_store, "_path", tmp_path / "c.json")
    monkeypatch.setattr(scalper_config_store, "_loaded", False)
    scalper_config_store.reset()
    scalper_config_store.update({"instruments": ["EUR/USD"], "min_signal_score": 65, "session_max_loss_money": 1e6, "max_consecutive_losses": 0})
    return TestClient(app), {"X-API-Key": "k" * 32}


def wait_done(c, h, job_id, timeout=120):
    end = time.time() + timeout
    while time.time() < end:
        st = c.get(f"/api/scalper/backtest/{job_id}", headers=h).json()
        if st["state"] != "running":
            return st
        time.sleep(0.5)
    raise AssertionError("backtest did not finish")


def test_backtest_endpoints_need_the_api_key(api):
    c, _ = api
    assert c.post("/api/scalper/backtest", json={"source": "synthetic"}).status_code == 401
    assert c.get("/api/scalper/backtest").status_code == 401


def test_backtest_job_full_lifecycle(api):
    c, h = api
    r = c.post("/api/scalper/backtest", headers=h, json={"source": "synthetic", "days": 1, "variants": ["strategy", "baseline"]})
    assert r.status_code == 200
    job_id = r.json()["job_id"]
    assert c.post("/api/scalper/backtest", headers=h, json={"source": "synthetic", "days": 1}).status_code == 409   # one at a time
    st = wait_done(c, h, job_id)
    assert st["state"] == "done", st
    res = st["result"]
    assert set(res["summaries"]) == {"strategy", "baseline"} and res["verdict"]["overall"] in ("not_viable", "inconclusive", "promising")
    assert res["summaries"]["strategy"]["n"] > 0 and any("SYNTHETIC" in n for n in res["notes"])
    assert os.path.exists(bt_jobs.jobs_root() / job_id / "trades_strategy.csv")
    assert any(j["job_id"] == job_id and j["state"] == "done" for j in c.get("/api/scalper/backtest", headers=h).json())
    assert c.get("/api/scalper/backtest/does-not-exist", headers=h).status_code == 404
    assert c.get("/api/scalper/backtest/..%2F..", headers=h).status_code in (404, 422)


def test_bad_requests_are_rejected_with_readable_errors(api, monkeypatch):
    import sys
    from app.mt5.client import get_mt5_client
    monkeypatch.setitem(sys.modules, "MetaTrader5", None)   # behave like a machine without MT5, whatever this machine has
    get_mt5_client.cache_clear()
    c, h = api
    r = c.post("/api/scalper/backtest", headers=h, json={"source": "csv", "days": 3})
    assert r.status_code == 400 and "folder" in r.json()["detail"].lower()
    r = c.post("/api/scalper/backtest", headers=h, json={"source": "csv", "csv_dir": "/definitely/not/here"})
    assert r.status_code == 400 and "not found" in r.json()["detail"].lower()
    assert c.post("/api/scalper/backtest", headers=h, json={"source": "synthetic", "config_overrides": {"max_concurrent_positions": 999}}).status_code == 422
    assert c.post("/api/scalper/backtest", headers=h, json={"source": "synthetic", "variants": ["baseline"]}).status_code == 422   # strategy required
    assert c.post("/api/scalper/backtest", headers=h, json={"source": "synthetic", "days": 0}).status_code == 422
    r = c.post("/api/scalper/backtest", headers=h, json={"source": "mt5"})
    get_mt5_client.cache_clear()
    assert r.status_code == 400 and "MetaTrader5" in r.json()["detail"]
    assert not bt_jobs.running_job_id()


def test_failed_worker_surfaces_its_error(api, tmp_path):
    c, h = api
    d = tmp_path / "csvs"; d.mkdir()
    (d / "EURUSD.csv").write_text("time,open,high,low,close\n" + "\n".join(f"2025-01-06 00:{i % 60:02d},1,1,1,1" for i in range(300)))
    r = c.post("/api/scalper/backtest", headers=h, json={"source": "csv", "csv_dir": str(d), "days": 1, "variants": ["strategy"]})
    assert r.status_code == 200          # file exists, so the job starts...
    st = wait_done(c, h, r.json()["job_id"])
    assert st["state"] == "failed" and st["error"]     # ...and the worker reports what's wrong with the data


def test_cancel(api):
    c, h = api
    job_id = c.post("/api/scalper/backtest", headers=h, json={"source": "synthetic", "days": 20}).json()["job_id"]
    st = c.post(f"/api/scalper/backtest/{job_id}/cancel", headers=h).json()
    assert st["state"] in ("cancelled", "running", "failed")
    end = time.time() + 20
    while bt_jobs.running_job_id() and time.time() < end:
        time.sleep(0.3)
    assert not bt_jobs.running_job_id()


# ---------------------------------------------------------------- CLI report + the two controls (slow, opt-in)
def test_cli_report_is_readable():
    from app.scalper.backtest.__main__ import format_report
    s = {k: summarize(run_of(good_run(mean=m, seed=i), k)) for i, (k, m) in enumerate([("strategy", 0.5), ("baseline", -0.2), ("shuffle", -0.2), ("stress", 0.2)])}
    text = format_report({"summaries": s, "verdict": build_verdict(s), "notes": ["a note"]})
    assert "VERDICT: PROMISING" in text and "By signal score" in text and "baseline" in text and "Note: a note" in text


slow = pytest.mark.skipif(not os.environ.get("RUN_SLOW_TESTS"), reason="set RUN_SLOW_TESTS=1 (about 2 minutes)")


@slow
@pytest.mark.asyncio
@pytest.mark.parametrize("trending,expected", [(False, {"not_viable"}), (True, {"promising"})])
async def test_backtester_finds_no_edge_in_noise_and_finds_a_planted_edge(trending, expected):
    """NULL TEST: pure random prices must never look promising. POSITIVE CONTROL: planted trends must."""
    d = synthetic(["EUR/USD"], 4, seed=11, trending=trending)
    cfg = ScalperConfig(**{**CFG, "min_signal_score": 70, "session_max_loss_money": 500, "max_concurrent_positions": 4, "max_positions_per_instrument": 2})
    out = {}
    for v in ("strategy", "baseline", "shuffle", "stress"):
        out[v] = summarize(await run_variant(d, cfg, Assumptions(slippage_pips=0.2), v, 5))
    assert build_verdict(out)["overall"] in expected, build_verdict(out)


# ---------------------------------------------------------------- worker uses the real account's balance and pip values
def test_mt5_job_starts_from_the_real_balance_and_measured_pip_value(api, tmp_path):
    import json
    from app.scalper.backtest.__main__ import worker
    c, h = api
    req = bt_jobs.BacktestRequest(source="mt5", days=1, variants=["strategy"])
    assert req.start_balance is None                                  # blank in the dashboard = use the account's own balance
    job_id, cfg = bt_jobs.create_job(req, ScalperConfig(instruments=["EUR/USD"], min_signal_score=65, session_max_loss_money=1e6,
                                                         max_consecutive_losses=0))
    d = bt_jobs.job_dir(job_id)
    df = synthetic(["EUR/USD"], 1, seed=2)["EUR/USD"]
    df.rename_axis("time").reset_index().to_csv(d / "data" / "EURUSDm.csv", index=False)
    (d / "data" / "mt5_meta.json").write_text(json.dumps({"account": {"equity": 100.0, "currency": "USC"},
                                                          "instruments": {"EUR/USD": {"pip_value_per_lot": 0.1}}}))
    assert worker(str(d), "strategy") == 0
    res = json.loads((d / "result_strategy.json").read_text())
    assert res["start_balance"] == 100.0 and res["assumptions"]["pip_values"] == {"EUR/USD": 0.1}
    # an explicit balance still wins
    job2, cfg2 = bt_jobs.create_job(bt_jobs.BacktestRequest(source="mt5", days=1, variants=["strategy"], start_balance=250), cfg)
    d2 = bt_jobs.job_dir(job2)
    df.rename_axis("time").reset_index().to_csv(d2 / "data" / "EURUSDm.csv", index=False)
    (d2 / "data" / "mt5_meta.json").write_text((d / "data" / "mt5_meta.json").read_text())
    assert worker(str(d2), "strategy") == 0
    assert json.loads((d2 / "result_strategy.json").read_text())["start_balance"] == 250
