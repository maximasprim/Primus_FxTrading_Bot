"""GET /api/backtests, POST /api/backtests/run — spec §19, §25."""
from datetime import datetime

from fastapi import APIRouter
from pydantic import BaseModel

from app.services.backtest_service import BacktestConfig, run_backtest

router = APIRouter()


class BacktestRequest(BaseModel):
    instrument: str
    timeframe: str = "1H"
    start: datetime
    end: datetime
    minimum_entry_score: int | None = None


@router.get("")
async def list_backtests():
    # TODO: query BacktestRun rows from the DB once persistence is wired up.
    return {"message": "No backtests recorded yet. POST /api/backtests/run to start one."}


@router.post("/run")
async def trigger_backtest(req: BacktestRequest):
    config = BacktestConfig(
        instrument=req.instrument.upper(),
        timeframe=req.timeframe,
        start=req.start,
        end=req.end,
        minimum_entry_score=req.minimum_entry_score,
    )
    return await run_backtest(config)
