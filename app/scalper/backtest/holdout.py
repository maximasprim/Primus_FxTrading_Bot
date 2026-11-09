"""
The locked test period ("holdout").

The problem it solves: every time you look at a backtest and then tweak a setting, you fit the settings a little more
to that data. After enough tweaks a strategy looks great on the data you tuned on and fails on anything new. The only
honest number is one measured on data that played NO part in your decisions.

How it works
------------
* The first time you run a backtest in "Explore" mode, the last ~30% of the loaded period is LOCKED: a split time is
  stored here. From then on, Explore runs only ever see data BEFORE that time (the worker physically removes the rest;
  the tests check no trade can open or close after the split).
* "Confirm" runs the same engine on the locked period (with a short warm-up before it so indicators are primed, but no
  trading before the split). It can be used ONCE per lock. That one number is the one to believe.
* After a confirmation the lock is "spent". Looking again would no longer be a clean test, so a second confirm is refused.
  You can reset the lock to start a new one, but the old locked period then counts as seen, and the history says so.

Nothing here stops you from fooling yourself outside the tool (e.g. peeking at charts of the locked period). It just
makes the honest path the easy path.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

from app.config import get_settings

MIN_FRACTION, MAX_FRACTION = 0.10, 0.50


class HoldoutError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def _path() -> Path:
    return Path(get_settings().scalper_state_dir) / "backtests" / "holdout.json"


def _load() -> dict[str, Any]:
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data.setdefault("current", None)
            data.setdefault("history", [])
            return data
    except (OSError, ValueError):
        pass
    return {"current": None, "history": []}


def _save(state: dict[str, Any]) -> None:
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    os.replace(tmp, p)


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def config_hash(cfg: dict[str, Any]) -> str:
    return hashlib.sha1(json.dumps(cfg, sort_keys=True, default=str).encode()).hexdigest()[:10]


def get() -> dict[str, Any] | None:
    return _load()["current"]


def pick_split(timeline_seconds: np.ndarray, fraction: float) -> float:
    if not MIN_FRACTION <= fraction <= MAX_FRACTION:
        raise HoldoutError(f"The locked share must be between {MIN_FRACTION:.0%} and {MAX_FRACTION:.0%}.")
    t = np.unique(np.asarray(timeline_seconds, dtype=np.int64))
    if len(t) < 600:
        raise HoldoutError("Not enough history to lock a test period (need at least ~600 candles). Load more days.")
    return float(t[int(len(t) * (1.0 - fraction))])


def create(split_ts: float, fraction: float, source: str, instruments: list[str], window: tuple[float, float]) -> dict[str, Any]:
    state = _load()
    lock_id = len(state["history"]) + 1
    state["current"] = {
        "lock_id": lock_id, "split_ts": split_ts, "split_iso": _iso(split_ts), "fraction": fraction, "created_at": time.time(),
        "source": source, "instruments": instruments, "window_start": window[0], "window_end": window[1],
        "last_explore": None, "confirmed": None,
    }
    _save(state)
    return state["current"]


def reset() -> dict[str, Any] | None:
    """Drop the current lock. Its period now counts as SEEN; the history keeps a record of that."""
    state = _load()
    old = state["current"]
    if old is not None:
        state["history"].append({
            "lock_id": old["lock_id"], "split_iso": old["split_iso"], "created_at": old["created_at"], "reset_at": time.time(),
            "was_confirmed": bool(old.get("confirmed")), "confirmed_net_pct": (old.get("confirmed") or {}).get("summary", {}).get("net_pct"),
        })
        state["current"] = None
        _save(state)
    return old


def record_explore(lock_id: int, job_id: str, cfg_hash: str, summary: dict[str, Any], overall: str) -> None:
    state = _load()
    cur = state["current"]
    if cur is None or cur["lock_id"] != lock_id:
        return
    cur["last_explore"] = {"job_id": job_id, "config_hash": cfg_hash, "at": time.time(), "summary": summary, "overall": overall}
    _save(state)


def record_confirm(lock_id: int, job_id: str, cfg_hash: str, summary: dict[str, Any], overall: str) -> bool:
    """Spend the lock. Idempotent for the same job; returns False if it was already spent by a different job."""
    state = _load()
    cur = state["current"]
    if cur is None or cur["lock_id"] != lock_id:
        return False
    if cur["confirmed"] and cur["confirmed"]["job_id"] != job_id:
        return False
    cur["confirmed"] = {"job_id": job_id, "config_hash": cfg_hash, "at": time.time(), "summary": summary, "overall": overall}
    _save(state)
    return True


def public_view() -> dict[str, Any]:
    state = _load()
    cur = state["current"]
    if cur is None:
        return {"locked": False, "history": state["history"]}
    return {
        "locked": True, "lock_id": cur["lock_id"], "split_ts": cur["split_ts"], "split_iso": cur["split_iso"], "fraction": cur["fraction"],
        "created_at": cur["created_at"], "source": cur["source"], "instruments": cur["instruments"],
        "last_explore": cur["last_explore"], "confirmed": cur["confirmed"], "history": state["history"],
    }
