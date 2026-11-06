"""
API authentication.

Until now only the TradingView webhook was protected; every other route
(including trade placement and the trading-mode switches) was open to anyone
who could reach the server. `require_api_key` closes that: the dashboard sends
`X-API-Key: <API_SECRET_KEY>` and the server compares it in constant time.

Deliberately NOT covered (they have their own verification):
  * POST /api/tradingview/webhook  -- shared secret in the payload
  * /api/notifications/*/inbound   -- Meta / Twilio HMAC signatures
  * /, /health                     -- liveness only
"""
import hmac
import logging

from fastapi import Header, HTTPException, Request, WebSocket, status

from app.config import get_settings

logger = logging.getLogger(__name__)

_WEAK_KEYS = {"", "change-me", "changeme", "secret", "password"}


def _matches(provided: str | None, expected: str) -> bool:
    if not provided:
        return False
    return hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))


def api_key_is_weak() -> bool:
    key = get_settings().api_secret_key
    return key.strip().lower() in _WEAK_KEYS or len(key) < 16


async def require_api_key(request: Request, x_api_key: str | None = Header(default=None)) -> None:
    settings = get_settings()
    if not settings.api_auth_enabled:
        return
    # Signature-verified provider callbacks can't send our header.
    if request.url.path.endswith("/inbound"):
        return
    if not _matches(x_api_key, settings.api_secret_key):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid API key.",
            headers={"WWW-Authenticate": "ApiKey"},
        )


async def authorize_websocket(websocket: WebSocket) -> bool:
    """Browsers can't set headers on WebSockets, so the key rides in `?token=`."""
    settings = get_settings()
    if not settings.api_auth_enabled:
        return True
    if _matches(websocket.query_params.get("token"), settings.api_secret_key):
        return True
    await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
    return False


def warn_if_weak_key() -> None:
    settings = get_settings()
    if settings.api_auth_enabled and api_key_is_weak():
        logger.warning(
            "API_SECRET_KEY is weak or still the default. Use a long random value "
            "(e.g. `python -c \"import secrets; print(secrets.token_urlsafe(32))\"`)."
        )
