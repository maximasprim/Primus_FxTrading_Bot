"""
Simulated market + broker. Lets you try the scalper, the dashboard and every
limit without touching MT5 (and on non-Windows machines). Prices follow a random
walk with drifting regimes; SL/TP fill against bid/ask. It proves the plumbing
works -- it says NOTHING about real-market profitability.
"""
from __future__ import annotations

import itertools
import time
from collections import deque
from typing import Callable

import numpy as np
import pandas as pd

from app.core.pip_utils import get_pip_size
from app.scalper.gateway import ScalperGateway
from app.scalper.models import AccountSnapshot, Deal, OrderOutcome, ScalpPosition, SymbolSpec, Tick

_START_PRICE = {"EUR/USD": 1.0850, "GBP/USD": 1.2700, "USD/JPY": 150.00, "XAU/USD": 2350.0, "AUD/USD": 0.6600}
_SPREAD_PIPS = {"EUR/USD": 0.8, "GBP/USD": 1.2, "USD/JPY": 1.0, "XAU/USD": 2.5, "AUD/USD": 1.2}
_SIGMA_PIPS_PER_SEC = {"XAU/USD": 0.9}  # default 0.5
_BAR = 60


def _pip_value_per_lot(instrument: str) -> float:
    return 7.0 if "JPY" in instrument else 10.0


class _Market:
    def __init__(self, instrument: str, rng: np.random.Generator, now: float):
        self.instrument = instrument
        self.pip = get_pip_size(instrument)
        self.spread = _SPREAD_PIPS.get(instrument, 1.5) * self.pip
        self.sigma = _SIGMA_PIPS_PER_SEC.get(instrument, 0.5) * self.pip
        self.rng = rng
        self.mid = _START_PRICE.get(instrument, 1.0)
        self.drift = 0.0
        self.regime_left = 0
        self.t = now
        self.bars: deque[list[float]] = deque(maxlen=400)  # [bar_start, o, h, l, c]
        self._seed_history(now)

    def _seed_history(self, now: float) -> None:
        start = (int(now) // _BAR) * _BAR - 200 * _BAR
        for i in range(200):
            o = self.mid
            path = o + np.cumsum(self.rng.normal(0, self.sigma, _BAR))
            self.bars.append([start + i * _BAR, o, float(path.max()), float(path.min()), float(path[-1])])
            self.mid = float(path[-1])

    def step_to(self, now: float) -> None:
        steps = min(int(now - self.t), 900)
        for _ in range(max(steps, 0)):
            if self.regime_left <= 0:
                self.drift = float(self.rng.normal(0, 0.12)) * self.pip
                self.regime_left = int(self.rng.integers(120, 900))
            self.regime_left -= 1
            self.t += 1
            self.mid += float(self.rng.normal(self.drift, self.sigma))
            bar_start = (int(self.t) // _BAR) * _BAR
            if bar_start != self.bars[-1][0]:
                self.bars.append([bar_start, self.mid, self.mid, self.mid, self.mid])
            else:
                bar = self.bars[-1]
                bar[2], bar[3], bar[4] = max(bar[2], self.mid), min(bar[3], self.mid), self.mid
        if steps >= 900:
            self.t = now

    @property
    def bid(self) -> float:
        return self.mid - self.spread / 2

    @property
    def ask(self) -> float:
        return self.mid + self.spread / 2


class SimGateway(ScalperGateway):
    name = "simulation"

    def __init__(self, balance: float = 10_000.0, seed: int | None = None, clock: Callable[[], float] = time.time):
        self._rng = np.random.default_rng(seed)
        self._clock = clock
        self._markets: dict[str, _Market] = {}
        self._balance = balance
        self._positions: dict[str, ScalpPosition] = {}
        self._deals: list[Deal] = []
        self._ids = itertools.count(1000)

    # -- internals ----------------------------------------------------
    def _market(self, instrument: str) -> _Market:
        m = self._markets.get(instrument)
        if m is None:
            m = self._markets[instrument] = _Market(instrument, self._rng, self._clock())
        m.step_to(self._clock())
        return m

    def _mark(self, p: ScalpPosition, m: _Market) -> float:
        px = m.bid if p.side == "BUY" else m.ask
        diff = (px - p.price_open) if p.side == "BUY" else (p.price_open - px)
        return diff / m.pip * _pip_value_per_lot(p.instrument) * p.volume

    def _settle(self, p: ScalpPosition, px: float, m: _Market) -> float:
        diff = (px - p.price_open) if p.side == "BUY" else (p.price_open - px)
        profit = diff / m.pip * _pip_value_per_lot(p.instrument) * p.volume
        self._balance += profit
        self._deals.append(Deal(p.ticket, p.instrument, profit, self._clock()))
        self._positions.pop(p.ticket, None)
        return profit

    def _process_stops(self) -> None:
        for p in list(self._positions.values()):
            m = self._market(p.instrument)
            if p.side == "BUY":
                if p.sl and m.bid <= p.sl:
                    self._settle(p, p.sl, m)
                elif p.tp and m.bid >= p.tp:
                    self._settle(p, p.tp, m)
            else:
                if p.sl and m.ask >= p.sl:
                    self._settle(p, p.sl, m)
                elif p.tp and m.ask <= p.tp:
                    self._settle(p, p.tp, m)

    # -- gateway API --------------------------------------------------
    async def account(self) -> AccountSnapshot:
        self._process_stops()
        floating = sum(self._mark(p, self._market(p.instrument)) for p in self._positions.values())
        return AccountSnapshot(
            connected=True, login="SIM-0001", server="Simulator", currency="USD", balance=self._balance,
            equity=self._balance + floating, is_demo=True, trade_allowed=True, algo_trading_enabled=True,
            hedging=True, backend="simulation",
        )

    async def spec(self, instrument: str) -> SymbolSpec:
        pip = get_pip_size(instrument)
        digits = 3 if "JPY" in instrument else 2 if "XAU" in instrument else 5
        return SymbolSpec(instrument, instrument.replace("/", ""), digits, 10 ** -digits, pip, 0.01, 50.0, 0.01, 0)

    async def tick(self, instrument: str) -> Tick | None:
        m = self._market(instrument)
        return Tick(m.bid, m.ask, self._clock())

    async def candles(self, instrument: str, count: int = 120) -> pd.DataFrame:
        m = self._market(instrument)
        rows = list(m.bars)[-count:]
        df = pd.DataFrame(rows, columns=["t", "open", "high", "low", "close"])
        df.index = pd.to_datetime(df.pop("t"), unit="s", utc=True)
        return df

    async def positions(self) -> list[ScalpPosition]:
        self._process_stops()
        out = []
        for p in self._positions.values():
            m = self._market(p.instrument)
            out.append(ScalpPosition(**{**p.__dict__, "profit": self._mark(p, m)}))
        return out

    async def send_market(self, instrument: str, side: str, volume: float, sl: float, tp: float) -> OrderOutcome:
        m = self._market(instrument)
        price = m.ask if side == "BUY" else m.bid
        ticket = str(next(self._ids))
        self._positions[ticket] = ScalpPosition(ticket, instrument, side, volume, price, sl, tp, 0.0, self._clock())
        return OrderOutcome(True, ticket, price)

    async def close(self, ticket: str, volume: float | None = None, reason: str = "") -> OrderOutcome:
        p = self._positions.get(ticket)
        if p is None:
            return OrderOutcome(False, ticket, None, "Position not found (already closed?).")
        m = self._market(p.instrument)
        px = m.bid if p.side == "BUY" else m.ask
        self._settle(p, px, m)
        return OrderOutcome(True, ticket, px)

    async def modify(self, ticket: str, sl: float | None, tp: float | None) -> bool:
        p = self._positions.get(ticket)
        if p is None:
            return False
        if sl is not None:
            p.sl = sl
        if tp is not None:
            p.tp = tp
        return True

    async def loss_money(self, instrument: str, side: str, volume: float, entry: float, sl: float) -> float | None:
        return abs(entry - sl) / get_pip_size(instrument) * _pip_value_per_lot(instrument) * volume

    async def pip_value(self, instrument: str) -> float | None:
        return _pip_value_per_lot(instrument)

    async def check_order(self, instrument: str) -> dict | None:
        return {"ok": True, "detail": "simulated order accepted"}

    async def deals_since(self, since_ts: float) -> list[Deal]:
        self._process_stops()
        return [d for d in self._deals if d.time >= since_ts]
