"""Plain data types shared by the scalper's gateway, signals, guard and engine."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Tick:
    bid: float
    ask: float
    time: float  # epoch seconds, UTC

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2


@dataclass
class SymbolSpec:
    instrument: str          # "EUR/USD"
    symbol: str              # broker symbol, e.g. "EURUSDm"
    digits: int
    point: float
    pip_size: float          # spec pip: 0.0001 FX, 0.01 JPY, 0.1 XAU
    volume_min: float
    volume_max: float
    volume_step: float
    stops_level_points: int  # broker minimum SL/TP distance from price, in points
    trade_allowed: bool = True


@dataclass
class ScalpPosition:
    ticket: str
    instrument: str
    side: str                # "BUY" | "SELL"
    volume: float
    price_open: float
    sl: float                # 0.0 when unset
    tp: float
    profit: float            # money, includes swap/commission where the broker reports it
    opened_at: float         # epoch seconds, UTC


@dataclass
class Deal:
    """A closed trade leg used for realised P&L and loss-streak tracking."""
    ticket: str
    instrument: str
    profit: float
    time: float


@dataclass
class OrderOutcome:
    ok: bool
    ticket: str | None = None
    price: float | None = None
    reason: str | None = None


@dataclass
class AccountSnapshot:
    connected: bool
    login: str | None = None
    server: str | None = None
    currency: str = ""
    balance: float = 0.0
    equity: float = 0.0
    is_demo: bool = True
    trade_allowed: bool = False         # account allows trading
    algo_trading_enabled: bool = False  # the terminal's "Algo Trading" button is ON
    hedging: bool = True                # False on NETTING accounts: one position per symbol, so no stacking
    backend: str = "mt5"
    error: str | None = None


@dataclass
class SignalResult:
    side: str | None                    # "BUY" | "SELL" | None
    score: int                          # 0-100
    atr_pips: float = 0.0
    reasons: list[str] = field(default_factory=list)
