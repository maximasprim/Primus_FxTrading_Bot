"""
Notification Preferences Manager (notification-feature spec §6-7).

Single source of truth for "which channels should this event go out on."
Starts from the spec's default priority/channel matrix and lets the user
override per event type. Mirrors the pattern used by
app/core/trading_mode.py.
"""
from app.models.notification_schemas import (
    DEFAULT_EVENT_PRIORITY,
    NotificationChannel,
    NotificationEventType,
    NotificationPreferences,
    NotificationPriority,
)


class NotificationPreferencesManager:
    """In-memory manager suitable for a single-user / single-process setup.
    # TODO: back with a DB table (one row per user) once this supports
    # multiple users - currently there is one global preferences object.
    """

    def __init__(self):
        self._preferences = NotificationPreferences()

    @property
    def preferences(self) -> NotificationPreferences:
        return self._preferences

    def set_contact_info(
        self, whatsapp_number: str | None = None, sms_number: str | None = None, email: str | None = None
    ) -> None:
        if whatsapp_number is not None:
            self._preferences.whatsapp_number = whatsapp_number
        if sms_number is not None:
            self._preferences.sms_number = sms_number
        if email is not None:
            self._preferences.email = email

    def set_channels_for_event(self, event_type: NotificationEventType, channels: list[NotificationChannel]) -> None:
        self._preferences.overrides[event_type.value] = channels

    def channels_for(self, event_type: NotificationEventType) -> set[NotificationChannel]:
        return self._preferences.channels_for(event_type)

    def priority_for(self, event_type: NotificationEventType) -> NotificationPriority:
        return DEFAULT_EVENT_PRIORITY.get(event_type, NotificationPriority.MEDIUM)


notification_preferences_manager = NotificationPreferencesManager()
