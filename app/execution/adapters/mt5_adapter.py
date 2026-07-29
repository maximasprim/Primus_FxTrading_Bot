"""
MetaTrader 5 Adapter — STUB.

# TODO: implement against the `MetaTrader5` Python package (Windows-only,
# requires a running MT5 terminal) or an MT5 bridge/REST gateway if running
# on Linux. Every method below currently raises NotImplementedError so this
# adapter can never be silently selected and used by accident — it must be
# deliberately finished and tested (ideally against a demo account first)
# before `BROKER_ADAPTER=mt5` is used anywhere near real capital.
"""
from app.execution.broker_interface import (
    BrokerExecutionInterface,
    OrderRequest,
    OrderResult,
    Position,
)


class MT5Adapter(BrokerExecutionInterface):
    def __init__(self, login: str, password: str, server: str):
        self.login = login
        self.password = password
        self.server = server
        # TODO: establish MT5 terminal connection here.

    async def get_account_balance(self) -> float:
        raise NotImplementedError("MT5Adapter is a stub — implement before use.")

    async def get_open_positions(self) -> list[Position]:
        raise NotImplementedError("MT5Adapter is a stub — implement before use.")

    async def calculate_position_size(
        self, equity: float, risk_pct: float, entry: float, stop_loss: float
    ) -> float:
        raise NotImplementedError("MT5Adapter is a stub — implement before use.")

    async def place_order(self, order: OrderRequest) -> OrderResult:
        raise NotImplementedError("MT5Adapter is a stub — implement before use.")

    async def modify_order(self, broker_order_id: str, stop_loss: float | None, take_profit: float | None) -> bool:
        raise NotImplementedError("MT5Adapter is a stub — implement before use.")

    async def close_order(self, broker_order_id: str) -> OrderResult:
        raise NotImplementedError("MT5Adapter is a stub — implement before use.")

    async def is_connection_healthy(self) -> bool:
        return False  # honest default: an unimplemented adapter is never "healthy"

    async def is_market_open(self, instrument: str) -> bool:
        raise NotImplementedError("MT5Adapter is a stub — implement before use.")
