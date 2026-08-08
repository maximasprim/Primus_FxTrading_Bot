"""
Position monitoring routes.

POST /check  -- checks open trades against current prices, closes any that
                 hit stop/target, and records a real equity snapshot.
GET  /open   -- lists currently open trades.
"""
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db_session
from app.services.position_monitor_service import check_and_close_open_positions
from app.services.trade_journal_service import get_open_trades

router = APIRouter()


@router.post("/check")
async def check_positions(
    starting_equity: float = 10_000.0,
    db: AsyncSession = Depends(get_db_session),
):
    return await check_and_close_open_positions(db, starting_equity=starting_equity)


@router.get("/open")
async def list_open_positions(db: AsyncSession = Depends(get_db_session)):
    return await get_open_trades(db)