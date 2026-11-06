"""
Real MT5 gateway for the scalper (desktop terminal, Windows).

Handled here so users don't hit these walls:
  * Order filling mode is read from the symbol (brokers differ: FOK/IOC/RETURN).
  * Requotes / price-changed rejections get one fresh-price retry.
  * SL/TP below the broker's minimum stop distance are rejected up front by the
    engine (see SymbolSpec.stops_level_points).
  * Netting accounts (one position per symbol) are detected and reported so the
    engine can avoid "stacking" trades that the broker would merge.
  * The terminal's "Algo Trading" button and account trade permission are
    reported in `account()`; the engine refuses to start if they're off.
  * Only positions carrying SCALPER_MAGIC are ever listed/modified/closed.
  * Broker server time is converted to UTC.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta
from typing import Any

import pandas as pd

from app.core.pip_utils import get_pip_size
from app.mt5.client import MT5Client, MT5Unavailable
from app.scalper.gateway import SCALPER_COMMENT, SCALPER_MAGIC, ScalperGateway
from app.scalper.models import AccountSnapshot, Deal, OrderOutcome, ScalpPosition, SymbolSpec, Tick

_RETRY_RETCODES = {10004, 10020, 10021}  # requote, prices changed, no prices


class MT5Gateway(ScalperGateway):
    name = "mt5"

    def __init__(self, client: MT5Client, symbol_suffix: str = "", deviation_points: int = 30):
        self.c = client
        self.suffix = symbol_suffix
        self.deviation = deviation_points
        self._specs: dict[str, SymbolSpec] = {}
        self._filling: dict[str, int] = {}
        self._order_style: str | None = None   # "dict" or "kwargs": whichever form this MT5 build accepts

    def _symbol(self, instrument: str) -> str:
        return instrument.replace("/", "") + self.suffix

    async def _offset(self, symbol: str) -> int:
        return await self.c.server_offset_seconds(symbol)

    # -- account --------------------------------------------------------
    async def account(self) -> AccountSnapshot:
        try:
            info = await self.c.call("account_info")
            term = await self.c.call("terminal_info")
        except MT5Unavailable as exc:
            return AccountSnapshot(connected=False, error=str(exc), backend="mt5")
        if info is None or term is None:
            self.c.mark_disconnected()
            return AccountSnapshot(connected=False, error=f"MT5 returned no account info: {await self.c.last_error()}", backend="mt5")
        c = self.c.const
        return AccountSnapshot(
            connected=bool(term.connected),
            login=str(info.login),
            server=info.server,
            currency=info.currency,
            balance=float(info.balance),
            equity=float(info.equity),
            is_demo=info.trade_mode == c("ACCOUNT_TRADE_MODE_DEMO"),
            trade_allowed=bool(info.trade_allowed) and bool(info.trade_expert),
            algo_trading_enabled=bool(term.trade_allowed),
            hedging=info.margin_mode == c("ACCOUNT_MARGIN_MODE_RETAIL_HEDGING"),
            backend="mt5",
            error=None if term.connected else "MT5 terminal isn't connected to the broker.",
        )

    # -- market data ------------------------------------------------------
    async def spec(self, instrument: str) -> SymbolSpec:
        cached = self._specs.get(instrument)
        if cached:
            return cached
        symbol = self._symbol(instrument)
        info = await self.c.call("symbol_info", symbol)
        if info is None:
            raise RuntimeError(f"Symbol {symbol} not found on this account (check MT5_SYMBOL_SUFFIX).")
        if not info.visible and not await self.c.call("symbol_select", symbol, True):
            raise RuntimeError(f"Could not add {symbol} to Market Watch.")
        info = await self.c.call("symbol_info", symbol)
        self._filling[symbol] = int(info.filling_mode)
        spec = SymbolSpec(
            instrument=instrument, symbol=symbol, digits=int(info.digits), point=float(info.point),
            pip_size=get_pip_size(instrument), volume_min=float(info.volume_min),
            volume_max=float(info.volume_max), volume_step=float(info.volume_step),
            stops_level_points=int(info.trade_stops_level),
            trade_allowed=info.trade_mode == self.c.const("SYMBOL_TRADE_MODE_FULL"),
        )
        self._specs[instrument] = spec
        return spec

    async def tick(self, instrument: str) -> Tick | None:
        symbol = self._symbol(instrument)
        t = await self.c.call("symbol_info_tick", symbol)
        if t is None or not t.bid or not t.ask:
            return None
        return Tick(float(t.bid), float(t.ask), float(t.time) - await self._offset(symbol))

    async def candles(self, instrument: str, count: int = 120) -> pd.DataFrame:
        symbol = self._symbol(instrument)
        rates = await self.c.call("copy_rates_from_pos", symbol, self.c.const("TIMEFRAME_M1"), 0, count)
        if rates is None or len(rates) == 0:
            return pd.DataFrame(columns=["open", "high", "low", "close"])
        df = pd.DataFrame(rates)
        offset = await self._offset(symbol)
        df.index = pd.to_datetime(df["time"] - offset, unit="s", utc=True)
        return df[["open", "high", "low", "close"]]

    # -- positions ----------------------------------------------------------
    async def positions(self) -> list[ScalpPosition]:
        raw = await self.c.call("positions_get")
        if not raw:
            return []
        buy = self.c.const("POSITION_TYPE_BUY")
        out: list[ScalpPosition] = []
        for p in raw:
            if p.magic != SCALPER_MAGIC:
                continue
            instrument = self._instrument_for(p.symbol)
            offset = await self._offset(p.symbol)
            out.append(ScalpPosition(
                ticket=str(p.ticket), instrument=instrument, side="BUY" if p.type == buy else "SELL",
                volume=float(p.volume), price_open=float(p.price_open), sl=float(p.sl), tp=float(p.tp),
                profit=float(p.profit) + float(getattr(p, "swap", 0.0)), opened_at=float(p.time) - offset,
            ))
        return out

    def _instrument_for(self, symbol: str) -> str:
        base = symbol[: len(symbol) - len(self.suffix)] if self.suffix and symbol.endswith(self.suffix) else symbol
        return f"{base[:3]}/{base[3:]}" if len(base) == 6 else base

    # -- orders ---------------------------------------------------------------
    async def _order_call(self, fn: str, request: dict) -> Any:
        """Call order_send / order_check.

        The MetaTrader5 package documents `fn(request)`, but some builds only accept keyword arguments and answer the
        dict with (-2, 'Unnamed arguments not allowed') -- every order then fails. So: try the documented dict form,
        and ONLY on that specific -2 argument error try `fn(**request)`; remember whichever works. A None result means
        MT5 rejected the call before sending anything, so retrying in the other form can't double-trade.
        """
        styles = [self._order_style] if self._order_style else ["dict", "kwargs"]
        for style in styles:
            try:
                result = await (self.c.call(fn, request) if style == "dict" else self.c.call(fn, **request))
            except TypeError:
                continue
            if result is not None:
                self._order_style = style
                return result
            err = await self.c.last_error()
            if not (isinstance(err, (tuple, list)) and err and err[0] == -2):
                break  # a different failure: the other calling form won't fix it
        return None

    async def _order_error(self, fn: str) -> str:
        err = await self.c.last_error()
        hint = ""
        if isinstance(err, (tuple, list)) and err and err[0] == -2:
            hint = (" This is MT5 refusing the order's format, not your broker. Update the MetaTrader5 package "
                    "(`pip install -U MetaTrader5`) and run the pre-flight checks.")
        return f"{fn} returned None: {err}.{hint}"

    async def pip_value(self, instrument: str) -> float | None:
        """What the BROKER says one pip on 1.0 lot is worth in account currency (works for standard, cent, JPY, gold...)."""
        spec = await self.spec(instrument)
        tick = await self.c.call("symbol_info_tick", spec.symbol)
        if tick is None:
            return None
        price = float(tick.ask)
        profit = await self.c.call("order_calc_profit", self.c.const("ORDER_TYPE_BUY"), spec.symbol, 1.0, price, price + spec.pip_size)
        return None if profit is None else abs(float(profit))

    async def check_order(self, instrument: str) -> dict | None:
        """order_check: MT5/broker validate a minimum-size order and NOTHING is traded."""
        spec = await self.spec(instrument)
        tick = await self.c.call("symbol_info_tick", spec.symbol)
        if tick is None:
            return {"ok": False, "detail": f"no price for {spec.symbol}"}
        c = self.c.const
        price = float(tick.ask)
        gap = max(50 * spec.pip_size, (spec.stops_level_points + 20) * spec.point)
        request = {
            "action": c("TRADE_ACTION_DEAL"), "symbol": spec.symbol, "volume": float(spec.volume_min),
            "type": c("ORDER_TYPE_BUY"), "price": price, "sl": round(price - gap, spec.digits), "tp": round(price + gap, spec.digits),
            "deviation": self.deviation, "magic": SCALPER_MAGIC, "comment": SCALPER_COMMENT + ":check",
            "type_time": c("ORDER_TIME_GTC"), "type_filling": self._fill_type(spec.symbol),
        }
        result = await self._order_call("order_check", request)
        if result is None:
            return {"ok": False, "detail": await self._order_error("order_check")}
        ok = result.retcode in (0, c("TRADE_RETCODE_DONE"))
        return {"ok": ok, "market_closed": result.retcode == 10018,
                "detail": f"{'accepted' if ok else 'rejected'}: retcode {result.retcode} ({result.comment}); call style: {self._order_style}"}

    def _fill_type(self, symbol: str) -> int:
        mode = self._filling.get(symbol, 0)
        if mode & 2:
            return self.c.const("ORDER_FILLING_IOC")
        if mode & 1:
            return self.c.const("ORDER_FILLING_FOK")
        return self.c.const("ORDER_FILLING_RETURN")

    async def send_market(self, instrument: str, side: str, volume: float, sl: float, tp: float) -> OrderOutcome:
        spec = await self.spec(instrument)
        if not spec.trade_allowed:
            return OrderOutcome(False, reason=f"Trading is disabled for {spec.symbol} right now.")
        c = self.c.const
        last_reason = "unknown"
        for attempt in range(2):
            tick = await self.c.call("symbol_info_tick", spec.symbol)
            if tick is None:
                return OrderOutcome(False, reason=f"No price for {spec.symbol}.")
            price = float(tick.ask if side == "BUY" else tick.bid)
            request = {
                "action": c("TRADE_ACTION_DEAL"), "symbol": spec.symbol, "volume": float(volume),
                "type": c("ORDER_TYPE_BUY") if side == "BUY" else c("ORDER_TYPE_SELL"),
                "price": price, "sl": float(sl), "tp": float(tp), "deviation": self.deviation,
                "magic": SCALPER_MAGIC, "comment": SCALPER_COMMENT,
                "type_time": c("ORDER_TIME_GTC"), "type_filling": self._fill_type(spec.symbol),
            }
            result = await self._order_call("order_send", request)
            if result is None:
                return OrderOutcome(False, reason=await self._order_error("order_send"))
            if result.retcode == c("TRADE_RETCODE_DONE"):
                return OrderOutcome(True, str(result.order), float(result.price) or price)
            last_reason = f"MT5 rejected order: retcode={result.retcode} ({result.comment})"
            if result.retcode not in _RETRY_RETCODES:
                break
        return OrderOutcome(False, reason=last_reason)

    async def close(self, ticket: str, volume: float | None = None, reason: str = "") -> OrderOutcome:
        c = self.c.const
        found = await self.c.call("positions_get", ticket=int(ticket))
        if not found:
            return OrderOutcome(False, ticket, None, "Position not found (already closed?).")
        p = found[0]
        if p.magic != SCALPER_MAGIC:
            return OrderOutcome(False, ticket, None, "Refusing to close a position the scalper didn't open.")
        spec = await self.spec(self._instrument_for(p.symbol))
        for _ in range(2):
            tick = await self.c.call("symbol_info_tick", p.symbol)
            if tick is None:
                return OrderOutcome(False, ticket, None, f"No price for {p.symbol}.")
            is_buy = p.type == c("POSITION_TYPE_BUY")
            request = {
                "action": c("TRADE_ACTION_DEAL"), "symbol": p.symbol,
                "volume": float(volume if volume and volume < p.volume else p.volume),
                "type": c("ORDER_TYPE_SELL") if is_buy else c("ORDER_TYPE_BUY"),
                "position": int(ticket), "price": float(tick.bid if is_buy else tick.ask),
                "deviation": self.deviation, "magic": SCALPER_MAGIC, "comment": SCALPER_COMMENT + ":close",
                "type_time": c("ORDER_TIME_GTC"), "type_filling": self._fill_type(spec.symbol),
            }
            result = await self._order_call("order_send", request)
            if result is not None and result.retcode == c("TRADE_RETCODE_DONE"):
                return OrderOutcome(True, ticket, float(result.price))
            if result is None or result.retcode not in _RETRY_RETCODES:
                reason = await self._order_error("order_send") if result is None else f"{result.retcode} ({result.comment})"
                return OrderOutcome(False, ticket, None, f"Close failed: {reason}")
        return OrderOutcome(False, ticket, None, "Close failed after retry (price kept moving).")

    async def modify(self, ticket: str, sl: float | None, tp: float | None) -> bool:
        c = self.c.const
        found = await self.c.call("positions_get", ticket=int(ticket))
        if not found or found[0].magic != SCALPER_MAGIC:
            return False
        p = found[0]
        result = await self._order_call("order_send", {
            "action": c("TRADE_ACTION_SLTP"), "symbol": p.symbol, "position": int(ticket),
            "sl": float(sl) if sl is not None else p.sl, "tp": float(tp) if tp is not None else p.tp,
        })
        return result is not None and result.retcode == c("TRADE_RETCODE_DONE")

    async def loss_money(self, instrument: str, side: str, volume: float, entry: float, sl: float) -> float | None:
        spec = await self.spec(instrument)
        c = self.c.const
        profit = await self.c.call(
            "order_calc_profit", c("ORDER_TYPE_BUY") if side == "BUY" else c("ORDER_TYPE_SELL"),
            spec.symbol, float(volume), float(entry), float(sl),
        )
        return None if profit is None else abs(float(profit))

    # -- history ----------------------------------------------------------------
    async def deals_since(self, since_ts: float) -> list[Deal]:
        c = self.c.const
        start = datetime.utcfromtimestamp(since_ts) - timedelta(days=2)  # wide: server-time offsets
        end = datetime.utcnow() + timedelta(days=2)
        raw = await self.c.call("history_deals_get", start, end)
        if not raw:
            return []
        offset = 0
        entry_in = {c("DEAL_ENTRY_IN")}
        entry_out = {c("DEAL_ENTRY_OUT"), c("DEAL_ENTRY_INOUT"), c("DEAL_ENTRY_OUT_BY")}
        mine = [d for d in raw if d.magic == SCALPER_MAGIC]
        if mine:
            offset = await self._offset(mine[0].symbol)
        entry_cost: dict[int, float] = {}
        for d in mine:
            if d.entry in entry_in:
                entry_cost[d.position_id] = entry_cost.get(d.position_id, 0.0) + float(d.commission) + float(getattr(d, "fee", 0.0))
        deals: list[Deal] = []
        for d in sorted(mine, key=lambda x: x.time):
            ts = float(d.time) - offset
            if d.entry in entry_out and ts >= since_ts:
                net = float(d.profit) + float(d.commission) + float(d.swap) + float(getattr(d, "fee", 0.0))
                net += entry_cost.pop(d.position_id, 0.0)
                deals.append(Deal(str(d.position_id), self._instrument_for(d.symbol), net, ts))
        return deals

    async def aclose(self) -> None:
        return None
