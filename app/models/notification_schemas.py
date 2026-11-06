"""
Schemas for the Multi-Channel Notification System
(notification-feature spec §2, §6-9, §10-11).
"""
from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field


class NotificationChannel(str, Enum):
    WHATSAPP = "whatsapp"
    SMS = "sms"
    EMAIL = "email"
    TELEGRAM = "telegram"
    DASHBOARD = "dashboard"
    TRADINGVIEW = "tradingview"


class NotificationPriority(str, Enum):
    LOW = "LOW"        # market watch / setup forming
    MEDIUM = "MEDIUM"   # entry zone reached
    HIGH = "HIGH"       # confirmed entry, auto-trade executed, safety/system events


class NotificationEventType(str, Enum):
    """Spec §7 priority matrix rows."""

    SETUP_FORMING = "setup_forming"
    ENTRY_ZONE_REACHED = "entry_zone_reached"
    CONFIRMED_ENTRY = "confirmed_entry"
    AUTO_TRADE_EXECUTED = "auto_trade_executed"
    AUTO_TRADE_FAILED = "auto_trade_failed"
    TRADE_CLOSED = "trade_closed"
    DAILY_LOSS_LIMIT_REACHED = "daily_loss_limit_reached"
    AUTO_TRADING_ENABLED = "auto_trading_enabled"
    AUTO_TRADING_DISABLED = "auto_trading_disabled"
    AUTO_TRADING_PAUSED = "auto_trading_paused"
    BROKER_CONNECTION_FAILURE = "broker_connection_failure"
    SYSTEM_ERROR = "system_error"
    DAILY_SUMMARY = "daily_summary"
    WEEKLY_REPORT = "weekly_report"
    TRADE_APPROVAL_REQUIRED = "trade_approval_required"


# Spec §7 default priority matrix: which priority each event type carries,
# used to pick the default channel set before any user override is applied.
DEFAULT_EVENT_PRIORITY: dict[NotificationEventType, NotificationPriority] = {
    NotificationEventType.SETUP_FORMING: NotificationPriority.LOW,
    NotificationEventType.ENTRY_ZONE_REACHED: NotificationPriority.MEDIUM,
    NotificationEventType.CONFIRMED_ENTRY: NotificationPriority.HIGH,
    NotificationEventType.AUTO_TRADE_EXECUTED: NotificationPriority.HIGH,
    NotificationEventType.AUTO_TRADE_FAILED: NotificationPriority.HIGH,
    NotificationEventType.TRADE_CLOSED: NotificationPriority.MEDIUM,
    NotificationEventType.DAILY_LOSS_LIMIT_REACHED: NotificationPriority.HIGH,
    NotificationEventType.AUTO_TRADING_ENABLED: NotificationPriority.HIGH,
    NotificationEventType.AUTO_TRADING_DISABLED: NotificationPriority.HIGH,
    NotificationEventType.AUTO_TRADING_PAUSED: NotificationPriority.HIGH,
    NotificationEventType.BROKER_CONNECTION_FAILURE: NotificationPriority.HIGH,
    NotificationEventType.SYSTEM_ERROR: NotificationPriority.HIGH,
    NotificationEventType.DAILY_SUMMARY: NotificationPriority.LOW,
    NotificationEventType.WEEKLY_REPORT: NotificationPriority.LOW,
    NotificationEventType.TRADE_APPROVAL_REQUIRED: NotificationPriority.HIGH,
}

# Spec §7 default channel matrix (Yes/Optional/No per event x channel).
# "Optional" defaults to OFF here - the user must opt in - matching the
# spec's own examples (e.g. setup forming: WhatsApp OFF, Email ON in §6).
DEFAULT_CHANNELS_ON: dict[NotificationEventType, set[NotificationChannel]] = {
    NotificationEventType.SETUP_FORMING: {NotificationChannel.DASHBOARD, NotificationChannel.TRADINGVIEW, NotificationChannel.EMAIL},
    NotificationEventType.ENTRY_ZONE_REACHED: {
        NotificationChannel.WHATSAPP, NotificationChannel.EMAIL, NotificationChannel.DASHBOARD, NotificationChannel.TRADINGVIEW,
    },
    NotificationEventType.CONFIRMED_ENTRY: {
        NotificationChannel.WHATSAPP, NotificationChannel.SMS, NotificationChannel.EMAIL,
        NotificationChannel.DASHBOARD, NotificationChannel.TRADINGVIEW,
    },
    NotificationEventType.AUTO_TRADE_EXECUTED: {
        NotificationChannel.WHATSAPP, NotificationChannel.SMS, NotificationChannel.EMAIL, NotificationChannel.DASHBOARD,
    },
    NotificationEventType.AUTO_TRADE_FAILED: {
        NotificationChannel.WHATSAPP, NotificationChannel.SMS, NotificationChannel.EMAIL, NotificationChannel.DASHBOARD,
    },
    NotificationEventType.TRADE_CLOSED: {NotificationChannel.EMAIL, NotificationChannel.DASHBOARD},
    NotificationEventType.DAILY_LOSS_LIMIT_REACHED: {
        NotificationChannel.WHATSAPP, NotificationChannel.SMS, NotificationChannel.EMAIL,
    },
    NotificationEventType.AUTO_TRADING_ENABLED: {NotificationChannel.WHATSAPP, NotificationChannel.EMAIL},
    NotificationEventType.AUTO_TRADING_DISABLED: {
        NotificationChannel.WHATSAPP, NotificationChannel.SMS, NotificationChannel.EMAIL,
    },
    NotificationEventType.AUTO_TRADING_PAUSED: {NotificationChannel.WHATSAPP, NotificationChannel.EMAIL},
    NotificationEventType.BROKER_CONNECTION_FAILURE: {
        NotificationChannel.WHATSAPP, NotificationChannel.SMS, NotificationChannel.EMAIL,
    },
    NotificationEventType.SYSTEM_ERROR: {
        NotificationChannel.WHATSAPP, NotificationChannel.SMS, NotificationChannel.EMAIL,
    },
    NotificationEventType.DAILY_SUMMARY: {NotificationChannel.EMAIL},
    NotificationEventType.WEEKLY_REPORT: {NotificationChannel.EMAIL},
    NotificationEventType.TRADE_APPROVAL_REQUIRED: {NotificationChannel.WHATSAPP, NotificationChannel.EMAIL},
}


class NotificationPreferences(BaseModel):
    """Spec §6 - user-configurable per-event channel toggles + contact info."""

    whatsapp_number: str | None = None
    sms_number: str | None = None
    email: str | None = None

    # event_type -> set of channels the user wants ON. If an event isn't
    # present here, DEFAULT_CHANNELS_ON is used instead.
    overrides: dict[str, list[NotificationChannel]] = Field(default_factory=dict)

    def channels_for(self, event_type: NotificationEventType) -> set[NotificationChannel]:
        if event_type.value in self.overrides:
            return set(self.overrides[event_type.value])
        return DEFAULT_CHANNELS_ON.get(event_type, set())


class DeliveryStatus(str, Enum):
    """Spec §9 delivery lifecycle."""

    CREATED = "CREATED"
    SENT_TO_PROVIDER = "SENT_TO_PROVIDER"
    PROVIDER_ACCEPTED = "PROVIDER_ACCEPTED"
    DELIVERED = "DELIVERED"
    FAILED = "FAILED"
    READ = "READ"


class DeliveryAttempt(BaseModel):
    channel: NotificationChannel
    status: DeliveryStatus
    provider_message_id: str | None = None
    failure_reason: str | None = None
    attempted_at: datetime = Field(default_factory=datetime.utcnow)


class NotificationDispatchResult(BaseModel):
    event_type: NotificationEventType
    priority: NotificationPriority
    attempts: list[DeliveryAttempt] = Field(default_factory=list)
    fallback_triggered: bool = False


class ApprovalCommand(BaseModel):
    """Parsed result of a text command like 'APPROVE EURUSD SIGNAL-12345'
    (notification-feature spec §10)."""

    action: str  # "APPROVE" | "REJECT"
    instrument: str
    signal_id: str
