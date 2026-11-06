"""WhatsApp alert channel via Meta's WhatsApp Cloud API.
Requires WHATSAPP_API_TOKEN + WHATSAPP_PHONE_NUMBER_ID.
"""
import httpx

from app.config import get_settings
from app.models.schemas import TradeSetup


async def send_whatsapp(message: str, to_number: str) -> dict:
    """Low-level sender used directly by the notification engine
    (app/services/notification_service.py), which resolves `to_number`
    from NotificationPreferences.whatsapp_number."""
    settings = get_settings()
    if not settings.whatsapp_api_token or not settings.whatsapp_phone_number_id:
        return {"sent": False, "reason": "WHATSAPP_API_TOKEN / WHATSAPP_PHONE_NUMBER_ID not configured."}
    if not to_number:
        return {"sent": False, "reason": "No WhatsApp number configured for this user."}

    url = f"https://graph.facebook.com/v19.0/{settings.whatsapp_phone_number_id}/messages"
    headers = {"Authorization": f"Bearer {settings.whatsapp_api_token}"}
    body = {
        "messaging_product": "whatsapp",
        "to": to_number,
        "type": "text",
        "text": {"body": message},
    }
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.post(url, headers=headers, json=body)
        provider_message_id = None
        if response.status_code == 200:
            try:
                provider_message_id = response.json().get("messages", [{}])[0].get("id")
            except (ValueError, IndexError, KeyError):
                pass
        return {
            "sent": response.status_code == 200,
            "status_code": response.status_code,
            "provider_message_id": provider_message_id,
            "reason": None if response.status_code == 200 else response.text[:300],
        }


async def send_alert(message: str, setup: TradeSetup) -> dict:
    """Back-compat wrapper for app/services/alert_service.py's TradeSetup-based
    dispatch - resolves the recipient from stored preferences."""
    from app.core.notification_preferences import notification_preferences_manager

    to_number = notification_preferences_manager.preferences.whatsapp_number or ""
    return await send_whatsapp(message, to_number)
