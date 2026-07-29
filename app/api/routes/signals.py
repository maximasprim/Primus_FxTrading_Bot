"""GET /api/signals, GET /api/signals/{id} — spec §25, §20."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db_session
from app.models.db_models import Signal

router = APIRouter()


@router.get("")
async def list_signals(
    instrument: str | None = None,
    limit: int = 50,
    db: AsyncSession = Depends(get_db_session),
):
    query = select(Signal).order_by(Signal.timestamp.desc()).limit(limit)
    if instrument:
        query = query.where(Signal.instrument == instrument.upper())
    result = await db.execute(query)
    signals = result.scalars().all()
    return signals


@router.get("/{signal_id}")
async def get_signal(signal_id: int, db: AsyncSession = Depends(get_db_session)):
    signal = await db.get(Signal, signal_id)
    if signal is None:
        raise HTTPException(status_code=404, detail="Signal not found.")
    return signal
