"""
Shared, serialised access to the `MetaTrader5` Python package.

Why this exists
---------------
* The MetaTrader5 package talks to ONE desktop terminal through a single
  connection and is not safe to call from several threads at once. Every call
  therefore runs on one dedicated worker thread (`ThreadPoolExecutor(1)`), so
  the scalper loop, the market-data provider and the dashboard can all share it.
* It only works on Windows, with the MT5 desktop terminal installed and
  logged in on the SAME machine. `import MetaTrader5` fails elsewhere; we turn
  that into a clear `MT5Unavailable` error instead of a stack trace.
* MT5 timestamps are *broker server time* labelled as if they were UTC. The
  server offset is estimated from a fresh tick (see `server_offset_seconds`).

Note on mobile MT5: the phone app has no API. It can't run a bot. It will
simply *show* the trades this desktop-terminal bot places on the same account.
"""
from __future__ import annotations

import asyncio
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from typing import Any

from app.config import get_settings

logger = logging.getLogger(__name__)


class MT5Unavailable(RuntimeError):
    """MetaTrader5 can't be used (wrong OS, terminal not running, bad login...)."""


class MT5Client:
    def __init__(
        self,
        login: int = 0,
        password: str = "",
        server: str = "",
        path: str = "",
        mt5_module: Any | None = None,
    ) -> None:
        self.login = login
        self.password = password
        self.server = server
        self.path = path
        self._mt5 = mt5_module  # injectable for tests
        self._connected = False
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mt5")
        self._connect_lock = asyncio.Lock()
        self._server_offset = 0
        self._offset_updated_at = 0.0

    # -- connection ---------------------------------------------------
    def _connect_sync(self) -> None:
        if self._mt5 is None:
            try:
                import MetaTrader5 as mt5  # type: ignore[import-not-found]
            except ImportError as exc:
                raise MT5Unavailable(
                    "The MetaTrader5 Python package isn't available. It only installs on Windows "
                    "(`pip install MetaTrader5`) and needs the MT5 desktop terminal on the same machine."
                ) from exc
            self._mt5 = mt5
        kwargs: dict[str, Any] = {}
        if self.path:
            kwargs["path"] = self.path
        if self.login:
            kwargs.update(login=self.login, password=self.password, server=self.server)
        if not self._mt5.initialize(**kwargs):
            raise MT5Unavailable(
                f"MT5 initialize() failed: {self._mt5.last_error()}. Is the MT5 terminal open and logged in?"
            )
        self._connected = True

    async def ensure_connected(self) -> None:
        if self._connected:
            return
        async with self._connect_lock:
            if self._connected:
                return
            await asyncio.get_running_loop().run_in_executor(self._executor, self._connect_sync)

    def mark_disconnected(self) -> None:
        """Force a fresh initialize() on the next call (e.g. after repeated failures)."""
        self._connected = False

    # -- calls --------------------------------------------------------
    async def call(self, name: str, *args: Any, **kwargs: Any) -> Any:
        """Run `MetaTrader5.<name>(*args, **kwargs)` on the serialised worker thread."""
        await self.ensure_connected()
        mt5 = self._mt5

        def _run() -> Any:
            return getattr(mt5, name)(*args, **kwargs)

        return await asyncio.get_running_loop().run_in_executor(self._executor, _run)

    def const(self, name: str) -> Any:
        """A MetaTrader5 constant, e.g. const('ORDER_TYPE_BUY'). Requires a prior connect."""
        if self._mt5 is None:
            raise MT5Unavailable("MT5 isn't connected yet.")
        return getattr(self._mt5, name)

    async def last_error(self) -> Any:
        try:
            return await self.call("last_error")
        except Exception as exc:  # noqa: BLE001
            return str(exc)

    # -- time ---------------------------------------------------------
    async def server_offset_seconds(self, symbol: str) -> int:
        """Broker-server-time minus UTC, in seconds (e.g. 7200 for UTC+2).

        Estimated from a fresh tick: raw = tick.time - now. If raw is within two
        minutes of a half-hour multiple we trust it; otherwise (stale tick over a
        weekend, say) the previous estimate is kept. Cached for 10 minutes.
        """
        now = time.time()
        if now - self._offset_updated_at < 600:
            return self._server_offset
        try:
            tick = await self.call("symbol_info_tick", symbol)
        except Exception:  # noqa: BLE001
            return self._server_offset
        if tick is not None:
            raw = float(tick.time) - now
            snapped = round(raw / 1800) * 1800
            if abs(raw - snapped) <= 120 and abs(snapped) <= 14 * 3600:
                self._server_offset = int(snapped)
                self._offset_updated_at = now
        return self._server_offset

    def shutdown(self) -> None:
        try:
            if self._mt5 is not None and self._connected:
                self._mt5.shutdown()
        finally:
            self._connected = False
            self._executor.shutdown(wait=False)


@lru_cache
def get_mt5_client() -> MT5Client:
    s = get_settings()
    return MT5Client(login=s.mt5_login, password=s.mt5_password, server=s.mt5_server, path=s.mt5_terminal_path)
