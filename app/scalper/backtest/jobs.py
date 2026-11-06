"""
Backtest jobs.

Each variant (strategy / baseline / shuffle / stress) runs in its OWN OS process
(`python -m app.scalper.backtest worker ...`). That keeps a CPU-heavy backtest from
ever stalling the API server -- and therefore a live scalper running in it -- and lets
the variants run in parallel on separate cores.

Job folder (<SCALPER_STATE_DIR>/backtests/<job_id>/):
  request.json  progress_<v>.json  result_<v>.json  trades_<v>.csv  worker_<v>.log  combined.json  data/
"""
from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.config import get_settings
from app.scalper.backtest.metrics import build_verdict
from app.scalper.backtest.runner import VARIANTS
from app.scalper.config import ScalperConfig

SERVER_ROOT = Path(__file__).resolve().parents[3]
_PROCS: dict[str, dict[str, subprocess.Popen]] = {}


class BacktestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: Literal["mt5", "csv", "synthetic"] = "mt5"
    days: int = Field(10, ge=1, le=250)                    # trading days of history
    instruments: list[str] | None = None                   # default: the scalper's current pairs
    csv_dir: str | None = None                             # folder with EURUSD.csv ... (source="csv")
    csv_utc_offset_hours: float = Field(0.0, ge=-14, le=14)  # how far the CSV clock is ahead of UTC (MT5 exports: broker time)
    spread_pips: float | None = Field(None, ge=0, le=50)   # fixed spread; None = real per-bar spread if the data has it
    slippage_pips: float = Field(0.2, ge=0, le=10)
    commission_per_lot: float = Field(0.0, ge=0, le=100)   # round-turn, account currency
    start_balance: float | None = Field(None, ge=1, le=100_000_000)  # None: your MT5 account's equity (mt5), else 10,000
    stops_level_points: int = Field(0, ge=0, le=1000)
    variants: list[str] = Field(default_factory=lambda: list(VARIANTS))
    config_overrides: dict[str, Any] = Field(default_factory=dict)  # try other settings without touching the live ones
    seed: int = Field(1, ge=0, le=10_000)
    session_mode: Literal["day", "run"] = "day"            # day: limits reset every UTC day; run: first halt ends the test
    synthetic_trending: bool = False                       # synthetic only: plant persistent trends (positive control)

    @field_validator("variants")
    @classmethod
    def _variants(cls, v: list[str]) -> list[str]:
        bad = [x for x in v if x not in VARIANTS]
        if bad or "strategy" not in v:
            raise ValueError(f"variants must include 'strategy' and only use {list(VARIANTS)}")
        return list(dict.fromkeys(v))


def jobs_root() -> Path:
    root = Path(get_settings().scalper_state_dir) / "backtests"
    root.mkdir(parents=True, exist_ok=True)
    return root


def job_dir(job_id: str) -> Path:
    if not job_id.replace("-", "").isalnum():
        raise KeyError(job_id)
    d = jobs_root() / job_id
    if not d.is_dir():
        raise KeyError(job_id)
    return d


def _json_default(o: Any) -> Any:
    try:
        import numpy as np
        if isinstance(o, np.integer):
            return int(o)
        if isinstance(o, np.floating):
            return float(o)
        if isinstance(o, np.ndarray):
            return o.tolist()
    except ImportError:  # pragma: no cover
        pass
    raise TypeError(f"not JSON serialisable: {type(o)}")


def write_json(path: Path, obj: Any, *, best_effort: bool = False) -> None:
    """Atomically write JSON.

    On Windows, os.replace() raises PermissionError (WinError 5/32) if another
    process (the dashboard polling progress, antivirus, OneDrive/Desktop sync)
    has the destination open at that instant. Retry briefly; if it still fails
    and best_effort is set (progress files), skip rather than kill the job.
    """
    import time

    data = json.dumps(obj, default=_json_default)
    # unique tmp name so two writers never fight over the same .tmp file
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(data, encoding="utf-8")
        last_err: Exception | None = None
        for attempt in range(8):
            try:
                os.replace(tmp, path)
                return
            except PermissionError as e:  # Windows: file briefly locked
                last_err = e
                time.sleep(0.05 * (attempt + 1))
        # last resort: write in place (non-atomic, but readers tolerate it)
        try:
            path.write_text(data, encoding="utf-8")
            return
        except OSError as e:
            last_err = e
        if not best_effort:
            raise last_err  # type: ignore[misc]
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def read_json(path: Path) -> Any | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


# ------------------------------------------------------------------ create / spawn
def create_job(req: BacktestRequest, base_cfg: ScalperConfig) -> tuple[str, ScalperConfig]:
    """Validate overrides against the real config rules, then create the job folder."""
    cfg = ScalperConfig.model_validate({**base_cfg.model_dump(), **req.config_overrides})
    if req.instruments:
        cfg = ScalperConfig.model_validate({**cfg.model_dump(), "instruments": req.instruments})
    job_id = time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3)
    d = jobs_root() / job_id
    (d / "data").mkdir(parents=True)
    write_json(d / "request.json", {"request": req.model_dump(), "config": cfg.model_dump(), "created": time.time()})
    return job_id, cfg


def spawn_workers(job_id: str) -> None:
    d = job_dir(job_id)
    variants = read_json(d / "request.json")["request"]["variants"]
    procs: dict[str, subprocess.Popen] = {}
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    for v in variants:
        log = open(d / f"worker_{v}.log", "wb")
        procs[v] = subprocess.Popen(
            [sys.executable, "-m", "app.scalper.backtest", "worker", "--job-dir", str(d), "--variant", v],
            cwd=str(SERVER_ROOT), stdout=log, stderr=subprocess.STDOUT, creationflags=flags,
        )
    _PROCS[job_id] = procs


def running_job_id() -> str | None:
    """A job is running until every variant has a result (or failed/was cancelled). A worker process that is merely
    still exiting after writing its result doesn't count -- that race once wrongly blocked the next backtest."""
    for jid, procs in list(_PROCS.items()):
        if not any(p.poll() is None for p in procs.values()):
            continue
        try:
            if job_status(jid)["state"] == "running":
                return jid
        except KeyError:  # its folder was deleted; forget the handle
            _PROCS.pop(jid, None)
    return None


def cancel_job(job_id: str) -> bool:
    d = job_dir(job_id)
    (d / "cancelled").write_text("1")
    for p in _PROCS.get(job_id, {}).values():
        if p.poll() is None:
            p.terminate()
    return True


# ------------------------------------------------------------------ status
def _tail(path: Path, n: int = 600) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""
    return text[-n:]


def job_status(job_id: str) -> dict[str, Any]:
    d = job_dir(job_id)
    meta = read_json(d / "request.json") or {}
    variants = meta.get("request", {}).get("variants", [])
    procs = _PROCS.get(job_id, {})
    states: dict[str, dict[str, Any]] = {}
    for v in variants:
        prog = read_json(d / f"progress_{v}.json") or {}
        pct = (prog.get("done", 0) / prog["total"]) if prog.get("total") else 0.0
        if (d / f"result_{v}.json").exists():
            states[v] = {"state": "done", "progress": 1.0}
        elif (d / "cancelled").exists():
            states[v] = {"state": "cancelled", "progress": pct}
        elif v in procs and procs[v].poll() is None:
            states[v] = {"state": "running", "progress": pct}
        elif (d / f"error_{v}.txt").exists() or v in procs:
            msg = _tail(d / f"error_{v}.txt") or _tail(d / f"worker_{v}.log")
            states[v] = {"state": "failed", "progress": pct, "error": msg or "worker exited without a result"}
        elif time.time() - meta.get("created", 0) < 5:
            states[v] = {"state": "running", "progress": pct}
        else:
            states[v] = {"state": "interrupted", "progress": pct, "error": "The backend restarted while this was running."}

    kinds = {s["state"] for s in states.values()}
    if kinds == {"done"}:
        state = "done"
    elif "failed" in kinds:
        state = "failed"
    elif "cancelled" in kinds:
        state = "cancelled"
    elif "interrupted" in kinds:
        state = "interrupted"
    else:
        state = "running"

    result = None
    if state == "done":
        result = read_json(d / "combined.json")
        if result is None:
            sums = {v: read_json(d / f"result_{v}.json") for v in variants}
            result = {"summaries": sums, "verdict": build_verdict(sums), "notes": _notes(meta)}
            write_json(d / "combined.json", result)
    errors = [f"{v}: {s['error']}" for v, s in states.items() if s.get("error")]
    return {
        "job_id": job_id, "state": state, "created": meta.get("created"),
        "request": meta.get("request"), "variants": states,
        "progress": sum(s["progress"] for s in states.values()) / max(len(states), 1),
        "result": result, "error": errors[0] if errors else None,
    }


def _notes(meta: dict[str, Any]) -> list[str]:
    r = meta.get("request", {})
    notes = [
        "Replays your real scalper engine over historical 1-minute candles; each minute is rebuilt as a 60-second price path through the real open/high/low/close.",
        "Measured on synthetic data with known paths, that reconstruction is slightly pessimistic for momentum entries (about 0.03 account-currency per 0.01-lot trade, ~0.3 pip).",
        "Not modelled: requotes/rejections, margin, swap, news spread spikes that aren't in the data, partial fills. Account currency assumed USD.",
    ]
    if r.get("source") == "synthetic":
        notes.insert(0, "SYNTHETIC DATA: this only tests the machinery. Results say nothing about real markets.")
    if r.get("source") == "csv":
        notes.append("CSV spreads in MT5 exports are assumed to be in points with 10 points = 1 pip; set a fixed spread if your broker differs.")
    if r.get("spread_pips") is None and r.get("source") == "csv":
        notes.append("If the CSV has no spread column, typical default spreads are used (EUR/USD 0.8, GBP/USD 1.2, XAU/USD 2.5 pips).")
    return notes


def list_jobs(limit: int = 15) -> list[dict[str, Any]]:
    out = []
    for d in sorted((p for p in jobs_root().iterdir() if p.is_dir()), reverse=True)[:limit]:
        st = job_status(d.name)
        strat = ((st.get("result") or {}).get("summaries") or {}).get("strategy") or {}
        out.append({
            "job_id": d.name, "state": st["state"], "created": st["created"], "source": (st.get("request") or {}).get("source"),
            "days": (st.get("request") or {}).get("days"), "overall": ((st.get("result") or {}).get("verdict") or {}).get("overall"),
            "net": strat.get("net"), "trades": strat.get("n"),
        })
    return out


# ------------------------------------------------------------------ data preparation (runs in the API process / CLI)
async def prepare_data(job_id: str, req: BacktestRequest, cfg: ScalperConfig) -> dict[str, Any]:
    """Make sure the worker processes will find their data. MT5 history is pulled here (the terminal connection lives
    in this process) and saved as CSVs in the job folder; CSV sources are checked for existence; synthetic needs nothing."""
    from app.scalper.backtest.data import DataError, find_csv, fetch_mt5_history

    d = job_dir(job_id)
    if req.source == "mt5":
        from app.mt5.client import MT5Unavailable, get_mt5_client
        try:
            return await fetch_mt5_history(get_mt5_client(), cfg.instruments, get_settings().mt5_symbol_suffix, req.days, d / "data")
        except MT5Unavailable as exc:
            raise DataError(str(exc)) from exc
    if req.source == "csv":
        if not req.csv_dir:
            raise DataError("Enter the folder that contains your CSV files (EURUSD.csv, GBPUSD.csv, ...).")
        return {i: {"file": str(find_csv(req.csv_dir, i))} for i in cfg.instruments}
    return {}
