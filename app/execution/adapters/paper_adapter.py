"""
Paper Trading Adapter - the default, safe implementation of
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
    AccountInfo,
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

    async def get_account_info(self) -> AccountInfo:
        # Paper trading is always "connected" and has no margin system --
        # equity = balance + whatever every open position is worth right
        # now, marked to the live price so it's not just a stale number.
        unrealized = 0.0
        for position in self._positions.values():
            try:
                price_data = await self._market_data.get_current_price(position.instrument)
                mark = price_data["bid"] if position.direction == "BUY" else price_data["ask"]
                unrealized += (
                    (mark - position.entry_price) if position.direction == "BUY" else (position.entry_price - mark)
                ) * position.size
            except Exception:  # noqa: BLE001 - one bad quote shouldn't break the whole snapshot
                continue

        return AccountInfo(
            connected=True,
            broker_name="Paper Trading (Simulated)",
            account_id="paper-default",
            currency="USD",
            balance=self._balance,
            equity=self._balance + unrealized,
            server="simulated",
        )

    async def get_open_positions(self) -> list[Position]:
        return list(self._positions.values())

    async def calculate_position_size(
        self, equity: float, risk_pct: float, entry: float, stop_loss: float, instrument: str
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

    async def close_order(self, broker_order_id: str, size: float | None = None) -> OrderResult:
        position = self._positions.get(broker_order_id)
        if position is None:
            return OrderResult(success=False, broker_order_id=broker_order_id, filled_price=None, reason="Position not found.")

        price_data = await self._market_data.get_current_price(position.instrument)
        close_price = price_data["bid"] if position.direction == "BUY" else price_data["ask"]

        close_size = position.size if (size is None or size >= position.size) else size
        pnl = (
            (close_price - position.entry_price) if position.direction == "BUY"
            else (position.entry_price - close_price)
        ) * close_size
        self._balance += pnl

        remaining = round(position.size - close_size, 10)
        if remaining <= 0:
            self._positions.pop(broker_order_id, None)
            remaining = 0.0
        else:
            position.size = remaining

        return OrderResult(success=True, broker_order_id=broker_order_id, filled_price=close_price, remaining_size=remaining)

    async def is_connection_healthy(self) -> bool:
        return True  # paper adapter has no real connection to lose

    async def is_market_open(self, instrument: str) -> bool:
        now = datetime.now(timezone.utc)
        return now.weekday() < 5  # simplistic: closed on weekends only
