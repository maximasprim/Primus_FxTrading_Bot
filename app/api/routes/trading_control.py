"""
Trading Control Center API (execution-feature spec §7-9, §18, §20).

Endpoints backing the dashboard control panel: global mode, per-instrument
mode, two-step auto-trading activation, emergency stop, and manual approval
decisions.
"""
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.trading_mode import trading_mode_manager
from app.db.session import get_db_session
from app.execution.approval_service import approval_service
from app.models.execution_schemas import GlobalTradingMode, InstrumentTradingMode

router = APIRouter()


class SetGlobalModeRequest(BaseModel):
    mode: GlobalTradingMode
    changed_by: str


class SetInstrumentModeRequest(BaseModel):
    instrument: str
    mode: InstrumentTradingMode


class ActorRequest(BaseModel):
    actor: str


@router.get("/status")
async def get_control_center_status():
    state = trading_mode_manager.state
    return {
        "global_mode": state.global_mode.value,
        "auto_trading_enabled": state.auto_trading_enabled,
        "paper_trading_enabled": state.paper_trading_enabled,
        "emergency_stop_active": state.emergency_stop_active,
        "instrument_modes": {k: v.value for k, v in state.instrument_modes.items()},
        "recent_audit_log": [entry.model_dump(mode="json") for entry in state.audit_log[-10:]],
    }


@router.post("/mode")
async def set_global_mode(req: SetGlobalModeRequest, request: Request, db: AsyncSession = Depends(get_db_session)):
    await trading_mode_manager.set_global_mode(
        req.mode, changed_by=req.changed_by, ip_or_device=request.client.host if request.client else None, db=db
    )
    return {"global_mode": trading_mode_manager.state.global_mode.value}


@router.post("/instrument-mode")
async def set_instrument_mode(req: SetInstrumentModeRequest, db: AsyncSession = Depends(get_db_session)):
    await trading_mode_manager.set_instrument_mode(req.instrument, req.mode, db=db)
    return {
        "instrument": req.instrument.upper(),
        "mode": trading_mode_manager.get_instrument_mode(req.instrument).value,
    }


# ---------------- Two-step auto-trading activation (spec §8) ----------------

@router.post("/auto-trading/request-enable")
async def request_enable_auto_trading(req: ActorRequest, request: Request):
    message = trading_mode_manager.request_enable_auto_trading(
        requested_by=req.actor, ip_or_device=request.client.host if request.client else None
    )
    return {"confirmation_required": True, "message": message}


@router.post("/auto-trading/confirm-enable")
async def confirm_enable_auto_trading(req: ActorRequest, request: Request):
    success = trading_mode_manager.confirm_enable_auto_trading(
        confirmed_by=req.actor, ip_or_device=request.client.host if request.client else None
    )
    if not success:
        raise HTTPException(
            status_code=400,
            detail="No pending activation request, it expired, or the confirming actor didn't match the requester.",
        )
    return {"auto_trading_enabled": True}


# ---------------- Emergency controls (spec §9) ----------------

@router.post("/emergency/stop-new-trades")
async def stop_new_trades(req: ActorRequest):
    trading_mode_manager.stop_new_trades(changed_by=req.actor)
    return {"emergency_stop_active": True, "note": "Existing positions are untouched. Use /emergency/close-all-positions separately if needed."}


@router.post("/emergency/clear-stop")
async def clear_emergency_stop(req: ActorRequest):
    trading_mode_manager.clear_emergency_stop(changed_by=req.actor)
    return {"emergency_stop_active": False}


# ---------------- Manual approval decisions (spec §18) ----------------

@router.get("/approvals/{signal_id}")
async def get_approval(signal_id: str):
    request = approval_service.get(signal_id)
    if request is None:
        raise HTTPException(status_code=404, detail="Unknown signal ID.")
    return request


@router.post("/approvals/{signal_id}/approve")
async def approve_trade(signal_id: str, req: ActorRequest):
    result = await approval_service.approve(signal_id, decided_by=req.actor)
    if not result.success:
        raise HTTPException(status_code=409, detail=result.reason)
    return {"approved": True}


@router.post("/approvals/{signal_id}/reject")
async def reject_trade(signal_id: str, req: ActorRequest):
    result = approval_service.reject(signal_id, decided_by=req.actor)
    if not result.success:
        raise HTTPException(status_code=409, detail=result.reason)
    return {"rejected": True}
