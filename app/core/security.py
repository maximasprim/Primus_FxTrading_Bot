"""
Security helpers (spec §26).

Webhook secret validation using constant-time comparison, plus a minimal
in-memory rate limiter suitable for single-instance deployments.
# TODO: replace the in-memory rate limiter with a Redis-backed one (using
# `app.config.get_settings().redis_url`) once running with more than one
# backend process/worker.
"""
import hmac
import time
from collections import defaultdict

from fastapi import HTTPException, status

from app.config import get_settings


def verify_webhook_secret(provided_secret: str) -> None:
    settings = get_settings()
    if not hmac.compare_digest(provided_secret, settings.tradingview_webhook_secret):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid webhook secret.")


class SimpleRateLimiter:
    def __init__(self, max_calls: int, period_seconds: float):
        self.max_calls = max_calls
        self.period_seconds = period_seconds
        self._calls: dict[str, list[float]] = defaultdict(list)

    def check(self, key: str) -> None:
        now = time.monotonic()
        window_start = now - self.period_seconds
        self._calls[key] = [t for t in self._calls[key] if t > window_start]

        if len(self._calls[key]) >= self.max_calls:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Rate limit exceeded.",
            )
        self._calls[key].append(now)


webhook_rate_limiter = SimpleRateLimiter(max_calls=60, period_seconds=60.0)
