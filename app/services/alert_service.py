"""
Alert dispatch service (spec Sec.13, Sec.22; execution-feature spec Sec.17).

Renders the three-stage alert message formats from spec Sec.13, plus the
automatic-execution success/failure formats from execution-feature spec
Sec.17, and dispatches both through the central notification engine
(app/services/notification_service.py) -- this is what closes the "two
parallel alert paths" gap flagged in PHASES.md: every TradeSetup alert now
gets the same retry/fallback/delivery-logging treatment as everything else
routed through send_notification.

Grade-based channel selection (spec Sec.22's example: A+ -> TradingView +
Telegram + WhatsApp + Dashboard, B -> Dashboard only) is preserved as an
explicit channels_override passed into the notification engine, rather than
falling back to the engine's own event-type-priority defaults -- so existing
grade-based behavior doesn't silently change.
"""
from app.models.execution_schemas import ExecutionResult
from app.models.notification_schemas import NotificationChannel, NotificationEventType
from app.models.schemas import SetupGrade, SetupStatus, TradeSetup
from app.services.notification_service import send_notification

# Spec Sec.22 example routing config -- make this user-configurable via settings/DB later.
_CHANNEL_ROUTING: dict[SetupGrade, set[NotificationChannel]] = {
    SetupGrade.A_PLUS: {
        NotificationChannel.TRADINGVIEW, NotificationChannel.TELEGRAM,
        NotificationChannel.WHATSAPP, NotificationChannel.DASHBOARD,
    },
    SetupGrade.A: {NotificationChannel.TRADINGVIEW, NotificationChannel.TELEGRAM, NotificationChannel.DASHBOARD},
    SetupGrade.B: {NotificationChannel.DASHBOARD},
    SetupGrade.NO_TRADE: set(),
}

_STATUS_TO_EVENT_TYPE: dict[SetupStatus, NotificationEventType] = {
    SetupStatus.SETUP_FORMING: NotificationEventType.SETUP_FORMING,
    SetupStatus.ENTRY_ZONE_REACHED: NotificationEventType.ENTRY_ZONE_REACHED,
    SetupStatus.CONFIRMED: NotificationEventType.CONFIRMED_ENTRY,
}


def render_alert_message(setup: TradeSetup) -> str:
    if setup.status == SetupStatus.SETUP_FORMING:
        header = "\u26a0\ufe0f SETUP FORMING"
    elif setup.status == SetupStatus.ENTRY_ZONE_REACHED:
        header = "\U0001f3af ENTRY ZONE REACHED"
    elif setup.status == SetupStatus.CONFIRMED:
        header = "\U0001f6a8 HIGH-CONFLUENCE ENTRY ALERT"
    else:
        return f"{setup.instrument}: {setup.status.value}"

    lines = [
        header,
        "",
        f"Pair: {setup.instrument}",
        f"Direction: {setup.direction.value}",
        f"Score: {setup.confluence_score}/100  (Grade: {setup.grade.value})",
    ]
    if setup.entry_zone:
        lines.append(f"Entry Zone: {setup.entry_zone.min} - {setup.entry_zone.max}")
    if setup.stop_loss:
        lines.append(f"Stop Loss: {setup.stop_loss}")
    if setup.take_profit_1:
        lines.append(f"Take Profit 1: {setup.take_profit_1}")
    if setup.take_profit_2:
        lines.append(f"Take Profit 2: {setup.take_profit_2}")
    if setup.risk_reward:
        lines.append(f"Risk-to-Reward: 1:{setup.risk_reward}")

    lines += ["", setup.reasoning, "", f"Invalidation: {setup.scenario.invalidation_condition}",
              "", "Risk Reminder: Use predefined risk management. This is not a guaranteed outcome."]
    return "\n".join(lines)


async def dispatch_alert(setup: TradeSetup, db=None) -> dict:
    channels = _CHANNEL_ROUTING.get(setup.grade, set())
    event_type = _STATUS_TO_EVENT_TYPE.get(setup.status)

    if not channels or event_type is None:
        return {"dispatched": False, "channels": [], "reason": "Below alert threshold or invalidated."}

    message = render_alert_message(setup)
    result = await send_notification(
        event_type=event_type,
        message=message,
        subject=f"[{setup.grade.value}] {setup.instrument} {setup.direction.value} setup",
        related_signal_id=None,
        db=db,
        channels_override=channels,
    )
    return {
        "dispatched": True,
        "channels": [c.value for c in channels],
        "message": message,
        "priority": result.priority.value,
        "fallback_triggered": result.fallback_triggered,
        "attempts": [a.model_dump(mode="json") for a in result.attempts],
    }


def render_execution_alert(setup: TradeSetup, result: ExecutionResult) -> str:
    """Spec Sec.17 -- success and failure formats for automatic execution."""
    if result.executed:
        lines = [
            "\U0001f916 AUTOMATIC TRADE EXECUTED",
            "",
            f"Instrument: {setup.instrument}",
            f"Direction: {setup.direction.value}",
            "",
            f"Entry: {setup.entry_zone.min}-{setup.entry_zone.max}" if setup.entry_zone else "",
            f"Stop Loss: {setup.stop_loss}",
            f"Take Profit: {setup.take_profit_1}",
            "",
            f"Risk-to-Reward: 1:{setup.risk_reward}",
            f"Confluence Score: {setup.confluence_score}/100",
            "",
            "Execution Status:",
            "SUCCESS",
            "",
            "Broker Order ID:",
            result.broker_order_id or "N/A",
        ]
    else:
        reason = result.failure.reason if result.failure else "Unknown reason"
        lines = [
            "\u26a0\ufe0f AUTOMATIC TRADE FAILED",
            "",
            f"Instrument: {setup.instrument}",
            f"Direction: {setup.direction.value}",
            "",
            "Reason:",
            reason,
            "",
            "Action:",
            "No trade opened.",
        ]
    return "\n".join(line for line in lines if line is not None)


async def dispatch_execution_alert(setup: TradeSetup, result: ExecutionResult, db=None) -> dict:
    """Execution outcomes are always HIGH priority (notification-feature spec
    Sec.7) -- routed through the notification engine's default AUTO_TRADE_EXECUTED
    / AUTO_TRADE_FAILED channel set (WhatsApp + SMS + Email + Dashboard),
    which also means a failed WhatsApp send here automatically falls back to
    SMS then Email per the engine's fallback chain.
    """
    message = render_execution_alert(setup, result)
    event_type = (
        NotificationEventType.AUTO_TRADE_EXECUTED if result.executed else NotificationEventType.AUTO_TRADE_FAILED
    )
    dispatch_result = await send_notification(
        event_type=event_type,
        message=message,
        subject=f"{'AUTO TRADE EXECUTED' if result.executed else 'AUTO TRADE FAILED'}: {setup.instrument}",
        related_signal_id=result.signal_id,
        db=db,
    )
    return {
        "dispatched": True,
        "message": message,
        "priority": dispatch_result.priority.value,
        "fallback_triggered": dispatch_result.fallback_triggered,
        "attempts": [a.model_dump(mode="json") for a in dispatch_result.attempts],
    }
