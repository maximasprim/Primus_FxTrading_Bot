"""GET /api/analysis/{symbol}, POST /api/analysis/run — spec §25."""
from fastapi import APIRouter, HTTPException

from app.services.signal_service import run_full_analysis

router = APIRouter()


@router.get("/{symbol}")
async def get_analysis(symbol: str):
    instrument = symbol.replace("-", "/").upper()
    try:
        result = await run_full_analysis(instrument)
    except Exception as exc:  # noqa: BLE001 - surfaced to caller as a 422
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return result


@router.post("/run")
async def trigger_analysis(instrument: str):
    try:
        result = await run_full_analysis(instrument.upper())
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return result
