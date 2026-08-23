import asyncio

from app.config import get_settings
from app.execution.adapters.mt5_adapter import MT5Adapter
from app.execution.broker_interface import OrderRequest


async def main():

    settings = get_settings()

    adapter = MT5Adapter(
        login=settings.mt5_login,
        password=settings.mt5_password,
        server=settings.mt5_server,
        symbol_suffix=settings.mt5_symbol_suffix,
    )

    print("Connecting to MT5...")

    balance = await adapter.get_account_balance()

    print(f"Account balance: {balance}")
    print()

    order = OrderRequest(
        signal_id="DEMO-MANUAL-001",
        instrument="EUR/USD",
        direction="BUY",
        entry_price=0,
        stop_loss=0,
        take_profit=0,
        position_size=0.01,
        max_slippage_pips=2,
    )

    print("Placing 0.01 lot EURUSDm DEMO BUY...")

    result = await adapter.place_order(order)

    print()
    print("========== RESULT ==========")
    print(f"Success:       {result.success}")
    print(f"Order ID:      {result.broker_order_id}")
    print(f"Filled price:  {result.filled_price}")
    print(f"Reason:        {result.reason}")


if __name__ == "__main__":
    asyncio.run(main())