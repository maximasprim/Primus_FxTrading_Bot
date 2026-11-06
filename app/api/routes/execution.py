"""
Manual Execution API.

Backs the dashboard's per-pair "Enter" / "Close" buttons (and the chat
agent's place/close tools - see app/services/chat_agent_service.py). These
routes reuse the exact same safety pipeline as automatic execution
(app/execution/execution_service.py) for every hard capital-protection
gate; the only thing "manual" skips is the confluence-score/R:R
auto-execution minimums and the FULL_AUTO/MANUAL_APPROVAL mode gate - a
human explicitly clicking (or confirming in chat) IS the approval step.
"""
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.api_errors import market_data_error_to_http
from app.db.session import get_db_session
from app.execution.execution_service import get_broker, process_manual_close, process_manual_entry, process_manual_modify
from app.models.schemas import SetupStatus
from app.services.account_state_service import build_account_state
from app.services.signal_service import run_full_analysis
from app.services.trade_journal_service import get_open_trades_for_instrument

router = APIRouter()


class ManualActionRequest(BaseModel):
    instrument: str
    actor: str = "dashboard"
    # A trade only actually executes on the second call, once the caller
    # has shown the user what's about to happen and gotten an explicit
    # yes. The first call (confirm=false, the default) is always a no-op
    # preview - same two-step shape as the chat agent's confirm=true tools.
    confirm: bool = False
    # Fraction of the current open position to close (0 < x <= 1.0).
    # Defaults to a full close; e.g. 0.5 closes half and leaves the rest
    # open. Ignored by /manual/enter.
    close_fraction: float = 1.0


class ManualModifyRequest(BaseModel):
    instrument: str
    actor: str = "dashboard"
    new_stop_loss: float | None = None
    new_take_profit: float | None = None


@router.post("/manual/enter")
async def manual_enter(req: ManualActionRequest, db: AsyncSession = Depends(get_db_session)):
    instrument = req.instrument.upper()
    try:
        setup = await run_full_analysis(instrument)
    except Exception as exc:  # noqa: BLE001
        raise market_data_error_to_http(exc) from exc

    if not req.confirm:
        return {
            "setup": setup,
            "execution": None,
            "confirmation_required": True,
            "message": "Resend with confirm=true to place this trade.",
        }

    account = await build_account_state(db, get_broker())
    result = await process_manual_entry(setup, db=db, account=account, actor=req.actor)
    return {"setup": setup, "execution": result, "confirmation_required": False}


@router.post("/manual/close")
async def manual_close(req: ManualActionRequest, db: AsyncSession = Depends(get_db_session)):
    instrument = req.instrument.upper()

    if not req.confirm:
        open_entries = await get_open_trades_for_instrument(db, instrument)
        return {
            "instrument": instrument,
            "closed": [],
            "confirmation_required": True,
            "open_positions": [
                {
                    "signal_id": e.signal_id,
                    "direction": e.direction,
                    "entry": e.entry,
                    "stop_loss": e.stop_loss,
                    "take_profit": e.take_profit,
                }
                for e in open_entries
            ],
            "message": "Resend with confirm=true to close." if open_entries else "No open position to close.",
        }

    results = await process_manual_close(instrument, db=db, actor=req.actor, close_fraction=req.close_fraction)
    return {"instrument": instrument, "closed": results, "confirmation_required": False}


@router.post("/manual/modify")
async def manual_modify(req: ManualModifyRequest, db: AsyncSession = Depends(get_db_session)):
    instrument = req.instrument.upper()
    results = await process_manual_modify(
        instrument, db=db, actor=req.actor, new_stop_loss=req.new_stop_loss, new_take_profit=req.new_take_profit
    )
    return {"instrument": instrument, "modified": results}


@router.get("/manual/preview/{symbol}")
async def manual_preview(symbol: str):
    """Read-only: what would happen right now if the user clicked Enter -
    lets the frontend show the setup (and why it would or wouldn't be
    tradeable) before the button is pressed."""
    instrument = symbol.replace("-", "/").upper()
    try:
        setup = await run_full_analysis(instrument)
    except Exception as exc:  # noqa: BLE001
        raise market_data_error_to_http(exc) from exc
    return {
        "setup": setup,
        "tradeable": setup.status == SetupStatus.CONFIRMED
        and setup.entry_zone is not None
        and setup.stop_loss is not None
        and setup.take_profit_1 is not None,
    }
