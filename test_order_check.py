import sys, MetaTrader5 as mt5
print(sys.version, mt5.__version__)
print("initialize:", mt5.initialize(), mt5.last_error())
tick = mt5.symbol_info_tick("EURUSDm")
req = {"action": mt5.TRADE_ACTION_DEAL, "symbol": "EURUSDm", "volume": 0.01,
       "type": mt5.ORDER_TYPE_SELL, "price": tick.bid, "deviation": 30,
       "magic": 20260901, "comment": "test", "type_time": mt5.ORDER_TIME_GTC,
       "type_filling": mt5.ORDER_FILLING_IOC}
print("positional:", mt5.order_check(req), mt5.last_error())
print("keyword:", mt5.order_check(request=req), mt5.last_error())
mt5.shutdown()
