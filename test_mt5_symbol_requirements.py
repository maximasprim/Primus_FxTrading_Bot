import MetaTrader5 as mt5

LOGIN = 476462382
PASSWORD = "@0743493619Mm"
SERVER = "ExnessKE-MT5Trial9"

if not mt5.initialize(login=LOGIN, password=PASSWORD, server=SERVER):
    print("MT5 initialization failed:", mt5.last_error())
    raise SystemExit

symbol = "EURUSDm"

info = mt5.symbol_info(symbol)

if info is None:
    print("Symbol not found:", mt5.last_error())
    mt5.shutdown()
    raise SystemExit

tick = mt5.symbol_info_tick(symbol)

print("\n==============================")
print("MT5 SYMBOL RULES")
print("==============================")

print("Symbol:", info.name)
print("Digits:", info.digits)
print("Point:", info.point)

print("Bid:", tick.bid)
print("Ask:", tick.ask)

print("Trade stops level:", info.trade_stops_level)
print("Trade freeze level:", info.trade_freeze_level)

print("Volume minimum:", info.volume_min)
print("Volume maximum:", info.volume_max)
print("Volume step:", info.volume_step)

print("Filling mode:", info.filling_mode)
print("Trade mode:", info.trade_mode)

print("==============================")

mt5.shutdown()