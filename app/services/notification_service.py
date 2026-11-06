"""
Notification Engine (notification-feature spec §8-9).

The single place every trading event routes through to actually reach the
user. Responsibilities (spec §8):
  1. Receive a trading event
  2. Determine its priority
  3. Read user preferences
  4. Generate the appropriate message format (caller-supplied per channel)
  5. Send through enabled channels
  6. Track delivery status
  7. Retry failed notifications where appropriate
  8. Log all delivery attempts

Fallback chain (spec §9): if a HIGH priority WhatsApp send fails (after
retries), fall back to SMS, then Email.
"""
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.alerts.dashboard_ws import broadcast_notification
from app.alerts.email import send_email
from app.alerts.sms import send_sms
from app.alerts.telegram import send_telegram
from app.alerts.whatsapp import send_whatsapp
from app.config import get_settings
from app.core.notification_preferences import notification_preferences_manager
from app.models.db_models import NotificationLog
from app.models.notification_schemas import (
    DeliveryAttempt,
    DeliveryStatus,
    NotificationChannel,
    NotificationDispatchResult,
    NotificationEventType,
)

# Fallback order for HIGH-priority alerts when the primary channel fails
# (spec §9 example: WhatsApp -> SMS -> Email).
_FALLBACK_CHAIN: dict[NotificationChannel, NotificationChannel] = {
    NotificationChannel.WHATSAPP: NotificationChannel.SMS,
    NotificationChannel.SMS: NotificationChannel.EMAIL,
}


async def _send_via_channel(channel: NotificationChannel, message: str, subject: str) -> dict:
    prefs = notification_preferences_manager.preferences
    if channel == NotificationChannel.WHATSAPP:
        return await send_whatsapp(message, prefs.whatsapp_number or "")
    if channel == NotificationChannel.SMS:
        return await send_sms(message, prefs.sms_number or "")
    if channel == NotificationChannel.EMAIL:
        return await send_email(message, subject, prefs.email)
    if channel == NotificationChannel.TELEGRAM:
        return await send_telegram(message)
    if channel == NotificationChannel.DASHBOARD:
        return await broadcast_notification(message, "notification")
    if channel == NotificationChannel.TRADINGVIEW:
        # TradingView is the charting/visualization surface, not a push
        # target - its alerts are handled on-chart via the Pine Script,
        # not pushed from this backend.
        return {"sent": True, "reason": "TradingView alerts are handled on-chart, not pushed from here."}
    return {"sent": False, "reason": f"Unknown channel: {channel}"}


async def _log_attempt(
    db: AsyncSession | None,
    event_type: NotificationEventType,
    priority: str,
    attempt: DeliveryAttempt,
    related_signal_id: str | None,
    fallback_from: NotificationChannel | None,
) -> None:
    if db is None:
        return  # allow calling without a DB session (e.g. in tests / dry runs)
    entry = NotificationLog(
        event_type=event_type.value,
        priority=priority,
        channel=attempt.channel.value,
        related_signal_id=related_signal_id,
        status=attempt.status.value,
        provider_message_id=attempt.provider_message_id,
        failure_reason=attempt.failure_reason,
        fallback_from_channel=fallback_from.value if fallback_from else None,
    )
    if attempt.status == DeliveryStatus.PROVIDER_ACCEPTED:
        entry.sent_at = datetime.utcnow()
    elif attempt.status == DeliveryStatus.FAILED:
        entry.failed_at = datetime.utcnow()
    db.add(entry)
    await db.commit()


async def _attempt_with_retries(
    channel: NotificationChannel, message: str, subject: str
) -> DeliveryAttempt:
    settings = get_settings()
    last_reason = None

    for _attempt_num in range(settings.notification_max_retries + 1):
        result = await _send_via_channel(channel, message, subject)
        if result.get("sent"):
            return DeliveryAttempt(
                channel=channel,
                status=DeliveryStatus.PROVIDER_ACCEPTED,
                provider_message_id=result.get("provider_message_id"),
            )
        last_reason = result.get("reason", "Unknown failure")

    return DeliveryAttempt(channel=channel, status=DeliveryStatus.FAILED, failure_reason=last_reason)


async def send_notification(
    event_type: NotificationEventType,
    message: str,
    subject: str | None = None,
    related_signal_id: str | None = None,
    db: AsyncSession | None = None,
    channels_override: set[NotificationChannel] | None = None,
) -> NotificationDispatchResult:
    """Main entry point. Determines priority + channels from preferences
    (unless explicitly overridden), sends through each, retries on failure,
    and falls back to the next channel in the chain for HIGH priority events.
    """
    settings = get_settings()
    priority = notification_preferences_manager.priority_for(event_type)
    channels = channels_override or notification_preferences_manager.channels_for(event_type)
    subject = subject or f"[{priority.value}] {event_type.value.replace('_', ' ').title()}"

    result = NotificationDispatchResult(event_type=event_type, priority=priority)

    for channel in channels:
        attempt = await _attempt_with_retries(channel, message, subject)
        result.attempts.append(attempt)
        await _log_attempt(db, event_type, priority.value, attempt, related_signal_id, fallback_from=None)

        if (
            attempt.status == DeliveryStatus.FAILED
            and priority.value == "HIGH"
            and settings.notification_fallback_enabled
            and channel in _FALLBACK_CHAIN
        ):
            fallback_channel = _FALLBACK_CHAIN[channel]
            if fallback_channel not in channels:  # don't double-send if already scheduled
                fallback_attempt = await _attempt_with_retries(fallback_channel, message, subject)
                result.attempts.append(fallback_attempt)
                result.fallback_triggered = True
                await _log_attempt(
                    db, event_type, priority.value, fallback_attempt, related_signal_id, fallback_from=channel
                )

    return result
