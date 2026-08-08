"""
Capital & Risk Calculator routes.

This is deliberately arithmetic, not prediction -- see
app/services/risk_calculator_service.py's module docstring for why.
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db_session
from app.models.db_models import Signal
from app.services.risk_calculator_service import calculate_risk

router = APIRouter()


class RiskCalculatorRequest(BaseModel):
    instrument: str
    direction: str  # "BUY" | "SELL"
    entry: float
    stop_loss: float
    take_profit: float
    capital: float
    risk_pct: float = 1.0


@router.post("/calculate")
async def calculate_risk_endpoint(req: RiskCalculatorRequest):
    try:
        return calculate_risk(
            instrument=req.instrument,
            direction=req.direction,
            entry=req.entry,
            stop_loss=req.stop_loss,
            take_profit=req.take_profit,
            capital=req.capital,
            risk_pct=req.risk_pct,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/signals-for-calculator")
async def signals_available_for_calculator(db: AsyncSession = Depends(get_db_session)):
    """Recent CONFIRMED signals with entry/stop/target filled in, so the
    frontend calculator can offer 'pick a recent setup' instead of requiring
    the user to type numbers in manually.
    """
    result = await db.execute(
        select(Signal)
        .where(Signal.status == "CONFIRMED", Signal.entry_zone_min.isnot(None), Signal.stop_loss.isnot(None))
        .order_by(Signal.timestamp.desc())
        .limit(10)
    )
    return result.scalars().all()