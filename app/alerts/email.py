"""Email alert channel via SMTP. Requires SMTP_* settings in .env."""
import asyncio
import smtplib
from email.mime.text import MIMEText

from app.config import get_settings
from app.models.schemas import TradeSetup


def _send_sync(message: str, subject: str, to_email: str | None) -> bool:
    settings = get_settings()
    recipient = to_email or settings.alert_email_to
    if not (settings.smtp_host and settings.alert_email_from and recipient):
        return False

    msg = MIMEText(message)
    msg["Subject"] = subject
    msg["From"] = settings.alert_email_from
    msg["To"] = recipient

    with smtplib.SMTP(settings.smtp_host, settings.smtp_port) as server:
        server.starttls()
        if settings.smtp_user:
            server.login(settings.smtp_user, settings.smtp_password)
        server.sendmail(settings.alert_email_from, [recipient], msg.as_string())
    return True


async def send_email(message: str, subject: str, to_email: str | None = None) -> dict:
    """Low-level sender used directly by the notification engine."""
    try:
        sent = await asyncio.to_thread(_send_sync, message, subject, to_email)
    except Exception as exc:  # noqa: BLE001
        return {"sent": False, "reason": str(exc)}
    return {"sent": sent, "reason": None if sent else "SMTP settings not configured."}


async def send_alert(message: str, setup: TradeSetup) -> dict:
    """Back-compat wrapper for app/services/alert_service.py's TradeSetup-based dispatch."""
    from app.core.notification_preferences import notification_preferences_manager

    subject = f"[{setup.grade.value}] {setup.instrument} {setup.direction.value} setup"
    to_email = notification_preferences_manager.preferences.email
    return await send_email(message, subject, to_email)
