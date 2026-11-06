"""
Command line for the backtester.

  python -m app.scalper.backtest run --source csv --csv-dir C:\\data --days 20
  python -m app.scalper.backtest run --source mt5 --days 10 --set min_signal_score=60
  python -m app.scalper.backtest run --source synthetic --days 3          # plumbing test only

(`worker` is the internal entry point the dashboard / `run` spawn, one process per variant.)
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import sys
import time
from pathlib import Path
from typing import Any

from app.scalper.backtest import jobs
from app.scalper.backtest.data import DataError, limit_days, load_dir, synthetic
from app.scalper.backtest.metrics import summarize
from app.scalper.backtest.replay_gateway import Assumptions
from app.scalper.backtest.runner import run_variant
from app.scalper.config import ScalperConfig


def load_job_data(req: dict[str, Any], cfg: ScalperConfig, d: Path):
    inst = cfg.instruments
    if req["source"] == "synthetic":
        return synthetic(inst, req["days"], req["seed"], req["synthetic_trending"])
    folder = req["csv_dir"] if req["source"] == "csv" else str(d / "data")
    offset = req["csv_utc_offset_hours"] if req["source"] == "csv" else 0.0
    data = load_dir(folder, inst, offset)
    return {k: limit_days(v, req["days"]) for k, v in data.items()}


def worker(job_dir: str, variant: str) -> int:
    d = Path(job_dir)
    meta = jobs.read_json(d / "request.json")
    req, cfg = meta["request"], ScalperConfig.model_validate(meta["config"])
    last = [0.0]

    def progress(done: int, total: int) -> None:
        if time.time() - last[0] > 0.7 or done == total:
            last[0] = time.time()
            jobs.write_json(
                d / f"progress_{variant}.json", {"done": done, "total": total}, best_effort=True
            )

    try:
        data = load_job_data(req, cfg, d)
        pip_values, balance = None, req["start_balance"]
        meta_file = d / "data" / "mt5_meta.json"
        if req["source"] == "mt5" and meta_file.exists():
            m = jobs.read_json(meta_file) or {}
            pip_values = {k: v["pip_value_per_lot"] for k, v in m.get("instruments", {}).items() if v.get("pip_value_per_lot")}
            balance = balance or (m.get("account") or {}).get("equity")
        a = Assumptions(spread_pips=req["spread_pips"], slippage_pips=req["slippage_pips"], commission_per_lot=req["commission_per_lot"],
                        start_balance=balance or 10_000.0, stops_level_points=req["stops_level_points"], path_seed=req["seed"],
                        pip_values=pip_values)
        run = asyncio.run(run_variant(data, cfg, a, variant, req["seed"], req["session_mode"], progress))
        summary = summarize(run)
        with open(d / f"trades_{variant}.csv", "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            cols = ["t_open", "t_close", "instrument", "side", "volume", "entry", "exit", "pips", "pips_net", "gross", "cost", "net", "reason", "score"]
            w.writerow(cols)
            for t in sorted(run["trades"], key=lambda x: x["t_open"]):
                w.writerow([t.get(c) for c in cols])
        jobs.write_json(d / f"result_{variant}.json", summary)
        return 0
    except (DataError, ValueError, KeyError, RuntimeError) as exc:
        (d / f"error_{variant}.txt").write_text(f"{type(exc).__name__}: {exc}", encoding="utf-8")
        return 1


def format_report(result: dict[str, Any]) -> str:
    lines: list[str] = []
    sums = result["summaries"]
    lines.append(f"{'variant':10s} {'trades':>7s} {'net':>11s} {'win%':>6s} {'PF':>6s} {'avg/trade':>10s} {'gross':>10s} {'costs':>9s} {'maxDD%':>7s}")
    for v, s in sums.items():
        lines.append(f"{v:10s} {s['n']:7d} {s['net']:+11.2f} {s['win_rate'] if s['win_rate'] is not None else 0:6.1f} "
                     f"{s['profit_factor'] if s['profit_factor'] is not None else 0:6.2f} {s['expectancy'] if s['expectancy'] is not None else 0:+10.3f} "
                     f"{s['gross_before_costs']:+10.1f} {s['costs']:9.1f} {s['max_drawdown_pct']:7.1f}")
    verdict = result["verdict"]
    lines += ["", f"VERDICT: {verdict['overall'].upper().replace('_', ' ')}"]
    mark = {"good": "[+]", "warn": "[!]", "bad": "[x]", "info": "[i]"}
    lines += [f"  {mark.get(p['level'], '-')} {p['text']}" for p in verdict["points"]]
    st = sums["strategy"]
    if st["by_score_band"]:
        lines += ["", "By signal score (does a higher score mean better trades?):"]
        lines += [f"  {k:>7s}  n={x['n']:5d}  win%={x['win_rate']:5.1f}  avg/trade={x['expectancy']:+.3f}  net={x['net']:+.2f}" for k, x in st["by_score_band"].items()]
    lines += [""] + [f"Note: {n}" for n in result.get("notes", [])]
    return "\n".join(lines)


def run(args: argparse.Namespace) -> int:
    from app.scalper.config import scalper_config_store

    overrides: dict[str, Any] = {}
    for item in args.set or []:
        key, _, raw = item.partition("=")
        try:
            overrides[key.strip()] = __import__("json").loads(raw)
        except ValueError:
            overrides[key.strip()] = raw
    req = jobs.BacktestRequest(
        source=args.source, days=args.days, instruments=args.instruments.split(",") if args.instruments else None, csv_dir=args.csv_dir,
        csv_utc_offset_hours=args.utc_offset, spread_pips=args.spread, slippage_pips=args.slippage, commission_per_lot=args.commission,
        start_balance=args.balance, variants=args.variants.split(","), config_overrides=overrides, seed=args.seed,
        session_mode=args.session_mode, synthetic_trending=args.trending,
    )
    job_id, cfg = jobs.create_job(req, scalper_config_store.get())
    try:
        asyncio.run(jobs.prepare_data(job_id, req, cfg))
    except DataError as exc:
        print(f"Data problem: {exc}", file=sys.stderr)
        return 2
    jobs.spawn_workers(job_id)
    print(f"Backtest {job_id}: {req.days} trading days, {', '.join(cfg.instruments)}, variants: {', '.join(req.variants)}")
    while True:
        st = jobs.job_status(job_id)
        print(f"\r  {st['state']:9s} {st['progress'] * 100:5.1f}%", end="", flush=True)
        if st["state"] != "running":
            break
        time.sleep(1.0)
    print()
    if st["state"] != "done":
        print(f"Failed: {st.get('error')}", file=sys.stderr)
        return 1
    print(format_report(st["result"]))
    print(f"\nTrade-by-trade CSVs: {jobs.job_dir(job_id)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.scalper.backtest", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    w = sub.add_parser("worker")
    w.add_argument("--job-dir", required=True)
    w.add_argument("--variant", required=True)
    r = sub.add_parser("run")
    r.add_argument("--source", choices=["mt5", "csv", "synthetic"], default="csv")
    r.add_argument("--days", type=int, default=10)
    r.add_argument("--instruments", help="comma list, e.g. EUR/USD,XAU/USD (default: the scalper's pairs)")
    r.add_argument("--csv-dir")
    r.add_argument("--utc-offset", type=float, default=0.0, help="hours the CSV clock is ahead of UTC (MT5 exports: broker time)")
    r.add_argument("--spread", type=float, default=None, help="fixed spread in pips (default: data's own, else typical)")
    r.add_argument("--slippage", type=float, default=0.2)
    r.add_argument("--commission", type=float, default=0.0, help="round-turn per lot")
    r.add_argument("--balance", type=float, default=None, help="starting balance in account units (default: your MT5 account, else 10,000)")
    r.add_argument("--variants", default="strategy,baseline,shuffle,stress")
    r.add_argument("--seed", type=int, default=1)
    r.add_argument("--session-mode", choices=["day", "run"], default="day")
    r.add_argument("--trending", action="store_true", help="synthetic only: plant persistent trends")
    r.add_argument("--set", action="append", metavar="key=value", help="override a scalper setting for this test only")
    args = ap.parse_args(argv)
    return worker(args.job_dir, args.variant) if args.cmd == "worker" else run(args)


if __name__ == "__main__":
    sys.exit(main())
