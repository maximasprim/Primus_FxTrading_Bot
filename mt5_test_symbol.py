import MetaTrader5 as mt5

from app.config import get_settings


settings = get_settings()

print("Connecting to MT5...")

if not mt5.initialize(
    login=settings.mt5_login,
    password=settings.mt5_password,
    server=settings.mt5_server,
):
    print("MT5 connection failed:")
    print(mt5.last_error())
    raise SystemExit

print("Connected successfully.")

symbols = ["EURUSD", "GBPUSD", "XAUUSD"] + [f"{symbol}{settings.mt5_symbol_suffix}" for symbol in ["EURUSD", "GBPUSD", "XAUUSD"]]

for symbol in symbols:
    print("\n==============================")
    print(f"SYMBOL: {symbol}")
    print("==============================")

    info = mt5.symbol_info(symbol)

    if info is None:
        print("NOT FOUND")
        print("MT5 error:", mt5.last_error())
        continue

    print("Name:", info.name)
    print("Visible:", info.visible)
    print("Trade mode:", info.trade_mode)
    print("Volume minimum:", info.volume_min)
    print("Volume maximum:", info.volume_max)
    print("Volume step:", info.volume_step)
    print("Point:", info.point)
    print("Digits:", info.digits)

    tick = mt5.symbol_info_tick(symbol)

    if tick:
        print("Bid:", tick.bid)
        print("Ask:", tick.ask)
    else:
        print("No tick data")

mt5.shutdown()