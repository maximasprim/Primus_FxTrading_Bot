"""Telegram alert channel. Requires TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID."""
import httpx

from app.config import get_settings
from app.models.schemas import TradeSetup


async def send_telegram(message: str) -> dict:
    """Low-level sender — Telegram has a single fixed chat (TELEGRAM_CHAT_ID),
    so there's no per-user recipient to resolve, unlike WhatsApp/SMS/Email."""
    return await send_alert(message, setup=None)


async def send_alert(message: str, setup: TradeSetup | None = None) -> dict:
    settings = get_settings()
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        return {"sent": False, "reason": "TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not configured."}

    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.post(
            url, json={"chat_id": settings.telegram_chat_id, "text": message}
        )
        return {"sent": response.status_code == 200, "status_code": response.status_code}
