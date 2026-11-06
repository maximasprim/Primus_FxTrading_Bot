"""
Historical data for the backtester.

Sources
-------
* CSV files        one per pair (EURUSD.csv ...), M1 candles. Understands MT5's own export
                   (History Center -> Export: <DATE> <TIME> <OPEN> ... <SPREAD>), Dukascopy-style
                   and generic `time,open,high,low,close[,spread]` files.
* MT5 terminal     last N one-minute bars straight from the terminal (also records the real
                   per-bar spread, which is what makes cost modelling honest).
* Synthetic        random walk, for testing the plumbing. `trending=False` has NO edge by
                   construction (used as the "null test"); `trending=True` has persistent drift
                   (used as a "positive control": a working backtester must find an edge there).

All frames: UTC DatetimeIndex, float columns open/high/low/close, optional `spread_pips`.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from app.core.pip_utils import get_pip_size
from app.scalper.sim_gateway import _SIGMA_PIPS_PER_SEC, _SPREAD_PIPS, _START_PRICE

REQUIRED = ["open", "high", "low", "close"]


class DataError(ValueError):
    """Bad or missing historical data -- the message is shown to the user."""


# --------------------------------------------------------------------------- CSV
def _clean_columns(df: pd.DataFrame) -> pd.DataFrame:
    df.columns = [str(c).strip().strip("<>").strip().lower().replace(" ", "_") for c in df.columns]
    return df


def _parse_time(df: pd.DataFrame) -> pd.Series:
    cols = set(df.columns)
    if {"date", "time"} <= cols:
        raw = df["date"].astype(str).str.strip() + " " + df["time"].astype(str).str.strip()
    else:
        key = next((c for c in ("datetime", "timestamp", "time", "gmt_time", "local_time", "date") if c in cols), None)
        if key is None:
            raise DataError("No time column found. Expected 'time', 'datetime', 'timestamp' or MT5's <DATE>/<TIME>.")
        raw = df[key]
    if pd.api.types.is_numeric_dtype(raw):
        unit = "ms" if float(raw.iloc[0]) > 1e11 else "s"
        return pd.to_datetime(raw, unit=unit, utc=True, errors="coerce")
    sample = str(raw.iloc[0])
    dayfirst = len(sample) > 2 and sample[2] == "." and sample[:2].isdigit()  # 31.12.2024 style
    return pd.to_datetime(raw, utc=True, errors="coerce", dayfirst=dayfirst, format="mixed")


def load_csv(path: str | Path, utc_offset_hours: float = 0.0) -> pd.DataFrame:
    """`utc_offset_hours`: hours the file's clock is AHEAD of UTC (MT5 exports use broker time, e.g. 2 or 3)."""
    p = Path(path)
    if not p.exists():
        raise DataError(f"File not found: {p}")
    try:
        df = pd.read_csv(p, sep=None, engine="python")
    except Exception as exc:  # noqa: BLE001
        raise DataError(f"Could not read {p.name}: {exc}") from exc
    df = _clean_columns(df)
    missing = [c for c in REQUIRED if c not in df.columns]
    if missing:
        raise DataError(f"{p.name} is missing column(s): {', '.join(missing)}. Found: {', '.join(df.columns)}")
    ts = _parse_time(df)
    out = pd.DataFrame({c: pd.to_numeric(df[c], errors="coerce") for c in REQUIRED})
    out.index = pd.DatetimeIndex(ts - pd.Timedelta(hours=utc_offset_hours))
    # .to_numpy(): df has a RangeIndex but `out` is now time-indexed -- assigning a Series would misalign to NaN.
    if "spread_pips" in df.columns:
        out["spread_pips"] = pd.to_numeric(df["spread_pips"], errors="coerce").to_numpy()
    elif "spread" in df.columns:  # MT5 exports spread in POINTS; assume 5/3-digit pricing (1 pip = 10 points)
        out["spread_pips"] = pd.to_numeric(df["spread"], errors="coerce").to_numpy() / 10.0
    out = out[out.index.notna()].dropna(subset=REQUIRED)
    out = out[~out.index.duplicated(keep="first")].sort_index()
    if len(out) < 200:
        raise DataError(f"{p.name} has only {len(out)} usable rows -- need at least a few hundred one-minute candles.")
    if (out["high"] < out["low"]).any():
        raise DataError(f"{p.name} has candles where high < low -- is the column order right?")
    gaps = out.index.to_series().diff().dt.total_seconds().dropna()
    if gaps.median() > 90:
        raise DataError(f"{p.name} doesn't look like 1-minute data (median gap {gaps.median():.0f}s). Export M1.")
    return out


def find_csv(directory: str | Path, instrument: str) -> Path:
    d = Path(directory)
    if not d.is_dir():
        raise DataError(f"Folder not found: {d}")
    sym = instrument.replace("/", "").upper()
    hits = sorted((f for f in d.iterdir() if f.suffix.lower() == ".csv" and sym in f.stem.upper()), key=lambda f: len(f.name))
    if not hits:
        raise DataError(f"No CSV for {instrument} in {d}. Name it like {sym}.csv (or {sym}_M1.csv).")
    return hits[0]


def load_dir(directory: str | Path, instruments: list[str], utc_offset_hours: float = 0.0) -> dict[str, pd.DataFrame]:
    return {i: load_csv(find_csv(directory, i), utc_offset_hours) for i in instruments}


def limit_days(df: pd.DataFrame, trading_days: int) -> pd.DataFrame:
    """Keep the last `trading_days` days that actually have candles."""
    days = df.index.normalize().unique()
    if len(days) <= trading_days:
        return df
    return df[df.index >= days[-trading_days]]


# --------------------------------------------------------------------------- MT5
async def fetch_mt5_history(client, instruments: list[str], suffix: str, trading_days: int, out_dir: Path) -> dict[str, dict]:
    """Pull the last ~N trading days of M1 candles (+ real spreads) from the terminal into CSV files."""
    import json
    out_dir.mkdir(parents=True, exist_ok=True)
    meta: dict[str, dict] = {}
    acct = await client.call("account_info")
    account = {"equity": float(acct.equity), "currency": str(acct.currency)} if acct is not None else {}
    want = min(int(trading_days * 1440 * 1.02) + 50, 120_000)
    for instrument in instruments:
        symbol = instrument.replace("/", "") + suffix
        info = await client.call("symbol_info", symbol)
        if info is None:
            raise DataError(f"Symbol {symbol} not found in MT5 (check MT5_SYMBOL_SUFFIX).")
        await client.call("symbol_select", symbol, True)
        rates = await client.call("copy_rates_from_pos", symbol, client.const("TIMEFRAME_M1"), 0, want)
        if rates is None or len(rates) < 200:
            raise DataError(
                f"MT5 returned {0 if rates is None else len(rates)} bars for {symbol}. Open a 1-minute chart of it and scroll "
                "back to make the terminal download history, and check Tools > Options > Charts > 'Max bars in chart'."
            )
        offset = await client.server_offset_seconds(symbol)
        df = pd.DataFrame(rates)
        frame = pd.DataFrame({
            "time": pd.to_datetime(df["time"] - offset, unit="s", utc=True),
            "open": df["open"], "high": df["high"], "low": df["low"], "close": df["close"],
            "spread_pips": df["spread"] * float(info.point) / get_pip_size(instrument),
        })
        frame = limit_days(frame.set_index("time"), trading_days).reset_index()
        frame.to_csv(out_dir / f"{symbol}.csv", index=False)
        last_px = float(frame["close"].iloc[-1])
        pip = get_pip_size(instrument)
        profit = await client.call("order_calc_profit", client.const("ORDER_TYPE_BUY"), symbol, 1.0, last_px, last_px + pip)
        meta[instrument] = {"bars": len(frame), "first": str(frame["time"].iloc[0]), "last": str(frame["time"].iloc[-1]),
                            "pip_value_per_lot": None if profit is None else abs(float(profit))}
    (out_dir / "mt5_meta.json").write_text(json.dumps({"account": account, "instruments": meta}), encoding="utf-8")
    return meta


# --------------------------------------------------------------------------- synthetic
def synthetic(instruments: list[str], trading_days: int, seed: int = 1, trending: bool = False,
              return_paths: bool = False):
    """Random-walk M1 candles on weekdays. trending=False has no exploitable structure at all."""
    out: dict[str, pd.DataFrame] = {}
    paths: dict[str, np.ndarray] = {}
    n = trading_days * 1440
    stamps = pd.date_range("2025-01-06 00:00", periods=int(n * 7 / 5) + 2880, freq="1min", tz="UTC")
    stamps = stamps[stamps.dayofweek < 5][:n]
    for k, inst in enumerate(instruments):
        rng = np.random.default_rng(seed * 1000 + k)
        pip = get_pip_size(inst)
        sigma = _SIGMA_PIPS_PER_SEC.get(inst, 0.5) * pip
        drift = np.zeros(n)
        if trending:  # persistent regimes: the only structure a trend-follower could exploit
            i = 0
            while i < n:
                length = int(rng.integers(60, 400))
                drift[i:i + length] = rng.choice([-1, 1]) * 1.5 * pip  # ~40% of a bar's noise, per bar, sustained
                i += length
        steps = rng.normal(0.0, sigma, size=(n, 60)) + (drift / 60.0)[:, None]
        path = _START_PRICE.get(inst, 1.0) + np.cumsum(steps.reshape(-1)).reshape(n, 60)
        opens = np.r_[path[0, 0], path[:-1, -1]]
        out[inst] = pd.DataFrame({
            "open": opens, "high": np.maximum(path.max(axis=1), opens), "low": np.minimum(path.min(axis=1), opens),
            "close": path[:, -1],
        }, index=stamps)
        paths[inst] = path
    return (out, paths) if return_paths else out


def shuffle_bars(df: pd.DataFrame, seed: int) -> pd.DataFrame:
    """Scramble the ORDER of candles but keep each candle's own shape (range, body, gap) -- a "no market structure" twin.

    Anything a strategy earns from real serial structure (trends, momentum, mean reversion) is destroyed; costs,
    volatility distribution and the replay's own quirks stay the same. Used as the null hypothesis: a strategy that
    can't beat its own shuffled twin has shown no evidence of exploiting anything real. Timestamps and the spread
    column keep their original positions (time-of-day cost structure is preserved)."""
    rng = np.random.default_rng(seed)
    o, h, l, c = (df[k].to_numpy(dtype=float) for k in REQUIRED)
    gap = np.r_[0.0, o[1:] - c[:-1]]
    spans = np.maximum(h - l, 1e-12)
    gap = np.where(np.abs(gap) > 8 * np.median(spans), 0.0, gap)  # weekend/rollover jumps would land mid-session
    perm = rng.permutation(len(df))
    g, rel_c, rel_h, rel_l = gap[perm], (c - o)[perm], (h - o)[perm], (l - o)[perm]
    g[0] = 0.0
    close = o[0] + np.cumsum(g + rel_c)
    new_open = np.r_[o[0], close[:-1] + g[1:]]
    out = pd.DataFrame({"open": new_open, "high": new_open + rel_h, "low": new_open + rel_l, "close": new_open + rel_c}, index=df.index)
    if "spread_pips" in df.columns:
        out["spread_pips"] = df["spread_pips"].to_numpy()
    return out


def default_spread_pips(instrument: str) -> float:
    return _SPREAD_PIPS.get(instrument, 1.5)
