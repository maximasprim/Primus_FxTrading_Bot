"""
Duplicate Trade Protection (execution-feature spec Sec.13).

Generates a deterministic-ish unique signal ID per setup, and tracks which
signal IDs have already been processed/executed so retried webhooks, duplicate
alerts, or system retries can never result in the same trade twice.

Backed by Redis (SETNX-style, via `redis.asyncio`) when `REDIS_URL` is
configured and reachable, so this is safe across multiple backend
workers/processes and survives restarts. Falls back to an in-memory cache
automatically if Redis isn't configured or isn't reachable -- correct for a
single-process deployment, with a logged warning so the degradation is visible.
"""
import logging
import time
from dataclasses import dataclass

from app.config import get_settings
from app.models.schemas import TradeSetup

logger = logging.getLogger(__name__)


def generate_signal_id(setup: TradeSetup) -> str:
    """e.g. EURUSD-20260727-5M-LIQUIDITY-MSS-001"""
    symbol = setup.instrument.replace("/", "")
    date_part = setup.generated_at.strftime("%Y%m%d")
    timeframe = setup.timeframe.value.replace("_monthly", "")
    strategy_tag = "-".join(
        part.strip().upper().replace(" ", "")[:12]
        for part in setup.setup_type.split("+")
    )
    return f"{symbol}-{date_part}-{timeframe}-{strategy_tag}"


@dataclass
class ProcessedSignal:
    signal_id: str
    processed_at: float
    executed: bool


class DuplicateTradeGuard:
    """Redis-backed when available; in-memory fallback otherwise. Callers use
    the same async interface either way, so nothing upstream needs to know
    which backend is active.
    """

    _REDIS_KEY_PREFIX = "forex_ai:duplicate_guard:"

    def __init__(self, ttl_seconds: float = 24 * 60 * 60, redis_client=None):
        self.ttl_seconds = ttl_seconds
        self._seen: dict[str, ProcessedSignal] = {}  # in-memory fallback store
        self._redis = redis_client  # injectable for testing (e.g. fakeredis)
        self._redis_checked = redis_client is not None

    async def _get_redis(self):
        """Lazily connects to Redis on first use; caches the (un)availability
        decision so we don't retry a broken connection on every call."""
        if self._redis_checked:
            return self._redis

        self._redis_checked = True
        settings = get_settings()
        if not settings.redis_url:
            return None
        try:
            import redis.asyncio as aioredis

            client = aioredis.from_url(settings.redis_url, decode_responses=True)
            await client.ping()
            self._redis = client
            return client
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Redis unavailable (%s) -- duplicate-trade protection is falling back to "
                "in-memory storage. This is fine for a single backend process; it is NOT "
                "safe across multiple worker processes.",
                exc,
            )
            return None

    def _purge_expired_memory(self) -> None:
        now = time.time()
        expired = [sid for sid, rec in self._seen.items() if now - rec.processed_at > self.ttl_seconds]
        for sid in expired:
            del self._seen[sid]

    async def is_duplicate(self, signal_id: str) -> bool:
        redis_client = await self._get_redis()
        if redis_client is not None:
            return bool(await redis_client.exists(self._REDIS_KEY_PREFIX + signal_id))

        self._purge_expired_memory()
        return signal_id in self._seen

    async def mark_processed(self, signal_id: str, executed: bool) -> None:
        redis_client = await self._get_redis()
        if redis_client is not None:
            await redis_client.set(
                self._REDIS_KEY_PREFIX + signal_id, "1" if executed else "0", ex=int(self.ttl_seconds)
            )
            return

        self._seen[signal_id] = ProcessedSignal(signal_id, time.time(), executed)

    def has_active_trade_for_instrument(self, instrument: str, open_signal_ids: list[str]) -> bool:
        """Spec Sec.13: 'Is another trade already active for this setup?' --
        callers pass in the currently-open trade journal signal IDs for the
        instrument (from trade_journal_service) to check against. This one
        stays synchronous since it operates on a list already fetched from
        the DB by the caller, not on the guard's own storage.
        """
        return any(sid.startswith(instrument.replace("/", "")) for sid in open_signal_ids)


duplicate_guard = DuplicateTradeGuard()
