"""
POST /api/tradingview/webhook - spec §4, §25; execution-feature spec §1, §3.

The webhook endpoint must, in order:
  1. Validate the request (signature/secret)
  2. Validate the instrument
  3. Validate the event
  4. Fetch current market data
  5. Run full analysis
  6. Calculate confluence score
  7. Check news risk
  8. Check risk rules
  9. Generate the appropriate alert
  10. Store the event and analysis
  11. (execution feature) Hand confirmed setups to the execution pipeline -
      which itself decides, per trading mode, whether to auto-execute,
      create an approval request, or do nothing further (alert-only).
"""
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.security import verify_webhook_secret, webhook_rate_limiter
from app.db.session import get_db_session
from app.execution.execution_service import get_broker, process_setup_for_execution
from app.models.schemas import SetupStatus, TradingViewWebhookPayload
from app.services.account_state_service import build_account_state
from app.services.alert_service import dispatch_alert, dispatch_execution_alert
from app.services.signal_persistence_service import persist_signal
from app.services.signal_service import run_full_analysis

router = APIRouter()

_VALID_EVENTS = {"approaching_zone", "entered_zone", "pine_condition", "technical_condition"}


@router.post("/webhook")
async def tradingview_webhook(
    payload: TradingViewWebhookPayload,
    request: Request,
    db: AsyncSession = Depends(get_db_session),
):
    # 1. Validate the request
    webhook_rate_limiter.check(request.client.host if request.client else "unknown")
    verify_webhook_secret(payload.secret)

    # 2. Validate the instrument
    settings = get_settings()
    all_instruments = set(settings.initial_instruments) | set(settings.future_instruments)
    if payload.instrument.upper() not in all_instruments:
        raise HTTPException(status_code=400, detail=f"Unknown or unsupported instrument: {payload.instrument}")

    # 3. Validate the event
    if payload.event not in _VALID_EVENTS:
        raise HTTPException(status_code=400, detail=f"Unknown event type: {payload.event}")

    # 4-8: fetch data, run full analysis (includes confluence scoring,
    # news risk check, and risk-gate checks internally)
    try:
        setup = await run_full_analysis(payload.instrument.upper(), triggering_price=payload.price)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    # 9. Generate the appropriate alert (only if it clears the bar)
    alert_result = await dispatch_alert(setup, db=db)

    # 10. Store the event and analysis
    signal_row = await persist_signal(db, setup, triggering_event=payload.event)

    # 11. Hand off to the execution pipeline - only ever for CONFIRMED setups.
    # process_setup_for_execution internally checks trading mode and will:
    #   - do nothing further if the instrument is alert-only,
    #   - create a pending approval request if MANUAL_APPROVAL applies,
    #   - or run the full pre-trade validation pipeline and place the order
    #     if FULL_AUTO is enabled for this instrument.
    execution_result = None
    if setup.status == SetupStatus.CONFIRMED:
        account = await build_account_state(db, get_broker())
        execution_result = await process_setup_for_execution(setup, db=db, account=account)
        # The dedicated "AUTOMATIC TRADE EXECUTED / FAILED" alert (spec §17) is
        # for actual execution attempts only - a successful placement, or a
        # broker-level rejection. Routine pipeline gating (score too low, news
        # risk, safety shutdown, approval-required, etc.) is not a "failure";
        # it's the system correctly declining to trade, already reflected in
        # the regular setup alert above.
        if execution_result.executed or (
            execution_result.failure and execution_result.failure.stage == "broker_execution"
        ):
            await dispatch_execution_alert(setup, execution_result, db=db)

    return {"setup": setup, "signal_db_id": signal_row.id, "alert": alert_result, "execution": execution_result}

