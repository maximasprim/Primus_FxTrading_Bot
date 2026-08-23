"""GET /api/markets, GET /api/markets/{symbol} — spec §23 watchlist data."""
from datetime import timedelta

from fastapi import APIRouter

from app.config import get_settings
from app.core.api_errors import market_data_error_to_http
from app.data.market_data_engine import MarketDataEngine
from app.engines.market_regime import detect_regime
from app.engines.market_structure import analyze_market_structure

router = APIRouter()


@router.get("")
async def list_markets():
    settings = get_settings()
    return {
        "active_instruments": settings.initial_instruments,
        "planned_instruments": settings.future_instruments,
    }


@router.get("/{symbol}")
async def get_market(symbol: str):
    engine = MarketDataEngine()
    instrument = symbol.replace("-", "/").upper()

    try:
        candles_1h = await engine.get_validated_candles(
            instrument, "1H", lookback=timedelta(days=30)
        )
        price = await engine.get_current_price(instrument)
    except Exception as exc:  # noqa: BLE001 - classified below, not swallowed
        raise market_data_error_to_http(exc) from exc

    structure = analyze_market_structure(candles_1h)
    regime = detect_regime(candles_1h)

    return {
        "instrument": instrument,
        "current_price": price,
        "market_bias": structure.bias.value,
        "market_regime": regime.value,
        "latest_structure_event": (
            structure.latest_event.kind if structure.latest_event else None
        ),
    }

# """GET /api/markets, GET /api/markets/{symbol} — spec §23 watchlist data."""
# from datetime import timedelta

# from fastapi import APIRouter, HTTPException

# from app.config import get_settings
# from app.data.market_data_engine import InvalidMarketDataError, MarketDataEngine
# from app.engines.market_regime import detect_regime
# from app.engines.market_structure import analyze_market_structure

# router = APIRouter()


# @router.get("")
# async def list_markets():
#     settings = get_settings()
#     return {
#         "active_instruments": settings.initial_instruments,
#         "planned_instruments": settings.future_instruments,
#     }


# @router.get("/{symbol}")
# async def get_market(symbol: str):
#     engine = MarketDataEngine()
#     instrument = symbol.replace("-", "/").upper()

#     try:
#         candles_1h = await engine.get_validated_candles(
#             instrument, "1H", lookback=timedelta(days=30)
#         )
#         price = await engine.get_current_price(instrument)
#     except InvalidMarketDataError as exc:
#         raise HTTPException(status_code=422, detail=str(exc)) from exc

#     structure = analyze_market_structure(candles_1h)
#     regime = detect_regime(candles_1h)

#     return {
#         "instrument": instrument,
#         "current_price": price,
#         "market_bias": structure.bias.value,
#         "market_regime": regime.value,
#         "latest_structure_event": (
#             structure.latest_event.kind if structure.latest_event else None
#         ),
#     }
