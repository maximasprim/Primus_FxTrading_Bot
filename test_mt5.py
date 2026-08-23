import asyncio

from app.config import get_settings
from app.execution.adapters.mt5_adapter import MT5Adapter


async def main():
    settings = get_settings()

    print("Connecting to MT5...")
    print(f"MT5 Login: {settings.mt5_login}")
    print(f"MT5 Server: {settings.mt5_server}")
    print(f"Symbol suffix: '{settings.mt5_symbol_suffix}'")

    adapter = MT5Adapter(
        login=settings.mt5_login,
        password=settings.mt5_password,
        server=settings.mt5_server,
        symbol_suffix=settings.mt5_symbol_suffix,
    )

    try:
        balance = await adapter.get_account_balance()

        print("\n================================")
        print("MT5 CONNECTION SUCCESSFUL")
        print("================================")
        print(f"Demo account balance: {balance}")

        positions = await adapter.get_open_positions()
        print(f"Open positions: {positions}")

        eurusd_open = await adapter.is_market_open("EUR/USD")
        print(f"EUR/USD available for trading: {eurusd_open}")

    except Exception as e:
        print("\n================================")
        print("MT5 CONNECTION FAILED")
        print("================================")
        print(f"Error: {e}")


if __name__ == "__main__":
    asyncio.run(main())