"""GET /api/market-hours/status - is the forex/XAU market open right now,
and why not if it's closed. See app/core/market_hours.py."""
from fastapi import APIRouter

from app.core.market_hours import get_market_status

router = APIRouter()


@router.get("/status")
async def market_hours_status():
    status = get_market_status()
    return {
        "open": status.open,
        "reason": status.reason,
        "ny_time": status.ny_time,
        "holidays": status.holidays,
    }
