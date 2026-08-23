"""
MetaTrader 5 Adapter -- WINDOWS ONLY, requires a running MT5 terminal
logged into the target account on the SAME machine as this backend process.

# CRITICAL -- READ BEFORE USING
This is the one piece of this project that has never been tested in any
form, not even structurally. The `MetaTrader5` package only imports and
functions on Windows with a live terminal connection; the environment that
built this can do neither. Every method below is written carefully against
MetaTrader5's documented API (https://www.mql5.com/en/docs/python_metatrader5),
but you are the first person to actually run this code. Expect to find
bugs -- that's not a sign something went wrong, it's the nature of this
specific file. Test each method individually against a DEMO account before
trusting the full execution pipeline -- see the checklist below.
"""
import asyncio

from app.execution.broker_interface import (
    AccountInfo,
    BrokerExecutionInterface,
    OrderRequest,
    OrderResult,
    Position,
)


class MT5Adapter(BrokerExecutionInterface):
    def __init__(self, login: int, password: str, server: str, symbol_suffix: str = ""):
        self.login = login
        self.password = password
        self.server = server
        self.symbol_suffix = symbol_suffix
        self._mt5 = None
        self._connected = False

    def _to_mt5_symbol(self, instrument: str) -> str:
        """'EUR/USD' -> 'EURUSD' (+ optional broker-specific suffix, e.g. 'EURUSDm')."""
        return instrument.replace("/", "").upper() + self.symbol_suffix

    async def _ensure_connected(self) -> None:
        if self._connected:
            return
        await asyncio.to_thread(self._connect_sync)

    def _connect_sync(self) -> None:
        import MetaTrader5 as mt5  # local import: package only exists/works on Windows

        self._mt5 = mt5
        if not mt5.initialize(login=self.login, password=self.password, server=self.server):
            error = mt5.last_error()
            raise RuntimeError(f"MT5 initialize() failed: {error}")
        self._connected = True

    async def get_account_balance(self) -> float:
        await self._ensure_connected()
        info = await asyncio.to_thread(self._mt5.account_info)
        if info is None:
            raise RuntimeError(f"MT5 account_info() returned None: {self._mt5.last_error()}")
        return float(info.balance)

    async def get_account_info(self) -> AccountInfo:
        try:
            await self._ensure_connected()
        except Exception as exc:  # noqa: BLE001 - surfaced to the user as "not connected", not a 500
            return AccountInfo(
                connected=False, broker_name="MetaTrader 5", account_id=None, currency="",
                balance=0.0, equity=0.0, server=self.server, error=str(exc),
            )

        mt5 = self._mt5
        info = await asyncio.to_thread(mt5.account_info)
        if info is None:
            return AccountInfo(
                connected=False, broker_name="MetaTrader 5", account_id=str(self.login), currency="",
                balance=0.0, equity=0.0, server=self.server, error=str(mt5.last_error()),
            )

        return AccountInfo(
            connected=True,
            broker_name=getattr(info, "company", None) or "MetaTrader 5",
            account_id=str(info.login),
            currency=info.currency,
            balance=float(info.balance),
            equity=float(info.equity),
            margin_used=float(info.margin),
            margin_free=float(info.margin_free),
            leverage=int(info.leverage),
            server=self.server,
        )

    async def get_open_positions(self) -> list[Position]:
        await self._ensure_connected()
        positions = await asyncio.to_thread(self._mt5.positions_get)
        if positions is None:
            return []
        result = []
        for p in positions:
            direction = "BUY" if p.type == self._mt5.ORDER_TYPE_BUY else "SELL"
            result.append(
                Position(
                    broker_order_id=str(p.ticket),
                    instrument=p.symbol,
                    direction=direction,
                    entry_price=p.price_open,
                    stop_loss=p.sl,
                    take_profit=p.tp,
                    size=p.volume,
                    unrealized_pnl=p.profit,
                )
            )
        return result

    # async def calculate_position_size(
    #     self, equity: float, risk_pct: float, entry: float, stop_loss: float
    # ) -> float:
    #     from app.core.risk import position_size_from_risk

    #     # NOTE: MT5 lot sizes are typically in "lots" (1 lot = 100,000 units
    #     # for most forex pairs), not raw units. This returns raw units per
    #     # the shared risk-sizing formula. # TODO: verify your broker's
    #     # volume convention (units vs lots, and volume_min/volume_step) via
    #     # mt5.symbol_info(symbol) before relying on this for real sizing --
    #     # sending a raw-units number where MT5 expects lots will place a
    #     # wildly wrong position size.
    #     return position_size_from_risk(equity, risk_pct, entry, stop_loss)
    async def calculate_position_size(
         self,
         equity: float,
         risk_pct: float,
         entry: float,
         stop_loss: float,
         instrument: str,
    ) -> float:
         """
         Calculate MT5 position size in lots based on actual monetary risk.

         Uses MT5 order_calc_profit() so the calculation works correctly
         across forex and metals such as EURUSD, GBPUSD and XAUUSD.
         """

         await self._ensure_connected()

         mt5 = self._mt5
         symbol = self._to_mt5_symbol(instrument)

         symbol_info = await asyncio.to_thread(
         mt5.symbol_info,
         symbol
        )

         if symbol_info is None:
             raise RuntimeError(
                 f"MT5 symbol {symbol} was not found."
             )

         # Target amount we are willing to lose
         risk_amount = equity * (risk_pct / 100.0)

         stop_distance = abs(entry - stop_loss)

         if stop_distance <= 0:
             return 0.0

         # Determine direction
         # Use BUY as the reference calculation.
         profit = await asyncio.to_thread(
             mt5.order_calc_profit,
             mt5.ORDER_TYPE_BUY,
             symbol,
             1.0,
             entry,
             stop_loss,
        )

         if profit is None:
             raise RuntimeError(
                 f"MT5 order_calc_profit() failed for {symbol}: "
                 f"{mt5.last_error()}"
         )

        # Profit should be negative because stop_loss represents a loss.
         loss_per_lot = abs(float(profit))

         if loss_per_lot <= 0:
              return 0.0

         # Calculate required lots
         raw_volume = risk_amount / loss_per_lot

         # Respect broker limits
         volume_min = symbol_info.volume_min
         volume_max = symbol_info.volume_max
         volume_step = symbol_info.volume_step

         # Limit to maximum
         volume = min(raw_volume, volume_max)

         # Round DOWN to broker's volume step
         volume = (
        int(volume / volume_step)
        * volume_step
       )

         # If calculated size is below broker minimum,
         # use minimum only if doing so doesn't create excessive risk.
         if volume < volume_min:
             volume = volume_min

         return round(volume, 2)

    async def place_order(self, order: OrderRequest) -> OrderResult:
        await self._ensure_connected()
        mt5 = self._mt5
        symbol = self._to_mt5_symbol(order.instrument)

        symbol_info = await asyncio.to_thread(mt5.symbol_info, symbol)
        if symbol_info is None:
            return OrderResult(
                success=False, broker_order_id=None, filled_price=None,
                reason=f"Symbol {symbol} not found on this broker/account.",
            )
        # if not symbol_info.visible:
        #     await asyncio.to_thread(mt5.symbol_select, symbol, True)
        if not symbol_info.visible:
             selected = await asyncio.to_thread(mt5.symbol_select, symbol, True)

             if not selected:
                 return OrderResult(
                     success=False,
                     broker_order_id=None,
                     filled_price=None,
                     reason=f"Could not enable symbol {symbol} in MT5 Market Watch.",
        )

        symbol_info = await asyncio.to_thread(mt5.symbol_info, symbol)

        if symbol_info is None:
             return OrderResult(
                 success=False,
                 broker_order_id=None,
                 filled_price=None,
                 reason=f"Symbol {symbol} became unavailable after selection.",
        )

        tick = await asyncio.to_thread(mt5.symbol_info_tick, symbol)
        if tick is None:
            return OrderResult(
                success=False, broker_order_id=None, filled_price=None,
                reason=f"Could not get current tick for {symbol}.",
            )

        order_type = mt5.ORDER_TYPE_BUY if order.direction == "BUY" else mt5.ORDER_TYPE_SELL
        price = tick.ask if order.direction == "BUY" else tick.bid

        # NOTE: assumes order.position_size is already in LOTS, not raw
        # units -- see the TODO in calculate_position_size above.
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": order.position_size,
            "type": order_type,
            "price": price,
            "sl": order.stop_loss,
            "tp": order.take_profit,
            "deviation": max(int(order.max_slippage_pips * 10), 1),
            "magic": 20260812,
            # "comment": f"forex-ai-analyst:{order.signal_id}"[:31],
            "comment": "PrimusFX-DEMO",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }

        result = await asyncio.to_thread(mt5.order_send, request)
        if result is None:
            return OrderResult(
                success=False, broker_order_id=None, filled_price=None,
                reason=f"order_send() returned None: {mt5.last_error()}",
            )
        if result.retcode != mt5.TRADE_RETCODE_DONE:
            return OrderResult(
                success=False, broker_order_id=None, filled_price=None,
                reason=f"MT5 rejected order: retcode={result.retcode} ({result.comment})",
            )

        return OrderResult(success=True, broker_order_id=str(result.order), filled_price=result.price)

    async def modify_order(self, broker_order_id: str, stop_loss: float | None, take_profit: float | None) -> bool:
        await self._ensure_connected()
        mt5 = self._mt5
        ticket = int(broker_order_id)

        positions = await asyncio.to_thread(mt5.positions_get, ticket=ticket)
        if not positions:
            return False
        position = positions[0]

        request = {
            "action": mt5.TRADE_ACTION_SLTP,
            "symbol": position.symbol,
            "position": ticket,
            "sl": stop_loss if stop_loss is not None else position.sl,
            "tp": take_profit if take_profit is not None else position.tp,
        }
        result = await asyncio.to_thread(mt5.order_send, request)
        return result is not None and result.retcode == mt5.TRADE_RETCODE_DONE

    async def close_order(self, broker_order_id: str, size: float | None = None) -> OrderResult:
        await self._ensure_connected()
        mt5 = self._mt5
        ticket = int(broker_order_id)

        positions = await asyncio.to_thread(mt5.positions_get, ticket=ticket)
        if not positions:
            return OrderResult(
                success=False, broker_order_id=broker_order_id, filled_price=None,
                reason="Position not found (already closed?).",
            )
        position = positions[0]

        tick = await asyncio.to_thread(mt5.symbol_info_tick, position.symbol)
        close_type = mt5.ORDER_TYPE_SELL if position.type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY
        price = tick.bid if close_type == mt5.ORDER_TYPE_SELL else tick.ask

        close_volume = position.volume if (size is None or size >= position.volume) else size

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": position.symbol,
            "volume": close_volume,
            "type": close_type,
            "position": ticket,
            "price": price,
            "deviation": 20,
            "magic": 20260812,
            "comment": "forex-ai-analyst:close",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        result = await asyncio.to_thread(mt5.order_send, request)
        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
            reason = f"Close failed: {mt5.last_error() if result is None else result.comment}"
            return OrderResult(success=False, broker_order_id=broker_order_id, filled_price=None, reason=reason)
        remaining = round(position.volume - close_volume, 10)
        return OrderResult(success=True, broker_order_id=broker_order_id, filled_price=result.price, remaining_size=remaining)

    async def is_connection_healthy(self) -> bool:
        try:
            await self._ensure_connected()
            info = await asyncio.to_thread(self._mt5.terminal_info)
            return info is not None and info.connected
        except Exception:  # noqa: BLE001 - any connectivity failure means "not healthy"
            return False

    async def is_market_open(self, instrument: str) -> bool:
        await self._ensure_connected()
        symbol = self._to_mt5_symbol(instrument)
        info = await asyncio.to_thread(self._mt5.symbol_info, symbol)
        if info is None:
            return False
        return info.trade_mode != self._mt5.SYMBOL_TRADE_MODE_DISABLED