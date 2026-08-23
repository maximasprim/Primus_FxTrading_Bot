import asyncio

from app.config import get_settings
from app.execution.adapters.mt5_adapter import MT5Adapter
from app.execution.broker_interface import OrderRequest


async def main():
    settings = get_settings()

    print("Connecting to MT5...")

    adapter = MT5Adapter(
        login=settings.mt5_login,
        password=settings.mt5_password,
        server=settings.mt5_server,
        symbol_suffix=settings.mt5_symbol_suffix,
    )

    # 1. Connection
    balance = await adapter.get_account_balance()

    print()
    print("==============================")
    print("MT5 CONNECTION SUCCESSFUL")
    print("==============================")
    print(f"Demo balance: {balance}")
    print()

    # 2. Test EUR/USD
    instrument = "EUR/USD"

    print(f"Testing {instrument}...")

    market_open = await adapter.is_market_open(instrument)

    print(f"Market available: {market_open}")

    if not market_open:
        print("Market is not available. Stopping.")
        return

    # 3. Get MT5 price
    mt5 = adapter._mt5
    symbol = adapter._to_mt5_symbol(instrument)

    tick = await asyncio.to_thread(
        mt5.symbol_info_tick,
        symbol,
    )

    if tick is None:
        print(f"Could not get tick for {symbol}")
        return

    print(f"MT5 symbol: {symbol}")
    print(f"Bid: {tick.bid}")
    print(f"Ask: {tick.ask}")

    # 4. Build a DEMO BUY order
    entry = tick.ask

    # EURUSDm has 5 digits.
    # 20 pips = 0.00200
    # 40 pips = 0.00400

    stop_loss = round(entry - 0.00200, 5)
    take_profit = round(entry + 0.00400, 5)

    print()
    print("==============================")
    print("TEST TRADE")
    print("==============================")
    print(f"Direction: BUY")
    print(f"Entry: {entry}")
    print(f"Stop Loss: {stop_loss}")
    print(f"Take Profit: {take_profit}")

    # 5. Minimum demo volume
    volume = 0.01

    print(f"Volume: {volume} lots")

    # 6. Order request
    order = OrderRequest(
        signal_id="MANUAL-MT5-DEMO-TEST",
        instrument=instrument,
        direction="BUY",
        entry_price=entry,
        stop_loss=stop_loss,
        take_profit=take_profit,
        position_size=volume,
        max_slippage_pips=2.0,
    )

    print()
    print("Sending demo order...")

    # 7. Send
    result = await adapter.place_order(order)

    print()
    print("==============================")
    print("ORDER RESULT")
    print("==============================")
    print(f"Success: {result.success}")
    print(f"Order ID: {result.broker_order_id}")
    print(f"Filled price: {result.filled_price}")
    print(f"Reason: {result.reason}")

    if not result.success:
        print()
        print("ORDER WAS NOT EXECUTED.")
        return

    print()
    print("SUCCESS: DEMO ORDER WAS PLACED.")
    print("Check MT5 for the open EURUSDm position.")


if __name__ == "__main__":
    asyncio.run(main())