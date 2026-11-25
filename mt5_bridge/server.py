"""
MT5 bridge -- run this on the WINDOWS machine that has the MetaTrader 5 terminal.

It is a thin, authenticated RPC wrapper around the `MetaTrader5` package. It has
no trading logic of its own: the strategy, risk checks and scalper all stay in
the main backend. The backend's `app/mt5/remote.py` talks to this.

Endpoints
  GET  /health         no auth, says only that the process is up
  GET  /v1/health      (auth) is the MT5 terminal connected?
  GET  /v1/constants   (auth) every MetaTrader5 constant (ORDER_TYPE_BUY, ...)
  POST /v1/rpc         (auth) {"fn","args","kwargs","idem"} -> whitelisted MetaTrader5 call

Safety
  * Every /v1 call needs the X-Bridge-Key header (constant-time compare).
  * Only whitelisted MetaTrader5 functions can be called. `initialize`, `login`
    and `shutdown` are NOT reachable, so the bridge can't be pointed at another
    account. The MT5 login is read from this machine's own environment.
  * All MT5 calls run one at a time on a single thread (the package isn't thread-safe).
  * `order_send` is idempotent per `idem` key: a retried request returns the first
    result instead of trading twice.
  * BRIDGE_READ_ONLY=true blocks order_send. BRIDGE_MAX_VOLUME caps lots per order.
"""
from __future__ import annotations

import asyncio
import hmac
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.mt5.codec import CodecError, decode, encode

logger = logging.getLogger("mt5_bridge")

READ_FUNCS = {
    "account_info", "terminal_info", "version", "last_error",
    "symbol_info", "symbol_info_tick", "symbols_total", "symbols_get", "symbol_select",
    "copy_rates_from", "copy_rates_from_pos", "copy_rates_range", "copy_ticks_from", "copy_ticks_range",
    "positions_total", "positions_get", "orders_total", "orders_get",
    "history_orders_total", "history_orders_get", "history_deals_total", "history_deals_get",
    "order_calc_margin", "order_calc_profit", "order_check",
}
TRADE_FUNCS = {"order_send"}
ALLOWED_FUNCS = READ_FUNCS | TRADE_FUNCS
_IDEM_TTL = 600.0


class MT5Session:
    """Owns the terminal connection. Everything runs on one worker thread."""

    def __init__(self, mt5_module: Any | None, login: int, password: str, server: str, path: str) -> None:
        self._mt5 = mt5_module
        self.login, self.password, self.server, self.path = login, password, server, path
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mt5-bridge")
        self._initialized = False
        self._checked_at = 0.0

    def _ensure_sync(self) -> None:
        if self._mt5 is None:
            try:
                import MetaTrader5 as mt5  # type: ignore[import-not-found]
            except ImportError as exc:
                raise RuntimeError("The MetaTrader5 package isn't installed here (Windows only: pip install MetaTrader5).") from exc
            self._mt5 = mt5
        now = time.monotonic()
        if self._initialized and now - self._checked_at > 10:  # lost the terminal (closed/crashed)? reconnect
            self._checked_at = now
            if self._mt5.terminal_info() is None:
                self._initialized = False
        if self._initialized:
            return
        kwargs: dict[str, Any] = {}
        if self.path:
            kwargs["path"] = self.path
        if self.login:
            kwargs.update(login=self.login, password=self.password, server=self.server)
        if not self._mt5.initialize(**kwargs):
            raise RuntimeError(f"MT5 initialize() failed: {self._mt5.last_error()}. Is the terminal open and logged in?")
        self._initialized, self._checked_at = True, now

    async def run(self, fn: Any) -> Any:
        def job() -> Any:
            self._ensure_sync()
            return fn(self._mt5)
        return await asyncio.get_running_loop().run_in_executor(self._executor, job)

    def shutdown(self) -> None:
        try:
            if self._mt5 is not None and self._initialized:
                self._mt5.shutdown()
        finally:
            self._executor.shutdown(wait=False)


def _json(status: int, payload: dict) -> JSONResponse:
    return JSONResponse(payload, status_code=status)


def create_app(
    api_key: str,
    mt5_module: Any | None = None,
    *,
    login: int = 0,
    password: str = "",
    server: str = "",
    path: str = "",
    read_only: bool = False,
    max_volume: float = 0.0,
) -> FastAPI:
    if len(api_key) < 16 or api_key.lower().startswith("change"):
        raise ValueError("MT5_BRIDGE_API_KEY must be a long random secret (16+ characters).")
    session = MT5Session(mt5_module, login, password, server, path)
    app = FastAPI(title="MT5 Bridge", docs_url=None, redoc_url=None, openapi_url=None)
    idem: dict[str, tuple[float, asyncio.Future]] = {}

    @app.middleware("http")
    async def auth(request: Request, call_next):  # noqa: ANN001
        if request.url.path.startswith("/v1/"):
            sent = request.headers.get("x-bridge-key", "")
            if not hmac.compare_digest(sent.encode(), api_key.encode()):
                await asyncio.sleep(0.5)  # slow down key guessing
                return _json(401, {"ok": False, "error": "bad or missing X-Bridge-Key"})
        return await call_next(request)

    @app.on_event("shutdown")
    def _bye() -> None:
        session.shutdown()

    @app.get("/health")
    async def health_public() -> dict:
        return {"status": "ok"}

    @app.get("/v1/health")
    async def health() -> JSONResponse:
        try:
            def probe(m: Any) -> dict:
                term = m.terminal_info()
                acct = m.account_info()
                return {"connected": bool(term and term.connected), "trade_allowed": bool(term and term.trade_allowed),
                        "login": getattr(acct, "login", None)}
            info = await session.run(probe)
        except Exception as exc:  # noqa: BLE001
            return _json(503, {"ok": False, "error": str(exc)})
        return _json(200, {"ok": info["connected"], "read_only": read_only, **info,
                           "error": None if info["connected"] else "MT5 terminal isn't connected to the broker."})

    @app.get("/v1/constants")
    async def constants() -> JSONResponse:
        try:
            def grab(m: Any) -> dict:
                return {n: getattr(m, n) for n in dir(m)
                        if n.isupper() and isinstance(getattr(m, n), (int, float, str)) and not isinstance(getattr(m, n), bool)}
            return _json(200, {"constants": await session.run(grab)})
        except Exception as exc:  # noqa: BLE001
            return _json(503, {"ok": False, "error": str(exc)})

    def _guard_order(m: Any, args: list, kwargs: dict) -> str | None:
        if read_only:
            return "bridge is in read-only mode (BRIDGE_READ_ONLY=true): orders are blocked"
        req = args[0] if args and isinstance(args[0], dict) else kwargs
        vol = req.get("volume") if isinstance(req, dict) else None
        if max_volume > 0 and isinstance(vol, (int, float)) and vol > max_volume:
            return f"volume {vol} exceeds the bridge cap BRIDGE_MAX_VOLUME={max_volume}"
        return None

    async def _execute(fn: str, args: list, kwargs: dict) -> JSONResponse:
        def call(m: Any) -> dict:
            if fn in TRADE_FUNCS:
                blocked = _guard_order(m, args, kwargs)
                if blocked:
                    return {"blocked": blocked}
            result = getattr(m, fn)(*args, **kwargs)
            err = m.last_error() if result is None else None  # captured on the same thread: no race
            return {"result": encode(result), "last_error": encode(err) if err is not None else None}
        try:
            out = await session.run(call)
        except CodecError as exc:
            return _json(500, {"ok": False, "error": f"couldn't encode the MT5 result: {exc}"})
        except Exception as exc:  # noqa: BLE001
            return _json(503, {"ok": False, "error": str(exc)})
        if "blocked" in out:
            return _json(403, {"ok": False, "error": out["blocked"]})
        return _json(200, {"ok": True, **out})

    @app.post("/v1/rpc")
    async def rpc(request: Request) -> JSONResponse:
        try:
            body = await request.json()
            fn = body["fn"]
            args = [decode(a) for a in body.get("args", [])]
            kwargs = {str(k): decode(v) for k, v in (body.get("kwargs") or {}).items()}
        except (ValueError, KeyError, TypeError, CodecError) as exc:
            return _json(400, {"ok": False, "error": f"malformed request: {exc}"})
        if fn not in ALLOWED_FUNCS:
            return _json(403, {"ok": False, "error": f"{fn!r} isn't an allowed MT5 function"})
        key = body.get("idem")
        if fn not in TRADE_FUNCS or not key:
            return await _execute(fn, args, kwargs)

        now = time.monotonic()
        for k in [k for k, (t, f) in idem.items() if now - t > _IDEM_TTL and f.done()]:
            del idem[k]
        existing = idem.get(key)
        if existing:  # a retry of a request we already ran (or are running): never trade twice
            return await asyncio.shield(existing[1])
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        idem[key] = (now, fut)
        try:
            resp = await _execute(fn, args, kwargs)
        except BaseException as exc:  # noqa: BLE001
            idem.pop(key, None)
            fut.cancel()
            raise exc
        if resp.status_code >= 500:  # nothing was sent to the broker: allow a clean retry
            idem.pop(key, None)
        fut.set_result(resp)
        return resp

    return app


def app_from_env() -> FastAPI:
    """Build the app from environment variables (also read from .env in the working directory)."""
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
    env = os.environ.get
    return create_app(
        env("MT5_BRIDGE_API_KEY", ""),
        login=int(env("MT5_LOGIN", "0") or 0), password=env("MT5_PASSWORD", ""),
        server=env("MT5_SERVER", ""), path=env("MT5_TERMINAL_PATH", ""),
        read_only=env("BRIDGE_READ_ONLY", "false").lower() in ("1", "true", "yes"),
        max_volume=float(env("BRIDGE_MAX_VOLUME", "0") or 0),
    )
