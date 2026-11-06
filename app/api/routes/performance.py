"""GET /api/performance - spec §20, §23 performance dashboard."""
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db_session
from app.services.performance_service import compute_performance_summary

router = APIRouter()


@router.get("")
async def get_performance(
    instrument: str | None = None,
    db: AsyncSession = Depends(get_db_session),
):
    return await compute_performance_summary(db, instrument=instrument)
