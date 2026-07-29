"""
Paper Trading Service (spec §8 of phases / Phase 8).

Runs confirmed setups against live/streaming market data to simulate fills,
track open "paper" positions, and update MFE/MAE, without ever touching a
real broker account. This is the last validation step before even
considering Phase 10 (automated execution) — which remains out of scope.

# TODO: this needs a scheduler (Celery beat / APScheduler) to poll open
# paper positions against live prices and update them; wire that up once
# a real market data provider (see data/providers/) is streaming ticks.
"""
from dataclasses import dataclass, field
from datetime import datetime

from app.data.market_data_engine import MarketDataEngine
from app.models.schemas import TradeSetup


@dataclass
class PaperPosition:
    instrument: str
    direction: str
    entry_price: float
    stop_loss: float
    take_profit_1: float
    take_profit_2: float | None
    opened_at: datetime
    status: str = "OPEN"  # OPEN | CLOSED_WIN | CLOSED_LOSS
    max_favorable_excursion: float = 0.0
    max_adverse_excursion: float = 0.0
    closed_at: datetime | None = None
    closed_price: float | None = None


# In-memory ledger for local/dev use.
# TODO: persist to DB (a PaperPosition table alongside Signal) for anything
# beyond local testing — in-memory state is lost on restart.
_open_positions: list[PaperPosition] = []


def open_paper_position(setup: TradeSetup) -> PaperPosition | None:
    if setup.entry_zone is None or setup.stop_loss is None or setup.take_profit_1 is None:
        return None

    entry_price = (setup.entry_zone.min + setup.entry_zone.max) / 2
    position = PaperPosition(
        instrument=setup.instrument,
        direction=setup.direction.value,
        entry_price=entry_price,
        stop_loss=setup.stop_loss,
        take_profit_1=setup.take_profit_1,
        take_profit_2=setup.take_profit_2,
        opened_at=setup.generated_at,
    )
    _open_positions.append(position)
    return position


async def update_open_positions() -> list[PaperPosition]:
    """Poll current prices for every open paper position and update
    MFE/MAE, closing positions that have hit stop or target.
    """
    engine = MarketDataEngine()
    still_open: list[PaperPosition] = []

    for position in _open_positions:
        if position.status != "OPEN":
            continue

        price_data = await engine.get_current_price(position.instrument)
        mid = (price_data["bid"] + price_data["ask"]) / 2

        favorable = (
            (mid - position.entry_price) if position.direction == "BUY" else (position.entry_price - mid)
        )
        position.max_favorable_excursion = max(position.max_favorable_excursion, favorable)
        position.max_adverse_excursion = min(position.max_adverse_excursion, favorable)

        hit_stop = (
            (mid <= position.stop_loss) if position.direction == "BUY" else (mid >= position.stop_loss)
        )
        hit_target = (
            (mid >= position.take_profit_1)
            if position.direction == "BUY"
            else (mid <= position.take_profit_1)
        )

        if hit_stop:
            position.status = "CLOSED_LOSS"
            position.closed_at = price_data["timestamp"]
            position.closed_price = mid
        elif hit_target:
            position.status = "CLOSED_WIN"
            position.closed_at = price_data["timestamp"]
            position.closed_price = mid
        else:
            still_open.append(position)

    return still_open


def list_positions(status: str | None = None) -> list[PaperPosition]:
    if status is None:
        return list(_open_positions)
    return [p for p in _open_positions if p.status == status]
