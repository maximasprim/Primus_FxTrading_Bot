"""
ReplayGateway: a ScalperGateway that replays historical 1-minute candles, so the
REAL ScalperEngine can be backtested with no separate copy of its logic.

How a bar is replayed
---------------------
Each M1 bar becomes a 60-step price path (one step = one second) that starts at the
real open, touches the real high and low at RANDOM times, and ends at the real close
(a bridge through O, H, L, C). The engine is run at fixed moments (0, 15, 30, 45 s;
live it runs ~2x/second) and sees the bar being *formed* (high/low so far), never a
future high/low -- see tests/test_backtest_replay.py::test_no_lookahead.

Why not just open->low->high->close? Because then the engine is always evaluated
exactly AT the bar's extreme and the next point is always a retreat, which silently
penalises every breakout/momentum entry. Random extreme times remove that bias
(tests/test_backtest_replay.py::test_no_extreme_bias).

Fill model (deliberately pessimistic where the data can't tell)
---------------------------------------------------------------
* Entries fill at ask (buys) / bid (sells) plus `slippage_pips` against you.
* A stop that is crossed fills at the stop, plus slippage. If price GAPS past it
  (new bar opens beyond it) it fills at the worse gap price.
* Take-profits fill at the level (a limit order: no slippage), or better on a gap.
* If a trade that was already open at the start of a bar could have hit BOTH its
  stop and its target inside that bar, the STOP is assumed to hit first.
* Spread comes from the data (real per-bar spread from MT5) or a fixed value.
* Commission (round turn, per lot) is charged on close.
Not modelled: requotes/rejections, margin and leverage, swap, news-time spread
spikes that aren't in the data, partial fills.
"""
from __future__ import annotations

import bisect
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from app.core.pip_utils import get_pip_size
from app.scalper.backtest.data import default_spread_pips
from app.scalper.gateway import ScalperGateway
from app.scalper.models import AccountSnapshot, Deal, OrderOutcome, ScalpPosition, SymbolSpec, Tick

EVAL_STEPS = (0, 15, 30, 45)   # seconds into the bar at which the engine runs
BAR_STEPS = 60                 # path resolution inside a bar (1 second)
_CONTRACT = {"XAU/USD": 100.0, "XAG/USD": 5000.0}  # units per 1.00 lot; FX default 100,000


@dataclass
class Assumptions:
    spread_pips: float | None = None   # fixed spread for every bar; None -> data's spread column, else typical defaults
    slippage_pips: float = 0.2         # against you on entries, stop exits and market exits
    commission_per_lot: float = 0.0    # round-turn, in account currency (raw-spread accounts: ~6-7 per lot)
    cost_multiplier: float = 1.0       # stress test: scales spread + slippage + commission
    start_balance: float = 10_000.0
    stops_level_points: int = 0        # broker minimum stop distance
    path_seed: int = 1                 # seeds the intrabar paths (same seed => identical prices for every variant)
    pip_values: dict | None = None     # instrument -> account-currency value of 1 pip on 1.0 lot, measured on the real account
                                       # (cent / micro / JPY / gold all handled); None -> standard-contract maths


@dataclass
class _Series:
    instrument: str
    pip: float
    contract: float
    base_is_usd: bool
    digits: int
    t: np.ndarray
    path: np.ndarray        # (n, 60) mid price at each second of each bar
    hi: np.ndarray
    lo: np.ndarray
    op: np.ndarray
    cl: np.ndarray
    spread_pips: np.ndarray  # per bar (already multiplied by cost_multiplier)
    step_to_bar: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=np.int64))
    pip_value: float | None = None


@dataclass
class _Pos:
    ticket: str
    instrument: str
    side: str
    volume: float
    entry: float
    entry_mid: float
    sl: float
    tp: float
    opened_at: float


def _bridge_paths(o: np.ndarray, h: np.ndarray, l: np.ndarray, c: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    """(n, 60) second-by-second mid prices that start at the real open, end at the real close and touch the
    real high and low, while staying DIFFUSIVE in between.

    1. Draw a free Brownian bridge from open to close (natural random extreme times, natural shape).
    2. Stretch the price axis piecewise-linearly so the bridge's own max/min land exactly on the real
       high/low (the part between open and close is left alone).
    A first attempt used straight lines open->high->low->close; that sawtooth silently penalised momentum
    entries (measured: -0.07 per trade vs the true path -- see tests/test_backtest_replay.py).
    """
    n = len(o)
    last = BAR_STEPS - 1
    t = np.arange(BAR_STEPS, dtype=float)[None, :]
    rng_ = np.maximum(h - l, 0.0)
    sigma = np.maximum(rng_, 1e-12)[:, None] / 6.0
    w = np.cumsum(rng.normal(size=(n, BAR_STEPS)), axis=1)
    w -= w[:, :1]
    bridge = w - (t / last) * w[:, -1:]
    x = o[:, None] + bridge * sigma + (t / last) * (c - o)[:, None]
    lo_oc, hi_oc = np.minimum(o, c)[:, None], np.maximum(o, c)[:, None]
    L, H = l[:, None], h[:, None]

    # make sure the free path has an interior excursion wherever the real bar needs one
    interior = x[:, 1:-1]
    spike = rng.integers(1, last, n)
    rows = np.arange(n)
    need_lo = (L[:, 0] < lo_oc[:, 0] - 1e-12) & (interior.min(axis=1) >= lo_oc[:, 0] - 1e-12)
    need_hi = (H[:, 0] > hi_oc[:, 0] + 1e-12) & (interior.max(axis=1) <= hi_oc[:, 0] + 1e-12)
    x[rows[need_lo], spike[need_lo]] = lo_oc[need_lo, 0] - 0.3 * sigma[need_lo, 0]
    spike2 = rng.integers(1, last, n)
    x[rows[need_hi], spike2[need_hi]] = hi_oc[need_hi, 0] + 0.3 * sigma[need_hi, 0]

    interior = x[:, 1:-1]
    mn, mx = interior.min(axis=1)[:, None], interior.max(axis=1)[:, None]
    down = np.where(lo_oc - mn > 1e-15, (x - mn) * (lo_oc - L) / np.where(lo_oc - mn > 1e-15, lo_oc - mn, 1.0) + L, L)
    up = np.where(mx - hi_oc > 1e-15, hi_oc + (x - hi_oc) * (H - hi_oc) / np.where(mx - hi_oc > 1e-15, mx - hi_oc, 1.0), H)
    y = np.where(x <= lo_oc, down, np.where(x >= hi_oc, up, x))
    y = np.clip(y, L, H)
    y[:, 0], y[:, -1] = o, c
    return y


def _epoch_seconds(index: pd.DatetimeIndex) -> np.ndarray:
    """UTC epoch seconds, independent of the index's resolution (pandas 2 = ns, pandas 3 = us)."""
    idx = pd.DatetimeIndex(index)
    idx = idx.tz_localize("UTC") if idx.tz is None else idx.tz_convert("UTC")
    return ((idx - pd.Timestamp("1970-01-01", tz="UTC")) // pd.Timedelta(seconds=1)).to_numpy(dtype=np.int64)


def _digits_for(instrument: str) -> int:
    return 3 if "JPY" in instrument else 2 if "XAU" in instrument else 3 if "XAG" in instrument else 5


class ReplayGateway(ScalperGateway):
    name = "backtest"

    def __init__(self, data: dict[str, pd.DataFrame], assumptions: Assumptions | None = None,
                 true_paths: dict[str, np.ndarray] | None = None):
        """`true_paths` (instrument -> (n, 60) array) replaces the reconstructed intrabar path. Only synthetic data has them;
        used to measure how much bias the OHLC reconstruction adds (tests/test_backtest_replay.py)."""
        self.a = assumptions or Assumptions()
        m = self.a.cost_multiplier
        self.series: dict[str, _Series] = {}
        for k, (inst, df) in enumerate(data.items()):
            pip = get_pip_size(inst)
            o, h, l, c = (df[k_].to_numpy(dtype=float) for k_ in ("open", "high", "low", "close"))
            path = (true_paths[inst] if true_paths and inst in true_paths
                    else _bridge_paths(o, h, l, c, np.random.default_rng(self.a.path_seed * 7919 + k)))
            if self.a.spread_pips is not None:
                sp = np.full(len(df), float(self.a.spread_pips))
            elif "spread_pips" in df.columns and df["spread_pips"].notna().mean() > 0.9:
                sp = df["spread_pips"].ffill().bfill().to_numpy(dtype=float)
            else:
                sp = np.full(len(df), default_spread_pips(inst))
            self.series[inst] = _Series(
                inst, pip, _CONTRACT.get(inst, 100_000.0), inst.startswith("USD/"), _digits_for(inst),
                _epoch_seconds(df.index),
                path, h, l, o, c, sp * m,
                pip_value=(self.a.pip_values or {}).get(inst),
            )
        # master timeline = union of every pair's bar times
        self.timeline = np.unique(np.concatenate([s.t for s in self.series.values()]))
        for s in self.series.values():
            idx = np.searchsorted(s.t, self.timeline)
            idx_clip = np.minimum(idx, len(s.t) - 1)
            s.step_to_bar = np.where(s.t[idx_clip] == self.timeline, idx_clip, -1)
        self.balance = self.a.start_balance
        self.now = float(self.timeline[0])
        self._step = 0
        self._phase = 0
        self._eval_step = 0            # second within the current bar the prices are currently at
        self._bar: dict[str, int] = {i: -1 for i in self.series}
        self._mid: dict[str, float | None] = {i: None for i in self.series}
        self._pos: dict[str, _Pos] = {}
        self._ids = 0
        self._deals: list[Deal] = []
        self._deal_times: list[float] = []
        self.trades: list[dict[str, Any]] = []
        self.scores: dict[str, int] = {}
        self.equity_curve: list[tuple[float, float]] = []
        self.slip = self.a.slippage_pips * m
        self.commission = self.a.commission_per_lot * m

    # ------------------------------------------------------------ time stepping (driven by the runner)
    def begin_bar(self, step: int) -> None:
        self._step = step
        self.now = float(self.timeline[step])
        self._eval_step = 0
        for inst, s in self.series.items():
            self._bar[inst] = int(s.step_to_bar[step])
        self._gap_check()
        self._ambiguity_check()

    def set_phase(self, phase: int) -> None:
        """Move prices to the engine's next evaluation moment, settling any stop/target crossed on the way."""
        self._phase = phase
        target = EVAL_STEPS[phase]
        self._scan(self._eval_step, target)
        self._advance_to(target)

    def end_bar(self) -> None:
        self._scan(self._eval_step, BAR_STEPS - 1)
        self._advance_to(BAR_STEPS - 1)
        self.equity_curve.append((self.now, self._equity()))

    def _advance_to(self, step_in_bar: int) -> None:
        self._eval_step = step_in_bar
        self.now = float(self.timeline[self._step]) + step_in_bar
        for inst, s in self.series.items():
            b = self._bar[inst]
            if b >= 0:
                self._mid[inst] = float(s.path[b, step_in_bar])

    def clock(self) -> float:
        return self.now

    def has_open_positions(self) -> bool:
        return bool(self._pos)

    # ------------------------------------------------------------ prices & money
    def _half_spread(self, inst: str) -> float:
        s = self.series[inst]
        b = self._bar[inst] if self._bar[inst] >= 0 else 0
        return float(s.spread_pips[b]) * s.pip / 2.0

    def _bid_ask(self, inst: str) -> tuple[float, float]:
        mid = self._mid[inst]
        h = self._half_spread(inst)
        return mid - h, mid + h

    def _money(self, inst: str, price_diff: float, volume: float, ref_price: float) -> float:
        s = self.series[inst]
        if s.pip_value is not None:  # broker-measured: price move -> pips -> account currency
            return price_diff / s.pip * s.pip_value * volume
        usd = price_diff * s.contract * volume
        return usd / ref_price if s.base_is_usd else usd

    def _floating(self, p: _Pos) -> float:
        bid, ask = self._bid_ask(p.instrument)
        diff = (bid - p.entry) if p.side == "BUY" else (p.entry - ask)
        return self._money(p.instrument, diff, p.volume, self._mid[p.instrument] or p.entry)

    def _equity(self) -> float:
        return self.balance + sum(self._floating(p) for p in self._pos.values() if self._mid[p.instrument] is not None)

    # ------------------------------------------------------------ settlement
    def _settle(self, p: _Pos, fill: float, reason: str, at: float | None = None, slip_amt: float = 0.0) -> float:
        """`slip_amt` = slippage (price units) already baked into `fill`; it is a COST, so it must not leak into gross."""
        when = self.now if at is None else at
        inst = p.instrument
        diff = (fill - p.entry) if p.side == "BUY" else (p.entry - fill)
        gross_fill = self._money(inst, diff, p.volume, fill)
        commission = self.commission * p.volume
        net = gross_fill - commission
        half = self._half_spread(inst)
        exit_mid = fill + slip_amt + half if p.side == "BUY" else fill - slip_amt - half
        mid_diff = (exit_mid - p.entry_mid) if p.side == "BUY" else (p.entry_mid - exit_mid)
        gross_mid = self._money(inst, mid_diff, p.volume, exit_mid)
        self.balance += net
        self._deals.append(Deal(p.ticket, inst, net, when))
        self._deal_times.append(when)
        pip = self.series[inst].pip
        self.trades.append({
            "ticket": p.ticket, "instrument": inst, "side": p.side, "volume": p.volume,
            "t_open": p.opened_at, "t_close": when, "entry": p.entry, "exit": fill,
            "pips": mid_diff / pip, "pips_net": diff / pip, "gross": gross_mid, "net": net,
            "cost": gross_mid - net, "reason": reason,
        })
        self._pos.pop(p.ticket, None)
        return net

    def _scan(self, s0: int, s1: int) -> None:
        """Settle every stop/target touched by the price path during seconds (s0, s1] of this bar, in time order."""
        if s1 <= s0 or not self._pos:
            return
        base = float(self.timeline[self._step])
        hits: list[tuple[int, str, float, str, float]] = []  # (second, ticket, fill, reason, slippage in fill)
        for p in self._pos.values():
            b = self._bar[p.instrument]
            if b < 0:
                continue
            s = self.series[p.instrument]
            h = float(s.spread_pips[b]) * s.pip / 2.0
            slip = self.slip * s.pip
            seg = s.path[b, s0 + 1:s1 + 1]
            n = len(seg)
            never = n + 1
            if p.side == "BUY":
                px = seg - h
                i_sl = int(np.argmax(px <= p.sl)) if p.sl and (px <= p.sl).any() else never
                i_tp = int(np.argmax(px >= p.tp)) if p.tp and (px >= p.tp).any() else never
                stop_fill = p.sl - slip
            else:
                px = seg + h
                i_sl = int(np.argmax(px >= p.sl)) if p.sl and (px >= p.sl).any() else never
                i_tp = int(np.argmax(px <= p.tp)) if p.tp and (px <= p.tp).any() else never
                stop_fill = p.sl + slip
            if i_sl == never and i_tp == never:
                continue
            if i_sl <= i_tp:  # a tie goes to the stop (pessimistic)
                hits.append((s0 + 1 + i_sl, p.ticket, stop_fill, "stop-loss", slip))
            else:
                hits.append((s0 + 1 + i_tp, p.ticket, p.tp, "take-profit", 0.0))
        for second, ticket, fill, reason, slip_amt in sorted(hits):
            pos = self._pos.get(ticket)
            if pos is not None:
                self._settle(pos, fill, reason, at=base + second, slip_amt=slip_amt)

    def _gap_check(self) -> None:
        """A bar that OPENS beyond a trade's stop fills at the (worse) open; beyond its target at the (better) open."""
        for p in list(self._pos.values()):
            inst = p.instrument
            b = self._bar[inst]
            if b < 0:
                continue
            s = self.series[inst]
            h = float(s.spread_pips[b]) * s.pip / 2.0
            o = float(s.path[b, 0])
            if p.side == "BUY":
                px = o - h
                if p.sl and px <= p.sl:
                    self._settle(p, px, "stop-loss")
                elif p.tp and px >= p.tp:
                    self._settle(p, px, "take-profit")
            else:
                px = o + h
                if p.sl and px >= p.sl:
                    self._settle(p, px, "stop-loss")
                elif p.tp and px <= p.tp:
                    self._settle(p, px, "take-profit")

    def _ambiguity_check(self) -> None:
        """Trades open at bar start whose range could reach BOTH stop and target: assume the stop hit first."""
        for p in list(self._pos.values()):
            inst = p.instrument
            b = self._bar[inst]
            if b < 0 or not (p.sl and p.tp):
                continue
            s = self.series[inst]
            h = float(s.spread_pips[b]) * s.pip / 2.0
            slip = self.slip * s.pip
            if p.side == "BUY":
                if s.lo[b] - h <= p.sl and s.hi[b] - h >= p.tp:
                    self._settle(p, min(p.sl, s.op[b] - h) - slip, "stop-loss", slip_amt=slip)
            else:
                if s.hi[b] + h >= p.sl and s.lo[b] + h <= p.tp:
                    self._settle(p, max(p.sl, s.op[b] + h) + slip, "stop-loss", slip_amt=slip)

    # ------------------------------------------------------------ ScalperGateway API
    async def account(self) -> AccountSnapshot:
        eq = self._equity()
        return AccountSnapshot(
            connected=True, login="BACKTEST", server="Replay", currency="USD", balance=self.balance, equity=eq,
            is_demo=True, trade_allowed=True, algo_trading_enabled=True, hedging=True, backend="backtest",
        )

    async def spec(self, instrument: str) -> SymbolSpec:
        s = self.series[instrument]
        return SymbolSpec(instrument, instrument.replace("/", ""), s.digits, 10.0 ** -s.digits, s.pip,
                          0.01, 100.0, 0.01, self.a.stops_level_points)

    async def tick(self, instrument: str) -> Tick | None:
        if self._bar[instrument] < 0 or self._mid[instrument] is None:
            return None  # this pair has no candle at this minute (e.g. gold's daily break) -> "market closed"
        bid, ask = self._bid_ask(instrument)
        return Tick(bid, ask, self.now)

    async def candles(self, instrument: str, count: int = 120) -> pd.DataFrame:
        s, b = self.series[instrument], self._bar[instrument]
        if b < 0:
            return pd.DataFrame(columns=["open", "high", "low", "close"])
        lo_i = max(0, b - count + 1)
        pts = s.path[b, : self._eval_step + 1]  # only what has happened so far inside this bar
        o = np.r_[s.op[lo_i:b], pts[0]]
        h = np.r_[s.hi[lo_i:b], pts.max()]
        l = np.r_[s.lo[lo_i:b], pts.min()]
        c = np.r_[s.cl[lo_i:b], pts[-1]]
        return pd.DataFrame({"open": o, "high": h, "low": l, "close": c})

    async def positions(self) -> list[ScalpPosition]:
        out = []
        for p in self._pos.values():
            if self._mid[p.instrument] is None:
                continue
            out.append(ScalpPosition(p.ticket, p.instrument, p.side, p.volume, p.entry, p.sl, p.tp,
                                     self._floating(p), p.opened_at))
        return out

    async def send_market(self, instrument: str, side: str, volume: float, sl: float, tp: float) -> OrderOutcome:
        if self._bar[instrument] < 0 or self._mid[instrument] is None:
            return OrderOutcome(False, reason="no price (market closed)")
        bid, ask = self._bid_ask(instrument)
        slip = self.slip * self.series[instrument].pip
        fill = ask + slip if side == "BUY" else bid - slip
        self._ids += 1
        ticket = str(self._ids)
        self._pos[ticket] = _Pos(ticket, instrument, side, volume, fill, self._mid[instrument], sl, tp, self.now)
        return OrderOutcome(True, ticket, fill)

    async def close(self, ticket: str, volume: float | None = None, reason: str = "") -> OrderOutcome:
        p = self._pos.get(ticket)
        if p is None:
            return OrderOutcome(False, ticket, None, "Position not found (already closed?).")
        if self._mid[p.instrument] is None:
            return OrderOutcome(False, ticket, None, "no price (market closed)")
        bid, ask = self._bid_ask(p.instrument)
        slip = self.slip * self.series[p.instrument].pip
        fill = bid - slip if p.side == "BUY" else ask + slip
        self._settle(p, fill, _classify(reason), slip_amt=slip)
        return OrderOutcome(True, ticket, fill)

    async def modify(self, ticket: str, sl: float | None, tp: float | None) -> bool:
        p = self._pos.get(ticket)
        if p is None:
            return False
        if sl is not None:
            p.sl = sl
        if tp is not None:
            p.tp = tp
        return True

    async def loss_money(self, instrument: str, side: str, volume: float, entry: float, sl: float) -> float | None:
        return abs(self._money(instrument, abs(entry - sl), volume, entry))

    async def deals_since(self, since_ts: float) -> list[Deal]:
        return self._deals[bisect.bisect_left(self._deal_times, since_ts):]

    # ------------------------------------------------------------ runner helpers
    def note_open(self, event: str, data: dict[str, Any]) -> None:
        if event == "open" and data.get("ticket"):
            self.scores[str(data["ticket"])] = int(data.get("score", 0))

    def last_equity(self) -> float:
        return self._equity()


def _classify(reason: str) -> str:
    r = reason.lower()
    if "time stop" in r:
        return "time-stop"
    if "profit target" in r:
        return "profit-target"
    if "basket" in r:
        return "basket-take-profit"
    if "end of test" in r:
        return "end-of-test"
    if r:
        return "forced-exit"  # halted by a session rule, flatten, etc.
    return "manual-close"
