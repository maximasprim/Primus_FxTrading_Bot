# MT5 bridge

Lets the backend run on **Render** while MetaTrader 5 stays on your **Windows machine**.

```
Render backend ──HTTPS + API key──▶  Windows: mt5_bridge ──▶ MT5 terminal ──▶ broker
```

The bridge has no trading logic. Strategy, risk limits and the scalper stay in the backend; the bridge just relays
MetaTrader5 calls. If `MT5_BRIDGE_URL` is empty, nothing changes: the backend uses a local terminal exactly as before.

## 1. Windows machine

1. Open MT5, log in, and turn on **Algo Trading** (toolbar button green).
2. In the `Server` folder: `pip install -r requirements.txt` (adds MetaTrader5 on Windows).
3. In `Server\.env` set the same MT5 values you already use, plus a secret:
   ```
   MT5_LOGIN=...  MT5_PASSWORD=...  MT5_SERVER=...
   MT5_BRIDGE_API_KEY=<long random, e.g. python -c "import secrets;print(secrets.token_urlsafe(32))">
   ```
   Optional safety: `BRIDGE_MAX_VOLUME=0.10` (max lots per order), `BRIDGE_READ_ONLY=true` (data/account only, no orders: good for a first test).
4. Start it: double-click `run_bridge.bat` (or `python -m mt5_bridge`). It listens on `127.0.0.1:8765`.
5. Make it reachable over HTTPS. Easiest is a **Cloudflare Tunnel** (no open ports):
   `cloudflared tunnel --url http://localhost:8765` (quick test URL) or a named tunnel on your domain. Tailscale also works.
   Don't expose the plain-HTTP port directly to the internet.
6. Keep the PC awake, MT5 open, and the bridge running (Task Scheduler "at log on" works well).

## 2. Render (backend) environment

```
MT5_BRIDGE_URL=https://<your tunnel URL>
MT5_BRIDGE_API_KEY=<same secret>
MARKET_DATA_PROVIDER=mt5        # candles/prices from your terminal
BROKER_ADAPTER=paper            # or mt5 to route the execution pipeline's orders to the real account too
MT5_SYMBOL_SUFFIX=...           # same as locally, if your broker uses one
```
Do **not** set `MT5_LOGIN/PASSWORD` on Render: the login never leaves the Windows machine.
In the Scalper tab, `backend: mt5` then trades through the bridge.

## 3. Check it works

* `https://<tunnel>/health` -> `{"status":"ok"}`
* Dashboard Account tab (with `BROKER_ADAPTER=mt5`) or Scalper pre-flight checks show your real account.
* Test with a **demo** account and `BRIDGE_READ_ONLY=true` first, then turn read-only off.

## Important

* **Run only ONE scalper.** If the backend on this Windows machine also has the scalper running, it and Render would both trade the same account. Stop the local one.
* If the bridge/PC goes offline the backend raises an error and the scalper halts safely; open trades keep their broker-side SL/TP.
* `order_send` is idempotent: a network retry can't place a trade twice. If a reply is lost and every retry fails, the error says the outcome is unknown, so check positions.
* The bridge only allows a whitelist of MetaTrader5 functions and can't change accounts. Treat `MT5_BRIDGE_API_KEY` like a trading password; rotate it if leaked.
