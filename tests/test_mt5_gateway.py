"""MT5 gateway + data provider against a fake `MetaTrader5` module (the real one is Windows-only)."""
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS

import pytest

from app.mt5.client import MT5Client, MT5Unavailable
from app.scalper.gateway import SCALPER_MAGIC
from app.scalper.mt5_gateway import MT5Gateway
from app.data.providers.mt5_provider import MT5MarketDataProvider


class FakeMT5:
    ACCOUNT_TRADE_MODE_DEMO, ACCOUNT_TRADE_MODE_REAL = 0, 2
    ACCOUNT_MARGIN_MODE_RETAIL_NETTING, ACCOUNT_MARGIN_MODE_RETAIL_HEDGING = 0, 2
    TIMEFRAME_M1, TIMEFRAME_M5, TIMEFRAME_H1 = 1, 5, 16385
    ORDER_FILLING_FOK, ORDER_FILLING_IOC, ORDER_FILLING_RETURN = 0, 1, 2
    TRADE_ACTION_DEAL, TRADE_ACTION_SLTP = 1, 6
    ORDER_TYPE_BUY, ORDER_TYPE_SELL, ORDER_TIME_GTC = 0, 1, 0
    TRADE_RETCODE_DONE = 10009
    POSITION_TYPE_BUY, POSITION_TYPE_SELL = 0, 1
    SYMBOL_TRADE_MODE_FULL = 4
    DEAL_ENTRY_IN, DEAL_ENTRY_OUT, DEAL_ENTRY_INOUT, DEAL_ENTRY_OUT_BY = 0, 1, 2, 3

    def __init__(self, demo=True, hedging=True, algo=True, filling=3, offset=7200, kw_only=False, broken=False,
                 contract=100_000, equity=1000.0, currency="USD"):
        self.contract, self.equity, self.currency = contract, equity, currency   # contract=1000: Exness-style cent lot
        self.demo, self.hedging, self.algo, self.filling, self.offset = demo, hedging, algo, filling, offset
        self.kw_only = kw_only   # strict build: refuses the documented order_send(request) -> (-2, 'Unnamed arguments not allowed')
        self.broken = broken     # a build that refuses BOTH calling forms
        self._err = (1, "fake")
        self.calls = []          # (function, "dict" | "kwargs") for every order_send / order_check call
        self.sent, self.retcodes, self.positions, self.deals = [], [], [], []
        self.symbols = {"EURUSDm": 1.0850}

    # connection
    def initialize(self, **kw): self.init_kwargs = kw; return True
    def shutdown(self): pass
    def last_error(self): return self._err
    def terminal_info(self): return NS(connected=True, trade_allowed=self.algo)
    def account_info(self):
        return NS(login=123, server="Fake-Demo", currency=self.currency, balance=self.equity, equity=self.equity,
                  trade_mode=0 if self.demo else 2, margin_mode=2 if self.hedging else 0,
                  trade_allowed=True, trade_expert=True)
    # market data
    def symbol_info(self, s):
        if s not in self.symbols: return None
        return NS(visible=True, digits=5, point=0.00001, volume_min=0.01, volume_max=100.0, volume_step=0.01,
                  trade_stops_level=10, filling_mode=self.filling, trade_mode=4)
    def symbol_select(self, s, v): return True
    def symbol_info_tick(self, s):
        if s not in self.symbols: return None
        p = self.symbols[s]
        return NS(bid=p, ask=p + 0.00008, time=int(time.time()) + self.offset)
    def copy_rates_from_pos(self, s, tf, pos, n):
        import numpy as np
        base = int(time.time()) + self.offset - n * 60
        return np.array([(base + i * 60, 1.08, 1.09, 1.07, 1.085, 10, 8) for i in range(n)],
                        dtype=[("time", "i8"), ("open", "f8"), ("high", "f8"), ("low", "f8"), ("close", "f8"), ("tick_volume", "i8"), ("spread", "i4")])
    # trading
    def order_check(self, *args, **kw):
        req = self._accept("order_check", args, kw)
        return None if req is None else NS(retcode=self.check_retcode if hasattr(self, "check_retcode") else 0, comment="Done")

    def order_send(self, *args, **kw):
        req = self._accept("order_send", args, kw)
        return None if req is None else self._do_send(req)

    def _accept(self, fn, args, kw):
        self.calls.append((fn, "dict" if args else "kwargs"))
        if self.broken or (args and self.kw_only):
            self._err = (-2, "Unnamed arguments not allowed")
            return None
        return args[0] if args else kw

    def _do_send(self, req):
        self.sent.append(req)
        code = self.retcodes.pop(0) if self.retcodes else self.TRADE_RETCODE_DONE
        if code == self.TRADE_RETCODE_DONE and "position" in req and req["action"] == self.TRADE_ACTION_DEAL:
            self.positions = [p for p in self.positions if p.ticket != req["position"]]
        return NS(retcode=code, order=5001, price=req.get("price", 0.0), comment="fake")
    def positions_get(self, ticket=None):
        res = [p for p in self.positions if ticket is None or p.ticket == ticket]
        return tuple(res) or None
    def order_calc_profit(self, t, s, vol, a, b): return -abs(a - b) * self.contract * vol
    def history_deals_get(self, a, b): return tuple(self.deals)


def make(**kw):
    fake = FakeMT5(**kw)
    client = MT5Client(login=1, password="x", server="s", mt5_module=fake)
    return fake, client, MT5Gateway(client, "m")


def pos(ticket, magic=SCALPER_MAGIC, side=0):
    return NS(ticket=ticket, magic=magic, symbol="EURUSDm", type=side, volume=0.1, price_open=1.0850,
              sl=1.08, tp=1.09, profit=2.5, swap=-0.1, time=int(time.time()) + 7200)


@pytest.mark.asyncio
async def test_account_flags_demo_algo_hedging():
    _, _, gw = make()
    a = await gw.account()
    assert a.connected and a.is_demo and a.algo_trading_enabled and a.trade_allowed and a.hedging
    _, _, gw = make(demo=False, hedging=False, algo=False)
    a = await gw.account()
    assert not a.is_demo and not a.algo_trading_enabled and not a.hedging


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,expected", [(3, "ORDER_FILLING_IOC"), (1, "ORDER_FILLING_FOK"), (0, "ORDER_FILLING_RETURN")])
async def test_filling_mode_follows_symbol(mode, expected):
    fake, _, gw = make(filling=mode)
    out = await gw.send_market("EUR/USD", "BUY", 0.01, 1.08, 1.09)
    assert out.ok and out.ticket == "5001"
    assert fake.sent[-1]["type_filling"] == getattr(FakeMT5, expected)
    assert fake.sent[-1]["magic"] == SCALPER_MAGIC
    assert fake.sent[-1]["symbol"] == "EURUSDm"          # suffix applied
    assert fake.sent[-1]["sl"] == 1.08 and fake.sent[-1]["tp"] == 1.09  # protected from the first tick


@pytest.mark.asyncio
async def test_requote_is_retried_once_then_succeeds():
    fake, _, gw = make()
    fake.retcodes = [10004]
    out = await gw.send_market("EUR/USD", "SELL", 0.01, 1.09, 1.08)
    assert out.ok and len(fake.sent) == 2


@pytest.mark.asyncio
async def test_hard_rejection_returns_reason_not_exception():
    fake, _, gw = make()
    fake.retcodes = [10019]  # no money
    out = await gw.send_market("EUR/USD", "BUY", 0.01, 1.08, 1.09)
    assert not out.ok and "10019" in out.reason and len(fake.sent) == 1


@pytest.mark.asyncio
async def test_unknown_symbol_gives_actionable_error():
    _, _, gw = make()
    with pytest.raises(RuntimeError, match="MT5_SYMBOL_SUFFIX"):
        await gw.spec("GBP/USD")


@pytest.mark.asyncio
async def test_only_own_positions_listed_and_closable():
    fake, _, gw = make()
    fake.positions = [pos(1), pos(2, magic=999)]  # 2 = a manual trade
    listed = await gw.positions()
    assert [p.ticket for p in listed] == ["1"] and listed[0].instrument == "EUR/USD" and listed[0].side == "BUY"
    assert listed[0].profit == pytest.approx(2.4)
    refused = await gw.close("2")
    assert not refused.ok and "didn't open" in refused.reason
    assert not any(r.get("position") == 2 for r in fake.sent)
    ok = await gw.close("1")
    assert ok.ok and fake.sent[-1]["type"] == FakeMT5.ORDER_TYPE_SELL and fake.sent[-1]["position"] == 1
    assert not (await gw.modify("2", 1.0, 1.1))  # can't modify foreign trade either


@pytest.mark.asyncio
async def test_server_time_converted_to_utc():
    _, _, gw = make(offset=7200)  # broker runs UTC+2
    tick = await gw.tick("EUR/USD")
    assert abs(tick.time - time.time()) < 5
    df = await gw.candles("EUR/USD", 10)
    assert abs(df.index[-1].timestamp() - time.time()) < 120
    _, _, gw0 = make(offset=0)
    assert abs((await gw0.tick("EUR/USD")).time - time.time()) < 5


@pytest.mark.asyncio
async def test_deals_net_of_costs_and_filtered_by_magic():
    fake, _, gw = make()
    now = int(time.time()) + 7200
    d = lambda **k: NS(magic=SCALPER_MAGIC, symbol="EURUSDm", swap=0.0, fee=0.0, position_id=7, **k)
    fake.deals = [d(entry=0, time=now - 50, profit=0.0, commission=-0.35),
                  d(entry=1, time=now - 10, profit=3.0, commission=-0.35),
                  NS(magic=1, symbol="EURUSDm", entry=1, time=now - 5, profit=99.0, commission=0, swap=0, fee=0, position_id=8)]
    deals = await gw.deals_since(time.time() - 3600)
    assert len(deals) == 1 and deals[0].profit == pytest.approx(2.3) and deals[0].instrument == "EUR/USD"


@pytest.mark.asyncio
async def test_loss_money_and_provider():
    fake, client, gw = make()
    assert await gw.loss_money("EUR/USD", "BUY", 0.1, 1.0850, 1.0840) == pytest.approx(10.0)
    prov = MT5MarketDataProvider(client, "m")
    price = await prov.get_current_price("EUR/USD")
    assert price["ask"] > price["bid"] and abs(price["timestamp"].timestamp() - time.time()) < 5
    end = datetime.now(timezone.utc)
    df = await prov.get_historical_candles("EUR/USD", "1M", end - timedelta(minutes=30), end)
    assert {"open", "high", "low", "close", "volume"} <= set(df.columns) and len(df) > 0
    last = await prov.get_latest_candle("EUR/USD", "1M")
    assert last["close"] == pytest.approx(1.085)
    with pytest.raises(ValueError):
        await prov.get_latest_candle("EUR/USD", "3M")


@pytest.mark.asyncio
async def test_missing_metatrader_package_gives_clear_message(monkeypatch):
    import sys
    monkeypatch.setitem(sys.modules, "MetaTrader5", None)  # makes `import MetaTrader5` fail on any OS
    client = MT5Client(login=1, password="x", server="s")  # no injected module
    with pytest.raises(MT5Unavailable, match="Windows"):
        await client.ensure_connected()
    gw = MT5Gateway(client)
    a = await gw.account()
    assert not a.connected and "Windows" in a.error  # surfaced as a status, not a crash


# ---------------------------------------------------------------- strict MT5 builds: (-2, 'Unnamed arguments not allowed')
@pytest.mark.asyncio
async def test_strict_build_falls_back_to_keyword_form_and_remembers_it():
    """Reported on a real terminal: every order failed with (-2, 'Unnamed arguments not allowed')."""
    fake, _, gw = make(kw_only=True)
    out = await gw.send_market("EUR/USD", "BUY", 0.01, 1.08, 1.09)
    assert out.ok and out.ticket == "5001"
    assert fake.calls[:2] == [("order_send", "dict"), ("order_send", "kwargs")]   # documented form first, then keywords
    assert fake.sent[-1]["symbol"] == "EURUSDm" and fake.sent[-1]["magic"] == SCALPER_MAGIC and fake.sent[-1]["sl"] == 1.08
    fake.calls.clear()
    assert (await gw.send_market("EUR/USD", "SELL", 0.01, 1.09, 1.08)).ok
    assert fake.calls == [("order_send", "kwargs")]                                # remembered: no wasted attempt
    # closing and stop-modification use the same path
    fake.positions = [pos(7)]
    assert (await gw.modify("7", 1.081, None)) and fake.sent[-1]["action"] == FakeMT5.TRADE_ACTION_SLTP
    assert (await gw.close("7")).ok


@pytest.mark.asyncio
async def test_normal_build_still_uses_the_documented_form_only():
    fake, _, gw = make()
    assert (await gw.send_market("EUR/USD", "BUY", 0.01, 1.08, 1.09)).ok
    assert fake.calls == [("order_send", "dict")]


@pytest.mark.asyncio
async def test_other_errors_are_not_retried_in_the_other_form():
    fake, _, gw = make()
    fake.order_send = lambda *a, **k: (fake.calls.append(("order_send", "dict" if a else "kwargs")), setattr(fake, "_err", (-1, "terminal: no ipc")), None)[-1]
    out = await gw.send_market("EUR/USD", "BUY", 0.01, 1.08, 1.09)
    assert not out.ok and "no ipc" in out.reason
    assert fake.calls == [("order_send", "dict")]   # a non -2 failure must not trigger a second attempt


@pytest.mark.asyncio
async def test_build_that_refuses_both_forms_gives_an_actionable_message():
    fake, _, gw = make(broken=True)
    out = await gw.send_market("EUR/USD", "BUY", 0.01, 1.08, 1.09)
    assert not out.ok and "Unnamed arguments" in out.reason and "pip install -U MetaTrader5" in out.reason
    assert fake.sent == []   # nothing was traded


@pytest.mark.asyncio
async def test_order_check_dry_run_validates_without_trading():
    for kw in (False, True):
        fake, _, gw = make(kw_only=kw)
        res = await gw.check_order("EUR/USD")
        assert res["ok"] and fake.sent == []                    # order_check only: nothing sent
        assert fake.calls[-1][0] == "order_check"
    fake, _, gw = make(broken=True)
    res = await gw.check_order("EUR/USD")
    assert not res["ok"] and "Unnamed arguments" in res["detail"]
    fake, _, gw = make()
    fake.check_retcode = 10018
    res = await gw.check_order("EUR/USD")
    assert not res["ok"] and res["market_closed"]


@pytest.mark.asyncio
async def test_preflight_checks_report_the_order_problem_before_any_trade():
    from app.scalper.config import ScalperConfig
    from app.scalper.diagnostics import run_diagnostics
    cfg = ScalperConfig(instruments=["EUR/USD"])
    fake, _, gw = make()
    d = await run_diagnostics(cfg, gw)
    item = next(c for c in d["checks"] if c["name"].startswith("Order placement"))
    assert item["ok"] and d["ready"]
    fake, _, gw = make(broken=True)
    d = await run_diagnostics(cfg, gw)
    item = next(c for c in d["checks"] if c["name"].startswith("Order placement"))
    assert not item["ok"] and not d["ready"] and "MetaTrader5" in item["detail"]
    fake, _, gw = make()
    fake.check_retcode = 10018      # market closed is a warning, not a blocker
    d = await run_diagnostics(cfg, gw)
    assert next(c for c in d["checks"] if c["name"].startswith("Order placement"))["severity"] == "warning" and d["ready"]


# ---------------------------------------------------------------- cent / non-standard accounts: measure, don't assume
@pytest.mark.asyncio
async def test_pip_value_is_measured_from_the_broker_for_standard_and_cent_lots():
    _, _, std = make(contract=100_000)
    _, _, cent = make(contract=1_000)          # Exness Standard Cent: 1 lot = 1,000 units
    assert await std.pip_value("EUR/USD") == pytest.approx(10.0)
    assert await cent.pip_value("EUR/USD") == pytest.approx(0.1)


@pytest.mark.asyncio
async def test_preflight_shows_what_a_pip_is_worth_relative_to_the_account():
    from app.scalper.config import ScalperConfig
    from app.scalper.diagnostics import run_diagnostics
    cfg = ScalperConfig(instruments=["EUR/USD"])
    item = lambda d: next(c for c in d["checks"] if c["name"].startswith("Pip value"))
    # cent lot (1 lot = 1,000 units) on a 100-unit balance: 0.01 lot is worth 0.001 per pip = 0.001% of equity
    _, _, gw = make(contract=1_000, equity=100.0)
    d = await run_diagnostics(cfg, gw)
    assert item(d)["ok"] and "1 pip = 0.0010 USD = 0.001% of your equity" in item(d)["detail"]
    # standard-size lot on a tiny balance: one pip is 10% of equity -> loud warning, but not a hard blocker
    _, _, gw = make(contract=100_000, equity=1.0)
    d = await run_diagnostics(cfg, gw)
    assert not item(d)["ok"] and item(d)["severity"] == "warning" and "Too large" in item(d)["detail"] and d["ready"]


@pytest.mark.asyncio
async def test_balance_line_explains_cent_currencies():
    from app.scalper.config import ScalperConfig
    from app.scalper.diagnostics import run_diagnostics
    _, _, gw = make(contract=1_000, equity=100.0, currency="USC")
    d = await run_diagnostics(ScalperConfig(instruments=["EUR/USD"]), gw)
    bal = next(c for c in d["checks"] if c["name"] == "Balance")
    assert "100.00 USC" in bal["detail"] and "1.00 in whole currency" in bal["detail"]


@pytest.mark.asyncio
async def test_history_fetch_records_real_pip_value_and_account_equity(tmp_path):
    import json
    from app.scalper.backtest.data import fetch_mt5_history
    _, client, _ = make(contract=1_000, equity=100.0, currency="USC")
    meta = await fetch_mt5_history(client, ["EUR/USD"], "m", 2, tmp_path)
    assert meta["EUR/USD"]["pip_value_per_lot"] == pytest.approx(0.1)
    saved = json.loads((tmp_path / "mt5_meta.json").read_text())
    assert saved["account"] == {"equity": 100.0, "currency": "USC"}
    assert saved["instruments"]["EUR/USD"]["pip_value_per_lot"] == pytest.approx(0.1)
