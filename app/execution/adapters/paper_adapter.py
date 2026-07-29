"""
Paper Trading Adapter — the default, safe implementation of
BrokerExecutionInterface. Simulates fills using the market data engine's
current price; never touches a real account.

This is what `BROKER_ADAPTER=paper` (the default in .env.example) resolves to.
"""
import uuid
from datetime import datetime, timezone

from app.core.pip_utils import get_pip_size
from app.core.risk import position_size_from_risk
from app.data.market_data_engine import MarketDataEngine
from app.execution.broker_interface import (
    BrokerExecutionInterface,
    OrderRequest,
    OrderResult,
    Position,
)


class PaperTradingAdapter(BrokerExecutionInterface):
    def __init__(self, starting_balance: float = 10_000.0):
        self._balance = starting_balance
        self._positions: dict[str, Position] = {}
        self._market_data = MarketDataEngine()

    async def get_account_balance(self) -> float:
        return self._balance

    async def get_open_positions(self) -> list[Position]:
        return list(self._positions.values())

    async def calculate_position_size(
        self, equity: float, risk_pct: float, entry: float, stop_loss: float
    ) -> float:
        return position_size_from_risk(equity, risk_pct, entry, stop_loss)

    async def place_order(self, order: OrderRequest) -> OrderResult:
        price_data = await self._market_data.get_current_price(order.instrument)
        fill_price = price_data["ask"] if order.direction == "BUY" else price_data["bid"]

        slippage = abs(fill_price - order.entry_price)
        pip_size = get_pip_size(order.instrument)
        slippage_pips = slippage / pip_size
        if slippage_pips > order.max_slippage_pips:
            return OrderResult(
                success=False,
                broker_order_id=None,
                filled_price=None,
                reason=f"Slippage {slippage_pips:.1f} pips exceeds max allowed {order.max_slippage_pips} pips.",
            )

        order_id = f"PAPER-{uuid.uuid4().hex[:10].upper()}"
        self._positions[order_id] = Position(
            broker_order_id=order_id,
            instrument=order.instrument,
            direction=order.direction,
            entry_price=fill_price,
            stop_loss=order.stop_loss,
            take_profit=order.take_profit,
            size=order.position_size,
        )
        return OrderResult(success=True, broker_order_id=order_id, filled_price=fill_price)

    async def modify_order(self, broker_order_id: str, stop_loss: float | None, take_profit: float | None) -> bool:
        position = self._positions.get(broker_order_id)
        if position is None:
            return False
        if stop_loss is not None:
            position.stop_loss = stop_loss
        if take_profit is not None:
            position.take_profit = take_profit
        return True

    async def close_order(self, broker_order_id: str) -> OrderResult:
        position = self._positions.pop(broker_order_id, None)
        if position is None:
            return OrderResult(success=False, broker_order_id=broker_order_id, filled_price=None, reason="Position not found.")

        price_data = await self._market_data.get_current_price(position.instrument)
        close_price = price_data["bid"] if position.direction == "BUY" else price_data["ask"]
        pnl = (
            (close_price - position.entry_price) if position.direction == "BUY"
            else (position.entry_price - close_price)
        ) * position.size
        self._balance += pnl
        return OrderResult(success=True, broker_order_id=broker_order_id, filled_price=close_price)

    async def is_connection_healthy(self) -> bool:
        return True  # paper adapter has no real connection to lose

    async def is_market_open(self, instrument: str) -> bool:
        now = datetime.now(timezone.utc)
        return now.weekday() < 5  # simplistic: closed on weekends only
