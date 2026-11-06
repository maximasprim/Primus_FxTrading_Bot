"""
Turns a backtest's raw trade list into numbers and a plain-language verdict.

The verdict is intentionally hard to impress. "Promising" needs ALL of: enough trades,
net profit after costs, an average-trade interval that stays above zero, a clear
win over the random-direction baseline, survival of doubled costs, and a profitable
out-of-sample tail. Anything less is "inconclusive" or "not viable".
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

import numpy as np

MIN_TRADES_MEANINGFUL = 200
MIN_TRADES_ANY = 30
SPLIT = 0.6  # first 60% of trades = "in-sample", last 40% = "out-of-sample" (by close time)


def _band(score: int) -> str:
    lo = min(score, 99) // 10 * 10
    return f"{lo}-{lo + 9}" if lo < 90 else "90-100"


def _stats(nets: list[float]) -> dict[str, Any]:
    n = len(nets)
    if n == 0:
        return {"n": 0, "net": 0.0, "win_rate": None, "expectancy": None, "profit_factor": None,
                "avg_win": None, "avg_loss": None, "ci95": None, "se": None}
    a = np.asarray(nets, dtype=float)
    wins, losses = a[a > 0], a[a < 0]
    pf = float(wins.sum() / -losses.sum()) if losses.sum() < 0 else None
    mean = float(a.mean())
    se = float(a.std(ddof=1) / np.sqrt(n)) if n > 1 else None
    return {
        "n": n, "net": round(float(a.sum()), 2), "win_rate": round(float((a > 0).mean()) * 100, 1),
        "expectancy": round(mean, 3), "profit_factor": None if pf is None else round(pf, 2),
        "avg_win": round(float(wins.mean()), 2) if len(wins) else None,
        "avg_loss": round(float(losses.mean()), 2) if len(losses) else None,
        "ci95": None if se is None else [round(mean - 1.96 * se, 3), round(mean + 1.96 * se, 3)],
        "se": None if se is None else round(se, 4),
    }


def _downsample(points: list[tuple[float, float]], target: int = 400) -> list[list[float]]:
    if len(points) <= target:
        return [[round(t), round(v, 2)] for t, v in points]
    idx = np.linspace(0, len(points) - 1, target).astype(int)
    return [[round(points[i][0]), round(points[i][1], 2)] for i in idx]


def _max_drawdown(equity: list[tuple[float, float]]) -> tuple[float, float]:
    if not equity:
        return 0.0, 0.0
    eq = np.asarray([v for _, v in equity], dtype=float)
    peak = np.maximum.accumulate(eq)
    dd = peak - eq
    return round(float(dd.max()), 2), round(float((dd / np.where(peak > 0, peak, 1)).max()) * 100, 2)


def summarize(run: dict[str, Any]) -> dict[str, Any]:
    trades = sorted(run["trades"], key=lambda t: t["t_close"])
    start_balance = float(run["start_balance"])
    nets = [t["net"] for t in trades]
    s = _stats(nets)
    gross = float(sum(t["gross"] for t in trades))
    costs = float(sum(t["cost"] for t in trades))
    dd_money, dd_pct = _max_drawdown(run["equity"])

    streak = best = 0
    for v in nets:
        streak = streak + 1 if v < 0 else 0
        best = max(best, streak)

    by_day: dict[str, float] = defaultdict(float)
    for t in trades:
        by_day[datetime.fromtimestamp(t["t_close"], tz=timezone.utc).strftime("%Y-%m-%d")] += t["net"]
    days_total = max(len(by_day), 1)

    def group(key_fn) -> dict[str, dict[str, Any]]:
        g: dict[str, list[float]] = defaultdict(list)
        for t in trades:
            k = key_fn(t)
            if k is not None:
                g[k].append(t["net"])
        return {k: {"n": len(v), "net": round(sum(v), 2), "win_rate": round(float(np.mean([x > 0 for x in v])) * 100, 1),
                    "expectancy": round(float(np.mean(v)), 3)} for k, v in sorted(g.items())}

    cut = int(len(trades) * SPLIT)
    hold = [t["t_close"] - t["t_open"] for t in trades]
    return {
        "variant": run["variant"], "start": run["start"], "end": run["end"], "bars": run["bars"],
        "instruments": run["instruments"], "start_balance": start_balance,
        **s,
        "gross_before_costs": round(gross, 2), "costs": round(costs, 2),
        "costs_pct_of_gross": None if gross <= 0 else round(costs / gross * 100, 1),
        "net_pct": round(s["net"] / start_balance * 100, 2),
        "max_drawdown": dd_money, "max_drawdown_pct": dd_pct,
        "longest_losing_streak": best,
        "avg_hold_seconds": round(float(np.mean(hold))) if hold else None,
        "trades_per_day": round(len(trades) / days_total, 1),
        "profitable_days_pct": round(sum(1 for v in by_day.values() if v > 0) / days_total * 100, 1) if by_day else None,
        "best_trade": round(max(nets), 2) if nets else None, "worst_trade": round(min(nets), 2) if nets else None,
        "by_exit_reason": group(lambda t: t["reason"]),
        "by_instrument": group(lambda t: t["instrument"]),
        "by_score_band": group(lambda t: None if t.get("score") is None else _band(t["score"])),
        "by_hour_utc": group(lambda t: f"{datetime.fromtimestamp(t['t_open'], tz=timezone.utc).hour:02d}"),
        "in_sample": _stats(nets[:cut]), "out_of_sample": _stats(nets[cut:]),
        "halts": run.get("halts", []), "days_halted": len(run.get("halts", [])),
        "equity": _downsample(run["equity"]),
        "errors": run.get("errors", []), "assumptions": run["assumptions"],
    }


def build_verdict(sums: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """`sums` maps variant name -> summarize() output. Returns {"overall", "points"}."""
    pts: list[dict[str, str]] = []

    def add(level: str, text: str) -> None:
        pts.append({"level": level, "text": text})

    st = sums.get("strategy")
    if not st:
        return {"overall": "inconclusive", "points": [{"level": "bad", "text": "The strategy run didn't complete."}]}
    n, net = st["n"], st["net"]
    bal = st["start_balance"]
    promising = True

    if n < MIN_TRADES_ANY:
        add("bad", f"Only {n} trades. That's far too few to conclude anything: use more days of data or looser filters.")
        return {"overall": "inconclusive", "points": pts + _caveat()}
    if n < MIN_TRADES_MEANINGFUL:
        add("warn", f"{n} trades is a small sample (aim for 200+). Luck can easily explain what you see.")
        promising = False

    if net <= 0:
        add("bad", f"Lost money after costs: {net:+.2f} ({st['net_pct']:+.1f}% of the starting balance) over {n} trades.")
        promising = False
    else:
        add("info", f"Made {net:+.2f} ({st['net_pct']:+.1f}%) after costs over {n} trades; max drawdown {st['max_drawdown']:.2f} ({st['max_drawdown_pct']:.1f}%).")
        ci = st["ci95"]
        if ci and ci[0] > 0:
            add("good", f"Average trade {st['expectancy']:+.2f} (95% interval {ci[0]:+.2f} to {ci[1]:+.2f}): above zero beyond normal luck.")
        else:
            add("warn", f"Average trade {st['expectancy']:+.2f}, but the 95% interval ({ci[0]:+.2f} to {ci[1]:+.2f}) includes zero: this profit could be noise.")
            promising = False

    if st["gross_before_costs"] > 0 and st["costs_pct_of_gross"] is not None and st["costs_pct_of_gross"] >= 50:
        add("warn", f"Spread, slippage and commission ate {st['costs_pct_of_gross']:.0f}% of the gross profit. Thin targets are fragile.")
    elif st["gross_before_costs"] <= 0 and n >= MIN_TRADES_ANY:
        add("bad", f"Even before costs the trades lost {st['gross_before_costs']:+.2f}: the entries themselves have no edge.")

    base = sums.get("baseline")
    if base and base["n"] >= MIN_TRADES_ANY and st["se"] and base["se"]:
        diff = st["expectancy"] - base["expectancy"]
        se = float(np.hypot(st["se"], base["se"]))
        if diff > 1.64 * se:
            add("good", f"Beats a coin-flip direction at the same entry times by {diff:+.2f}/trade (random: {base['expectancy']:+.2f}): the signal's direction has some skill.")
        else:
            add("bad" if diff <= 0 else "warn",
                f"Not distinguishable from a random direction at the same entry times ({st['expectancy']:+.2f} vs {base['expectancy']:+.2f} per trade). "
                "The signal's direction adds no demonstrable value.")
            promising = False
    elif base:
        add("warn", "Too few baseline trades to compare against random direction.")
        promising = False

    shuf = sums.get("shuffle")
    if shuf and shuf["n"] >= MIN_TRADES_ANY and st["se"] and shuf["se"]:
        diff = st["expectancy"] - shuf["expectancy"]
        se = float(np.hypot(st["se"], shuf["se"]))
        if diff > 1.64 * se:
            add("good", f"Beats the same strategy run on scrambled prices by {diff:+.2f}/trade (scrambled: {shuf['expectancy']:+.2f}): "
                        "it is picking up something real in the price sequence, not just luck or the replay.")
        else:
            add("bad" if diff <= 0 else "warn",
                f"No better than on scrambled prices ({st['expectancy']:+.2f} vs {shuf['expectancy']:+.2f} per trade). "
                "Shuffling destroys any real market structure, so this gives no evidence the signal exploits any.")
            promising = False
    elif shuf:
        add("warn", "Too few trades on the scrambled-price control to compare.")
        promising = False

    stress = sums.get("stress")
    if stress:
        if stress["net"] <= 0 < net:
            add("warn", f"Profit disappears if costs are 2× higher ({stress['net']:+.2f}). Wider spreads (news, rollover, a worse broker) would turn this into a loss.")
            promising = False
        elif stress["net"] > 0:
            add("good", f"Still profitable with costs doubled ({stress['net']:+.2f}).")

    oos, ins = st["out_of_sample"], st["in_sample"]
    if oos["n"] >= 20 and ins["n"] >= 20:
        if oos["net"] <= 0 < ins["net"]:
            add("warn", f"Profitable in the first 60% of trades ({ins['net']:+.2f}) but not in the last 40% ({oos['net']:+.2f}): not stable.")
            promising = False
        elif oos["net"] > 0 and ins["net"] > 0:
            add("good", f"Profitable in both halves (first 60%: {ins['net']:+.2f}, last 40%: {oos['net']:+.2f}).")

    if st["days_halted"]:
        add("info", f"A session rule stopped the bot on {st['days_halted']} day(s) (e.g. “{st['halts'][0]['reason']}”).")
    if st["errors"]:
        add("warn", f"The engine raised errors during the test: {st['errors'][0]}")

    overall = "promising" if promising and net > 0 else ("not_viable" if net <= 0 else "inconclusive")
    return {"overall": overall, "points": pts + _caveat()}


def _caveat() -> list[dict[str, str]]:
    return [{"level": "info", "text": (
        "Backtests flatter reality: no requotes or rejected orders, no news-time spread spikes unless they're in the data, and the order of "
        "prices inside each minute is assumed. Even a 'promising' result only earns a demo-account trial, never live money. "
        "And don't tune settings on this same data until it looks good: that is how people fool themselves.")}]
