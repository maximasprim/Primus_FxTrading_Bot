"""
Broker Adapter Architecture (execution-feature spec §11-12).

The AI/analysis layer must never touch a broker directly. The execution
service (execution_service.py) is the *only* component authorized to call
into an adapter, and it only does so after every validation stage in the
pipeline has passed.

Implement one subclass per real broker/provider. `PaperTradingAdapter`
(adapters/paper_adapter.py) is the safe default and satisfies every method
without touching real capital.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class OrderRequest:
    signal_id: str
    instrument: str
    direction: str  # "BUY" | "SELL"
    entry_price: float
    stop_loss: float
    take_profit: float
    position_size: float
    max_slippage_pips: float = 2.0


@dataclass
class OrderResult:
    success: bool
    broker_order_id: str | None
    filled_price: float | None
    reason: str | None = None
    # Populated by close_order when a `size` less than the full open size was
    # requested (partial close) — units still open on that ticket afterwards.
    # None (not 0.0) after a full close, since 0.0 is itself meaningful.
    remaining_size: float | None = None


@dataclass
class Position:
    broker_order_id: str
    instrument: str
    direction: str
    entry_price: float
    stop_loss: float
    take_profit: float
    size: float
    unrealized_pnl: float = 0.0


@dataclass
class AccountInfo:
    """What the "Account" tab needs to answer 'is the bot actually
    connected, and to what?' — deliberately broker-agnostic; MT5 fills in
    every field from account_info(), the paper adapter fills in a
    consistent simulated equivalent."""

    connected: bool
    broker_name: str
    account_id: str | None
    currency: str
    balance: float
    equity: float
    margin_used: float | None = None
    margin_free: float | None = None
    leverage: int | None = None
    server: str | None = None
    error: str | None = None


class BrokerExecutionInterface(ABC):
    """Every adapter (paper, MT5, a future broker API) must implement this."""

    @abstractmethod
    async def get_account_balance(self) -> float:
        raise NotImplementedError

    @abstractmethod
    async def get_account_info(self) -> AccountInfo:
        """Richer account snapshot than get_account_balance() -- used by
        the "Account" tab to actually prove the bot is connected to a real
        (or paper) account and show what's in it, rather than just a
        balance number that could be a stale placeholder."""
        raise NotImplementedError

    @abstractmethod
    async def get_open_positions(self) -> list[Position]:
        raise NotImplementedError

    @abstractmethod
    async def calculate_position_size(
        self, equity: float, risk_pct: float, entry: float, stop_loss: float, instrument: str
    ) -> float:
        raise NotImplementedError

    @abstractmethod
    async def place_order(self, order: OrderRequest) -> OrderResult:
        raise NotImplementedError

    @abstractmethod
    async def modify_order(self, broker_order_id: str, stop_loss: float | None, take_profit: float | None) -> bool:
        raise NotImplementedError

    @abstractmethod
    async def close_order(self, broker_order_id: str, size: float | None = None) -> OrderResult:
        """Closes a position. `size` in units — omit (or pass the full open
        size) to close the whole position; pass a smaller amount for a
        partial close, leaving the remainder open with the same stop/target."""
        raise NotImplementedError

    @abstractmethod
    async def is_connection_healthy(self) -> bool:
        raise NotImplementedError

    @abstractmethod
    async def is_market_open(self, instrument: str) -> bool:
        raise NotImplementedError
