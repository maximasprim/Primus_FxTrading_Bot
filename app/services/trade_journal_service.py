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


async def get_open_trades(db: AsyncSession) -> list[TradeJournalEntry]:
    """All currently-open trade journal entries across every instrument --
    the set that position monitoring needs to check on each pass."""
    result = await db.execute(select(TradeJournalEntry).where(TradeJournalEntry.result == "OPEN"))
    return list(result.scalars().all())


def _r_multiple(direction: str, entry: float, exit_price: float, stop_loss: float) -> float | None:
    """How many multiples of the original risk (entry-to-stop distance) this
    trade made or lost. A +2.0 R win means it made twice what was risked;
    a -1.0 R loss means it lost exactly the planned risk amount."""
    risk_distance = abs(entry - stop_loss)
    if risk_distance == 0:
        return None
    raw_move = (exit_price - entry) if direction == "BUY" else (entry - exit_price)
    return raw_move / risk_distance


async def close_trade(
    db: AsyncSession,
    entry: TradeJournalEntry,
    exit_price: float,
    result: str,
) -> TradeJournalEntry:
    """Closes an OPEN trade journal entry with the real outcome -- this is
    what actually populates a genuine track record (spec §20, execution
    spec §19) rather than an invented number. `result` must be "WIN" or
    "LOSS" (callers decide which based on whether stop or target was hit).
    """
    r_multiple = _r_multiple(entry.direction, entry.entry, exit_price, entry.stop_loss)
    price_move = (exit_price - entry.entry) if entry.direction == "BUY" else (entry.entry - exit_price)

    entry.result = result
    entry.profit_loss = price_move * entry.position_size
    entry.r_multiple = r_multiple

    await db.commit()
    await db.refresh(entry)
    return entry

# """Trade Journal Service (execution-feature spec §19)."""
# from sqlalchemy import select
# from sqlalchemy.ext.asyncio import AsyncSession

# from app.models.db_models import TradeJournalEntry
# from app.models.execution_schemas import ExecutionSource
# from app.models.schemas import TradeSetup


# async def record_trade(
#     db: AsyncSession,
#     signal_id: str,
#     setup: TradeSetup,
#     entry_price: float,
#     position_size: float,
#     risk_pct: float,
#     execution_source: ExecutionSource,
#     broker_order_id: str | None,
# ) -> TradeJournalEntry:
#     entry = TradeJournalEntry(
#         signal_id=signal_id,
#         instrument=setup.instrument,
#         direction=setup.direction.value,
#         strategy_combination=setup.setup_type,
#         confluence_score=setup.confluence_score,
#         entry=entry_price,
#         stop_loss=setup.stop_loss or 0.0,
#         take_profit=setup.take_profit_1 or 0.0,
#         position_size=position_size,
#         risk_pct=risk_pct,
#         execution_source=execution_source.value,
#         broker_order_id=broker_order_id,
#         result="OPEN",
#     )
#     db.add(entry)
#     await db.commit()
#     await db.refresh(entry)
#     return entry


# async def get_open_signal_ids(db: AsyncSession, instrument: str) -> list[str]:
#     result = await db.execute(
#         select(TradeJournalEntry.signal_id).where(
#             TradeJournalEntry.instrument == instrument.upper(),
#             TradeJournalEntry.result == "OPEN",
#         )
#     )
#     return [row[0] for row in result.all()]
