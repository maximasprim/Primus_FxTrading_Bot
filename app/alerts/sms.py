"""
SMS alert channel (notification-feature spec §4) via a Twilio-style REST API.

Used primarily for urgent/high-priority alerts (confirmed entries, auto-trade
execution, critical risk events) — see DEFAULT_CHANNELS_ON in
app/models/notification_schemas.py for exactly which events include SMS.

# TODO: recipient number should come from NotificationPreferences.sms_number,
# not a hardcoded placeholder — the caller (notification_service) is
# expected to pass it in via `to_number` once wired to real user config.
"""
import httpx

from app.config import get_settings


async def send_sms(message: str, to_number: str) -> dict:
    settings = get_settings()
    if not (settings.twilio_account_sid and settings.twilio_auth_token and settings.twilio_from_number):
        return {"sent": False, "reason": "Twilio credentials not configured."}
    if not to_number:
        return {"sent": False, "reason": "No SMS number configured for this user."}

    url = f"https://api.twilio.com/2010-04-01/Accounts/{settings.twilio_account_sid}/Messages.json"
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.post(
            url,
            auth=(settings.twilio_account_sid, settings.twilio_auth_token),
            data={"From": settings.twilio_from_number, "To": to_number, "Body": message},
        )
        success = response.status_code in (200, 201)
        provider_message_id = None
        if success:
            try:
                provider_message_id = response.json().get("sid")
            except ValueError:
                pass
        return {
            "sent": success,
            "status_code": response.status_code,
            "provider_message_id": provider_message_id,
            "reason": None if success else response.text[:300],
        }
