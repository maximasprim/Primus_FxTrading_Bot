"""Tests for app/services/notification_service.py (notification-feature spec §7-9).

No credentials are configured in the test environment, so every channel
genuinely fails closed (rather than making a real network call) - this lets
us exercise the retry + fallback logic deterministically without mocking.
"""
import pytest

from app.models.notification_schemas import (
    DEFAULT_CHANNELS_ON,
    DEFAULT_EVENT_PRIORITY,
    NotificationChannel,
    NotificationEventType,
    NotificationPriority,
)
from app.services.notification_service import send_notification


def test_priority_matrix_matches_spec():
    """Spec §7 table: A+ confirmed entry / auto-trade / safety events are HIGH;
    setup forming is LOW."""
    assert DEFAULT_EVENT_PRIORITY[NotificationEventType.SETUP_FORMING] == NotificationPriority.LOW
    assert DEFAULT_EVENT_PRIORITY[NotificationEventType.CONFIRMED_ENTRY] == NotificationPriority.HIGH
    assert DEFAULT_EVENT_PRIORITY[NotificationEventType.AUTO_TRADE_EXECUTED] == NotificationPriority.HIGH
    assert DEFAULT_EVENT_PRIORITY[NotificationEventType.DAILY_LOSS_LIMIT_REACHED] == NotificationPriority.HIGH
    assert DEFAULT_EVENT_PRIORITY[NotificationEventType.WEEKLY_REPORT] == NotificationPriority.LOW


def test_confirmed_entry_includes_all_high_priority_channels():
    """Spec §2: HIGH priority confirmed entries should reach WhatsApp, SMS, and Email."""
    channels = DEFAULT_CHANNELS_ON[NotificationEventType.CONFIRMED_ENTRY]
    assert NotificationChannel.WHATSAPP in channels
    assert NotificationChannel.SMS in channels
    assert NotificationChannel.EMAIL in channels


def test_setup_forming_does_not_default_to_sms():
    """Spec §2/§7: low-priority setup-forming alerts should NOT default to SMS."""
    channels = DEFAULT_CHANNELS_ON[NotificationEventType.SETUP_FORMING]
    assert NotificationChannel.SMS not in channels


@pytest.mark.asyncio
async def test_fallback_triggers_for_high_priority_event():
    """WhatsApp fails (no credentials) on a HIGH priority event -> should fall
    back to SMS automatically (spec §9)."""
    result = await send_notification(
        event_type=NotificationEventType.CONFIRMED_ENTRY,
        message="test",
        channels_override={NotificationChannel.WHATSAPP},
        db=None,
    )
    assert result.priority == NotificationPriority.HIGH
    assert result.fallback_triggered is True
    channels_attempted = [a.channel for a in result.attempts]
    assert NotificationChannel.WHATSAPP in channels_attempted
    assert NotificationChannel.SMS in channels_attempted


@pytest.mark.asyncio
async def test_no_fallback_for_low_priority_event():
    """The same channel failure on a LOW priority event should NOT trigger fallback."""
    result = await send_notification(
        event_type=NotificationEventType.SETUP_FORMING,
        message="test",
        channels_override={NotificationChannel.WHATSAPP},
        db=None,
    )
    assert result.priority == NotificationPriority.LOW
    assert result.fallback_triggered is False
    channels_attempted = [a.channel for a in result.attempts]
    assert channels_attempted == [NotificationChannel.WHATSAPP]


@pytest.mark.asyncio
async def test_no_double_fallback_if_target_channel_already_scheduled():
    """If both WhatsApp and SMS are already in the channel set and WhatsApp
    fails, SMS should only be attempted once (not sent twice just because
    it's also WhatsApp's fallback target). Note: since SMS itself also fails
    here (no Twilio creds configured) and SMS has its own fallback target
    (Email), a *separate*, legitimate fallback to Email is still expected -
    this test only asserts SMS isn't double-sent because of WhatsApp's failure.
    """
    result = await send_notification(
        event_type=NotificationEventType.CONFIRMED_ENTRY,
        message="test",
        channels_override={NotificationChannel.WHATSAPP, NotificationChannel.SMS},
        db=None,
    )
    sms_attempts = [a for a in result.attempts if a.channel == NotificationChannel.SMS]
    assert len(sms_attempts) == 1
    # Email shows up as SMS's own fallback (SMS -> Email), not WhatsApp's
    # fallback (which would have been SMS, already scheduled and skipped).
    email_attempts = [a for a in result.attempts if a.channel == NotificationChannel.EMAIL]
    assert len(email_attempts) == 1
