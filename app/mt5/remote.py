"""
A stand-in for the `MetaTrader5` Python module that talks to the MT5 bridge.

Why
---
`MetaTrader5` only works on Windows next to a running terminal, so it can't run
on Render. The bridge (`mt5_bridge/`, run on the Windows box) exposes the
terminal over HTTPS. `RemoteMT5` looks like the module to the rest of the app:

    remote.account_info()            -> namedtuple, like the real thing
    remote.copy_rates_from_pos(...)  -> numpy structured array
    remote.ORDER_TYPE_BUY            -> the constant's real value (fetched from the bridge)
    remote.last_error()              -> (code, message)

so `MT5Client`, `MT5Gateway`, `MT5MarketDataProvider` and `MT5Adapter` run
completely unchanged on top of it. Used only when MT5_BRIDGE_URL is set; with it
empty the local Windows path is exactly what it always was.

Failure behaviour (important for a trading bot)
-----------------------------------------------
* The bridge being unreachable raises `MT5Unavailable`. It is NOT turned into a
  `None`/empty result, because `positions_get()` returning "nothing" would look
  like "all positions closed". The scalper's main loop already treats exceptions
  as errors and halts safely (broker-side SL/TP stay in place).
* `order_send` carries a unique idempotency key and is retried with the SAME key
  on network errors, so a lost response can never place the order twice. If every
  retry fails the outcome is unknown, and the error message says so.
"""
from __future__ import annotations

import logging
import threading
import uuid
from functools import lru_cache
from typing import Any, Callable

import httpx

from app.config import get_settings
from app.mt5.client import MT5Unavailable
from app.mt5.codec import CodecError, decode, encode

logger = logging.getLogger(__name__)

API_KEY_HEADER = "X-Bridge-Key"
_NO_ORDER_RETRY_STATUS = {400, 401, 403, 404, 422}


class RemoteMT5:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        timeout: float = 15.0,
        retries: int = 2,
        http: httpx.Client | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.retries = max(0, retries)
        self._http = http or httpx.Client(timeout=timeout)
        self._headers = {API_KEY_HEADER: api_key}
        self._constants: dict[str, Any] | None = None
        self._last_error: tuple[int, str] = (1, "Success")
        self._lock = threading.Lock()

    # -- the part of the MetaTrader5 API that isn't a plain call -----------
    def initialize(self, **_ignored: Any) -> bool:
        """Check the bridge is reachable and its terminal is connected.

        The bridge owns the MT5 login (its own .env), so credentials passed here
        are deliberately ignored: they never travel over the network.
        """
        try:
            health = self._request("GET", "/v1/health")
        except MT5Unavailable as exc:
            self._last_error = (-10001, str(exc))
            return False
        if not health.get("ok"):
            self._last_error = (-10004, str(health.get("error") or "the bridge's MT5 terminal isn't available"))
            return False
        self._load_constants()
        return True

    def shutdown(self) -> None:
        return None  # the bridge keeps the terminal connection; nothing to tear down here

    def last_error(self) -> tuple[int, str]:
        return self._last_error

    # -- module-style attribute access -------------------------------------
    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        if name.isupper():
            return self._constant(name)
        return self._make_call(name)

    def _make_call(self, name: str) -> Callable[..., Any]:
        def call(*args: Any, **kwargs: Any) -> Any:
            return self._rpc(name, args, kwargs)

        call.__name__ = name
        return call

    def _constant(self, name: str) -> Any:
        if self._constants is None:
            self._load_constants()
        assert self._constants is not None
        try:
            return self._constants[name]
        except KeyError:
            raise AttributeError(f"MetaTrader5 has no constant {name!r} (per the bridge)") from None

    def _load_constants(self) -> None:
        if self._constants is not None:
            return
        data = self._request("GET", "/v1/constants")
        self._constants = dict(data.get("constants") or {})

    # -- transport -----------------------------------------------------------
    def _rpc(self, fn: str, args: tuple, kwargs: dict) -> Any:
        try:
            body: dict[str, Any] = {"fn": fn, "args": [encode(a) for a in args],
                                    "kwargs": {k: encode(v) for k, v in kwargs.items()}}
        except CodecError as exc:
            raise MT5Unavailable(f"Can't send {fn}() to the MT5 bridge: {exc}") from exc
        is_order = fn == "order_send"
        if is_order:
            body["idem"] = uuid.uuid4().hex  # same key on every retry -> the bridge never executes it twice
        data = self._request("POST", "/v1/rpc", json=body, retry_budget=self.retries if is_order else 1,
                             unknown_outcome=is_order)
        result = decode(data.get("result"))
        if data.get("last_error") is not None:
            code, message = decode(data["last_error"])
            self._last_error = (int(code), str(message))
        return result

    def _request(self, method: str, path: str, json: dict | None = None, retry_budget: int = 1,
                 unknown_outcome: bool = False) -> dict[str, Any]:
        url = self.base_url + path
        last_problem = "unknown error"
        for attempt in range(retry_budget + 1):
            try:
                resp = self._http.request(method, url, json=json, headers=self._headers)
            except httpx.HTTPError as exc:
                last_problem = f"{type(exc).__name__}: {exc}"
                logger.warning("MT5 bridge %s %s failed (attempt %d): %s", method, path, attempt + 1, last_problem)
                continue
            if resp.status_code == 401:
                raise MT5Unavailable("The MT5 bridge rejected the API key (check MT5_BRIDGE_API_KEY on both sides).")
            if resp.status_code in _NO_ORDER_RETRY_STATUS:
                raise MT5Unavailable(f"MT5 bridge refused {path}: {_detail(resp)}")
            if resp.status_code == 503:
                # Bridge is up but its MT5 terminal isn't (closed, logged out...). Report it; for /v1/health the
                # caller turns it into initialize() == False.
                if path == "/v1/health":
                    try:
                        return resp.json()
                    except ValueError:
                        return {"ok": False, "error": _detail(resp)}
                raise MT5Unavailable(f"The MT5 terminal on the bridge machine isn't available: {_detail(resp)}")
            if resp.status_code >= 500:
                last_problem = f"HTTP {resp.status_code}: {_detail(resp)}"
                continue
            try:
                return resp.json()
            except ValueError as exc:
                raise MT5Unavailable(f"The MT5 bridge sent an unreadable reply to {path}.") from exc
        suffix = (" The order's outcome is UNKNOWN (it may have been placed): check open positions before retrying."
                  if unknown_outcome else "")
        raise MT5Unavailable(f"Can't reach the MT5 bridge at {self.base_url} ({last_problem}).{suffix}")


def _detail(resp: httpx.Response) -> str:
    try:
        data = resp.json()
        return str(data.get("error") or data.get("detail") or data)[:300]
    except ValueError:
        return resp.text[:300]


@lru_cache
def get_remote_mt5() -> RemoteMT5:
    """One shared remote 'module' for the whole process (MT5Client and the broker adapter both use it)."""
    s = get_settings()
    return RemoteMT5(s.mt5_bridge_url, s.mt5_bridge_api_key, timeout=s.mt5_bridge_timeout)
