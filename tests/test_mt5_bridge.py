"""MT5 bridge + RemoteMT5, end to end, against the same FakeMT5 the gateway tests use (the real package is Windows-only)."""
import time
from datetime import datetime, timedelta
from types import SimpleNamespace as NS

import httpx
import numpy as np
import pytest
from starlette.testclient import TestClient

from app.data.providers.mt5_provider import MT5MarketDataProvider
from app.mt5.client import MT5Client, MT5Unavailable
from app.mt5.codec import decode, encode
from app.mt5.remote import RemoteMT5
from app.scalper.gateway import SCALPER_MAGIC
from app.scalper.mt5_gateway import MT5Gateway
from mt5_bridge.server import create_app
from tests.test_mt5_gateway import FakeMT5, pos

KEY = "k" * 24


def build(fake=None, **kw):
    fake = fake or FakeMT5()
    app = create_app(KEY, fake, **kw)
    http = TestClient(app, base_url="http://bridge")
    http.__enter__()
    remote = RemoteMT5("http://bridge", KEY, http=http)
    return fake, remote, http


@pytest.fixture
def stack():
    fake, remote, http = build()
    yield fake, remote, http
    http.__exit__(None, None, None)


# ---- codec ----------------------------------------------------------------
def test_codec_round_trips_mt5_shapes():
    from collections import namedtuple
    Pos = namedtuple("TradePosition", "ticket symbol volume")
    rates = np.array([(1, 1.5, 2)], dtype=[("time", "i8"), ("open", "f8"), ("n", "i4")])
    src = {"a": (Pos(1, "EURUSD", 0.1), (3, "x")), "dt": datetime(2026, 1, 1, 5), "nan": float("nan"), "r": rates}
    out = decode(encode(src))
    assert out["a"][0].symbol == "EURUSD" and out["a"][0].ticket == 1 and out["a"][1] == (3, "x")
    assert out["dt"] == datetime(2026, 1, 1, 5) and out["nan"] != out["nan"]
    assert out["r"].dtype.names == ("time", "open", "n") and out["r"][0]["open"] == 1.5


# ---- auth + safety ----------------------------------------------------------
def test_bad_key_rejected_and_public_health_open(stack):
    _, remote, http = stack
    assert http.get("/health").json() == {"status": "ok"}
    assert http.get("/v1/health").status_code == 401
    bad = RemoteMT5("http://bridge", "wrong-key-wrong-key", http=http)
    with pytest.raises(MT5Unavailable, match="API key"):
        bad.account_info()


def test_weak_key_refused():
    with pytest.raises(ValueError):
        create_app("short", FakeMT5())


def test_only_whitelisted_functions(stack):
    _, remote, http = stack
    for fn in ("initialize", "shutdown", "login", "__import__"):
        r = http.post("/v1/rpc", json={"fn": fn, "args": [], "kwargs": {}}, headers={"X-Bridge-Key": KEY})
        assert r.status_code == 403


def test_read_only_and_max_volume_block_orders():
    fake, remote, http = build(read_only=True)
    with pytest.raises(MT5Unavailable, match="read-only"):
        remote.order_send({"volume": 0.01})
    assert fake.sent == []
    http.__exit__(None, None, None)
    fake, remote, http = build(max_volume=0.5)
    with pytest.raises(MT5Unavailable, match="exceeds"):
        remote.order_send({"volume": 1.0, "action": 1, "position": 0})
    assert fake.sent == []
    http.__exit__(None, None, None)


# ---- the module look-alike ------------------------------------------------------
def test_remote_acts_like_the_module(stack):
    fake, remote, _ = stack
    assert remote.initialize() is True
    assert remote.ORDER_TYPE_BUY == FakeMT5.ORDER_TYPE_BUY and remote.TRADE_RETCODE_DONE == 10009
    assert remote.account_info().login == 123
    assert remote.symbol_info("NOPE") is None
    rates = remote.copy_rates_from_pos("EURUSDm", 1, 0, 3)
    assert len(rates) == 3 and rates.dtype.names[0] == "time"
    with pytest.raises(AttributeError):
        remote.NOT_A_CONSTANT


def test_last_error_travels_with_none_results(stack):
    fake, remote, _ = stack
    fake.kw_only = True
    assert remote.order_send({"symbol": "x"}) is None
    assert remote.last_error()[0] == -2


# ---- unchanged app code through the bridge ----------------------------------------
@pytest.mark.asyncio
async def test_scalper_gateway_and_provider_work_through_the_bridge(stack):
    fake, remote, _ = stack
    fake.positions = [pos(7), pos(8, magic=999)]
    client = MT5Client(mt5_module=remote)
    gw = MT5Gateway(client, "m")
    acct = await gw.account()
    assert acct.connected and acct.is_demo and acct.algo_trading_enabled
    spec = await gw.spec("EUR/USD")
    assert spec.symbol == "EURUSDm" and spec.volume_min == 0.01
    assert (await gw.tick("EUR/USD")).bid == pytest.approx(1.085)
    assert len(await gw.candles("EUR/USD", 5)) == 5
    assert [p.ticket for p in await gw.positions()] == ["7"]          # foreign magic still filtered
    out = await gw.send_market("EUR/USD", "BUY", 0.01, 1.08, 1.09)
    assert out.ok and out.ticket == "5001" and fake.sent[-1]["magic"] == SCALPER_MAGIC
    closed = await gw.close("7")
    assert closed.ok
    provider = MT5MarketDataProvider(client, symbol_suffix="m")
    price = await provider.get_current_price("EUR/USD")
    assert price["ask"] > price["bid"]
    df = await provider.get_historical_candles("EUR/USD", "1M", datetime.utcnow() - timedelta(minutes=30), datetime.utcnow())
    assert not df.empty


@pytest.mark.asyncio
async def test_execution_adapter_works_through_the_bridge(stack, monkeypatch):
    fake, remote, _ = stack
    from app.execution.adapters import mt5_remote_adapter as mod
    from app.execution.broker_interface import OrderRequest
    monkeypatch.setattr(mod, "get_remote_mt5", lambda: remote)
    fake.SYMBOL_TRADE_MODE_DISABLED = 0   # the real package has it; the shared fake doesn't
    base = fake.account_info
    fake.account_info = lambda: NS(**vars(base()), company="Fake Broker", margin=0.0, margin_free=1000.0, leverage=500)
    adapter = mod.MT5RemoteAdapter(symbol_suffix="m")
    info = await adapter.get_account_info()
    assert info.connected and info.balance == 1000.0
    assert await adapter.is_connection_healthy() and await adapter.is_market_open("EUR/USD")
    assert await adapter.calculate_position_size(1000.0, 1.0, 1.0850, 1.0800, "EUR/USD") > 0


# ---- failure behaviour ----------------------------------------------------------------
def test_unreachable_bridge_raises_not_empty_results():
    def boom(request):
        raise httpx.ConnectError("down")
    remote = RemoteMT5("http://bridge", KEY, http=httpx.Client(transport=httpx.MockTransport(boom)))
    assert remote.initialize() is False and "reach" in remote.last_error()[1]
    with pytest.raises(MT5Unavailable):
        remote.positions_get()          # must NOT look like "no open positions"


def test_order_send_retry_never_double_trades():
    fake, _, http = build()
    sent_bodies = []
    real = http.request
    state = {"n": 0}

    def flaky(method, url, **kw):
        resp = real(method, url, **kw)                  # the bridge DID run the order...
        if kw.get("json", {}).get("fn") == "order_send":
            sent_bodies.append(kw["json"])
            state["n"] += 1
            if state["n"] == 1:
                raise httpx.ReadTimeout("response lost")  # ...but we never saw the reply
        return resp

    http.request = flaky
    remote = RemoteMT5("http://bridge", KEY, http=http, retries=2)
    res = remote.order_send({"action": 1, "symbol": "EURUSDm", "volume": 0.01, "price": 1.1})
    assert res.retcode == 10009
    assert len(fake.sent) == 1                           # exactly one order reached MT5
    assert sent_bodies[0]["idem"] == sent_bodies[1]["idem"]
    http.__exit__(None, None, None)


def test_order_send_unknown_outcome_message():
    def boom(request):
        raise httpx.ConnectError("down")
    remote = RemoteMT5("http://bridge", KEY, http=httpx.Client(transport=httpx.MockTransport(boom)), retries=1)
    with pytest.raises(MT5Unavailable, match="UNKNOWN"):
        remote.order_send({"volume": 0.01})


def test_terminal_down_reports_503_as_unavailable():
    class Dead(FakeMT5):
        def initialize(self, **kw): return False
        def last_error(self): return (-10003, "IPC initialize failed")
    fake, remote, http = build(Dead())
    assert remote.initialize() is False
    with pytest.raises(MT5Unavailable, match="isn't available"):
        remote.account_info()
    http.__exit__(None, None, None)
