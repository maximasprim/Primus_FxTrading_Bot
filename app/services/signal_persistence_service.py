"""
Signal Persistence Service (spec §20, §25 step 10).

Writes every analysis result to the `signals` table and records a
SignalEvent for the stage transition, so nothing analyzed is ever lost —
this closes the "Store the event and analysis" TODO that was previously
left in the TradingView webhook route.
"""
import pandas as pd
from sqlalchemy.ext.asyncio import AsyncSession

from app.engines.session_analysis import current_session
from app.models.db_models import Signal, SignalEvent
from app.models.schemas import TradeSetup


async def persist_signal(
    db: AsyncSession,
    setup: TradeSetup,
    triggering_event: str | None = None,
) -> Signal:
    """Insert a new Signal row for this analysis result and append a
    SignalEvent recording the stage it was in at write time. Each webhook
    hit / analysis run gets its own row (spec §20 wants every alert
    recorded, not just the latest state per instrument)."""
    session_tag = current_session(pd.Timestamp(setup.generated_at, tz="UTC")).value

    row = Signal(
        instrument=setup.instrument,
        direction=setup.direction.value,
        timestamp=setup.generated_at,
        timeframe=setup.timeframe.value,
        entry_zone_min=setup.entry_zone.min if setup.entry_zone else None,
        entry_zone_max=setup.entry_zone.max if setup.entry_zone else None,
        stop_loss=setup.stop_loss,
        take_profit_1=setup.take_profit_1,
        take_profit_2=setup.take_profit_2,
        confluence_score=setup.confluence_score,
        score_breakdown=setup.score_breakdown.model_dump(),
        grade=setup.grade.value,
        market_regime=setup.market_regime.value,
        strategy_components={"setup_type": setup.setup_type, "session": session_tag},
        news_conditions=session_tag,
        status=setup.status.value,
        reasoning=setup.reasoning,
    )
    db.add(row)
    await db.flush()

    event = SignalEvent(
        signal_id=row.id,
        stage=setup.status.value,
        payload={"triggering_event": triggering_event} if triggering_event else {},
    )
    db.add(event)
    await db.commit()
    await db.refresh(row)
    return row
