"""Runs one backtest variant: the real ScalperEngine driven over a ReplayGateway."""
from __future__ import annotations

import asyncio
import dataclasses
import logging
from datetime import datetime, timezone
from typing import Any, Callable

import numpy as np
import pandas as pd

from app.scalper.backtest.data import shuffle_bars
from app.scalper.backtest.replay_gateway import Assumptions, ReplayGateway
from app.scalper.config import ScalperConfig
from app.scalper.engine import HALTED, RUNNING, ScalperEngine
from app.scalper.models import SignalResult
from app.scalper.signals import compute_signal

VARIANTS = ("strategy", "baseline", "shuffle", "stress")
_MAX_ERRORS = 50


class _MemStore:
    def __init__(self, cfg: ScalperConfig):
        self.cfg = cfg

    def get(self) -> ScalperConfig:
        return self.cfg


def _random_direction_signal(seed: int) -> Callable:
    """Same entry TIMING and score as the real signal, but the direction is a coin flip.
    If the real signal's direction has skill, the strategy must beat this."""
    rng = np.random.default_rng(seed)

    def fn(candles, spread_pips, pip_size) -> SignalResult:
        r = compute_signal(candles, spread_pips, pip_size)
        if r.side is None:
            return r
        side = "BUY" if rng.random() < 0.5 else "SELL"
        return SignalResult(side, r.score, r.atr_pips, ["random direction (baseline)"])

    return fn


async def run_variant(
    data: dict[str, pd.DataFrame],
    cfg: ScalperConfig,
    assumptions: Assumptions,
    variant: str = "strategy",
    seed: int = 1,
    session_mode: str = "day",
    progress: Callable[[int, int], None] | None = None,
    true_paths: dict[str, np.ndarray] | None = None,
) -> dict[str, Any]:
    if variant not in VARIANTS:
        raise ValueError(f"unknown variant {variant!r}")
    engine_log = logging.getLogger("app.scalper.engine")  # thousands of simulated halts/closes: keep the console clean
    previous_level = engine_log.level
    engine_log.setLevel(logging.ERROR)
    try:
        return await _run(data, cfg, assumptions, variant, seed, session_mode, progress, true_paths)
    finally:
        engine_log.setLevel(previous_level)


async def _run(data, cfg, assumptions, variant, seed, session_mode, progress, true_paths=None) -> dict[str, Any]:
    a = dataclasses.replace(assumptions)
    if variant == "stress":
        a.cost_multiplier = assumptions.cost_multiplier * 2.0
    cfg = cfg.model_copy(update={"instruments": [i for i in cfg.instruments if i in data]})
    if variant == "shuffle":  # same strategy, same costs, but candle ORDER scrambled: the "no real structure" control
        data = {i: shuffle_bars(df, seed * 101 + k) for k, (i, df) in enumerate(data.items())}
        true_paths = None

    gw = ReplayGateway({i: data[i] for i in cfg.instruments}, a, true_paths)
    signal_fn = _random_direction_signal(seed) if variant == "baseline" else None
    engine = ScalperEngine(lambda c: gw, _MemStore(cfg), gw.clock, None, signal_fn=signal_fn, persist_log=False)
    engine.observer = gw.note_open

    gw.begin_bar(0)
    gw.set_phase(0)
    await engine._begin_session()

    total = len(gw.timeline)
    errors: list[str] = []
    n_errors = 0
    halts: list[dict[str, Any]] = []
    halt_logged = False
    cur_day: int | None = None

    for step in range(total):
        gw.begin_bar(step)
        day = int(gw.timeline[step] // 86400)
        if day != cur_day:
            if session_mode == "day" or cur_day is None:
                engine.new_session(gw.last_equity())
                halt_logged = False
            cur_day = day
        for phase in range(4):
            gw.set_phase(phase)
            if engine.state != RUNNING:
                continue
            try:
                await engine._cycle(cfg)
            except Exception as exc:  # noqa: BLE001 -- the live engine also survives per-cycle errors
                n_errors += 1
                msg = f"{type(exc).__name__}: {exc}"
                if msg not in errors and len(errors) < 5:
                    errors.append(msg)
                if n_errors > _MAX_ERRORS:
                    raise RuntimeError(f"Aborted: too many engine errors. First: {errors[0]}") from exc
            await asyncio.sleep(0)  # lets a scheduled halt task run
        gw.end_bar()
        if engine.state == HALTED and not halt_logged:
            halts.append({"t": gw.now, "reason": engine.halt_reason})
            halt_logged = True
            if session_mode == "run":
                break
        if progress and (step % 250 == 0 or step == total - 1):
            progress(step + 1, total)

    for p in await gw.positions():  # don't leave trades hanging at the end of the data
        await gw.close(p.ticket, reason="end of test")
    for t in gw.trades:
        t["score"] = gw.scores.get(t["ticket"])

    return {
        "variant": variant,
        "bars": total,
        "start": datetime.fromtimestamp(float(gw.timeline[0]), tz=timezone.utc).isoformat(),
        "end": datetime.fromtimestamp(float(gw.timeline[-1]), tz=timezone.utc).isoformat(),
        "instruments": cfg.instruments,
        "trades": gw.trades,
        "equity": gw.equity_curve,
        "halts": halts,
        "errors": errors,
        "n_errors": n_errors,
        "start_balance": a.start_balance,
        "assumptions": dataclasses.asdict(a),
    }
