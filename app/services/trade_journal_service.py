"""Trade Journal Service (execution-feature spec §19)."""
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.db_models import TradeJournalEntry
from app.models.execution_schemas import ExecutionSource
from app.models.schemas import TradeSetup


async def record_trade(
    db: AsyncSession,
    signal_id: str,
    setup: TradeSetup,
    entry_price: float,
    position_size: float,
    risk_pct: float,
    execution_source: ExecutionSource,
    broker_order_id: str | None,
) -> TradeJournalEntry:
    entry = TradeJournalEntry(
        signal_id=signal_id,
        instrument=setup.instrument,
        direction=setup.direction.value,
        strategy_combination=setup.setup_type,
        confluence_score=setup.confluence_score,
        entry=entry_price,
        stop_loss=setup.stop_loss or 0.0,
        take_profit=setup.take_profit_1 or 0.0,
        position_size=position_size,
        risk_pct=risk_pct,
        execution_source=execution_source.value,
        broker_order_id=broker_order_id,
        result="OPEN",
    )
    db.add(entry)
    await db.commit()
    await db.refresh(entry)
    return entry


async def get_open_signal_ids(db: AsyncSession, instrument: str) -> list[str]:
    result = await db.execute(
        select(TradeJournalEntry.signal_id).where(
            TradeJournalEntry.instrument == instrument.upper(),
            TradeJournalEntry.result == "OPEN",
        )
    )
    return [row[0] for row in result.all()]
