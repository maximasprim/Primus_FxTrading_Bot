"""
Notification API (notification-feature spec §6, §9-13).

Covers: user notification preferences, a manual test-send endpoint, querying
the delivery log, inbound WhatsApp/SMS webhooks for trade-approval commands
(spec §10), and daily/weekly report generation (spec §13).
"""
import json

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.notification_preferences import notification_preferences_manager
from app.core.webhook_signatures import verify_meta_signature, verify_twilio_signature
from app.db.session import get_db_session
from app.execution.approval_service import execute_approval_command, parse_approval_command
from app.models.db_models import NotificationLog
from app.models.notification_schemas import NotificationChannel, NotificationEventType
from app.services.notification_service import send_notification
from app.services.report_service import (
    generate_daily_report,
    generate_weekly_report,
    render_daily_report_text,
    render_weekly_report_text,
)

router = APIRouter()


# ---------------- Preferences (spec §6) ----------------

class ContactInfoRequest(BaseModel):
    whatsapp_number: str | None = None
    sms_number: str | None = None
    email: str | None = None


class EventChannelsRequest(BaseModel):
    event_type: NotificationEventType
    channels: list[NotificationChannel]


@router.get("/preferences")
async def get_preferences():
    return notification_preferences_manager.preferences


@router.post("/preferences/contact-info")
async def set_contact_info(req: ContactInfoRequest):
    notification_preferences_manager.set_contact_info(
        whatsapp_number=req.whatsapp_number, sms_number=req.sms_number, email=req.email
    )
    return notification_preferences_manager.preferences


@router.post("/preferences/event-channels")
async def set_event_channels(req: EventChannelsRequest):
    notification_preferences_manager.set_channels_for_event(req.event_type, req.channels)
    return {
        "event_type": req.event_type.value,
        "channels": [c.value for c in notification_preferences_manager.channels_for(req.event_type)],
    }


# ---------------- Test send + delivery log (spec §9) ----------------

class TestSendRequest(BaseModel):
    event_type: NotificationEventType
    message: str = "This is a test notification from your Forex AI Analyst."


@router.post("/test-send")
async def test_send(req: TestSendRequest, db: AsyncSession = Depends(get_db_session)):
    result = await send_notification(req.event_type, req.message, db=db)
    return result


@router.get("/log")
async def get_delivery_log(
    event_type: str | None = None,
    limit: int = 50,
    db: AsyncSession = Depends(get_db_session),
):
    query = select(NotificationLog).order_by(NotificationLog.created_at.desc()).limit(limit)
    if event_type:
        query = query.where(NotificationLog.event_type == event_type)
    result = await db.execute(query)
    return result.scalars().all()


# ---------------- Inbound approval commands (spec §10) ----------------

class InboundCommandResult(BaseModel):
    recognized: bool
    executed: bool
    reason: str | None = None


async def _handle_inbound_command(text: str, sender_identifier: str, authorized_identifier: str | None) -> InboundCommandResult:
    """Shared logic for both WhatsApp and SMS inbound webhooks: parse the
    command, verify the sender is the authorized user for this channel
    (spec §10: 'The system must validate: User identity...'), then execute.
    """
    command = parse_approval_command(text)
    if command is None:
        return InboundCommandResult(recognized=False, executed=False, reason="Not a recognized APPROVE/REJECT command.")

    if not authorized_identifier or sender_identifier != authorized_identifier:
        return InboundCommandResult(
            recognized=True, executed=False,
            reason="Sender is not the authorized user for this signal's approval channel.",
        )

    result = await execute_approval_command(command, decided_by=sender_identifier)
    return InboundCommandResult(recognized=True, executed=result.success, reason=result.reason)


@router.post("/whatsapp/inbound")
async def whatsapp_inbound(request: Request):
    """Meta WhatsApp Cloud API webhook shape. Configure this URL as the
    webhook callback in your WhatsApp Business app settings.
    """
    settings = get_settings()
    raw_body = await request.body()

    if settings.whatsapp_app_secret:
        signature = request.headers.get("X-Hub-Signature-256")
        if not verify_meta_signature(raw_body, signature, settings.whatsapp_app_secret):
            raise HTTPException(status_code=401, detail="Invalid webhook signature.")
    # else: WHATSAPP_APP_SECRET not configured -- signature check is skipped.
    # Fine for local testing; set it before exposing this endpoint publicly.

    try:
        payload = json.loads(raw_body)
        entry = payload["entry"][0]["changes"][0]["value"]
        message = entry["messages"][0]
        sender = message["from"]
        text = message["text"]["body"]
    except (KeyError, IndexError, ValueError):
        raise HTTPException(status_code=400, detail="Unrecognized WhatsApp webhook payload shape.")

    authorized = notification_preferences_manager.preferences.whatsapp_number
    return await _handle_inbound_command(text, sender, authorized)


@router.post("/sms/inbound")
async def sms_inbound(request: Request):
    """Twilio-style inbound SMS webhook (form-encoded: From, Body)."""
    settings = get_settings()
    form = await request.form()
    form_dict = {k: str(v) for k, v in form.items()}

    if settings.twilio_auth_token:
        signature = request.headers.get("X-Twilio-Signature")
        full_url = str(request.url)
        if not verify_twilio_signature(full_url, form_dict, signature, settings.twilio_auth_token):
            raise HTTPException(status_code=401, detail="Invalid webhook signature.")
    # else: TWILIO_AUTH_TOKEN not configured -- signature check is skipped.

    sender = form_dict.get("From")
    text = form_dict.get("Body")
    if not sender or not text:
        raise HTTPException(status_code=400, detail="Missing From/Body in inbound SMS payload.")

    authorized = notification_preferences_manager.preferences.sms_number
    return await _handle_inbound_command(text, sender, authorized)


# ---------------- Reports (spec §13) ----------------

@router.post("/reports/daily")
async def trigger_daily_report(db: AsyncSession = Depends(get_db_session)):
    report = await generate_daily_report(db)
    text = render_daily_report_text(report)
    dispatch = await send_notification(NotificationEventType.DAILY_SUMMARY, text, db=db)
    return {"report": report, "dispatch": dispatch}


@router.post("/reports/weekly")
async def trigger_weekly_report(db: AsyncSession = Depends(get_db_session)):
    report = await generate_weekly_report(db)
    text = render_weekly_report_text(report)
    dispatch = await send_notification(NotificationEventType.WEEKLY_REPORT, text, db=db)
    return {"report": report, "dispatch": dispatch}
