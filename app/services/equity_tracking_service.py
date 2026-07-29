"""
Equity Curve Tracking Service (execution-feature spec §10 — max drawdown
safety shutdown).

Records equity snapshots to the DB and computes real running peak-equity /
drawdown from that history, rather than inferring drawdown from a single
current-equity value with no memory of what came before.
"""
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.db_models import EquityCurvePoint


@dataclass
class DrawdownStatus:
    current_equity: float
    peak_equity: float
    drawdown_pct: float


async def record_equity_snapshot(db: AsyncSession, equity: float, reason: str = "trade_closed") -> EquityCurvePoint:
    point = EquityCurvePoint(equity=equity, reason=reason)
    db.add(point)
    await db.commit()
    await db.refresh(point)
    return point


async def get_drawdown_status(db: AsyncSession, current_equity: float) -> DrawdownStatus:
    """Computes drawdown against the highest equity ever recorded, including
    `current_equity` itself (so a fresh peak is reflected immediately even
    before it's been snapshotted to the DB)."""
    result = await db.execute(select(EquityCurvePoint.equity))
    historical_values = [row[0] for row in result.all()]

    peak = max(historical_values + [current_equity]) if historical_values else current_equity
    drawdown_pct = ((peak - current_equity) / peak * 100) if peak > 0 else 0.0

    return DrawdownStatus(current_equity=current_equity, peak_equity=peak, drawdown_pct=drawdown_pct)
