"""
FastAPI application entrypoint.

Run with:
    uvicorn app.main:app --reload
"""
from fastapi import FastAPI

from app.api.routes import (
    analysis,
    backtests,
    markets,
    notifications,
    performance,
    positions,
    risk_calculator,
    signals,
    trading_control,
    tradingview,
    websocket,
)
from app.config import get_settings
from app.core.trading_mode import trading_mode_manager
from app.db.session import AsyncSessionLocal
from fastapi.middleware.cors import CORSMiddleware

settings = get_settings()

app = FastAPI(
    title="AI Forex Confluence Analyst",
    description=(
        "Analysis-only, alert-driven Forex/XAU confluence engine. "
        "Does not place live trades. See README.md and PHASES.md."
    ),
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_allowed_origins_list,
    allow_credentials=settings.cors_allowed_origins_list != ["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def load_persisted_trading_mode_state() -> None:
    """Restores global_mode/paper_trading_enabled and per-instrument modes
    from the DB. Deliberately does NOT restore auto_trading_enabled — see
    TradingModeManager.load_state()'s docstring and spec §21: a restart must
    always come back up with auto-trading OFF.
    # NOTE: if the DB isn't reachable yet at startup (e.g. Supabase not
    # configured), this fails soft and the manager just keeps its safe
    # in-process defaults rather than blocking the app from starting.
    """
    try:
        async with AsyncSessionLocal() as db:
            await trading_mode_manager.load_state(db)
    except Exception as exc:  # noqa: BLE001
        import logging

        logging.getLogger(__name__).warning("Could not load persisted trading-mode state: %s", exc)


@app.get("/", tags=["health"])
async def root() -> dict:
    return {
        "status": "ok",
        "environment": settings.environment,
        "instruments": settings.initial_instruments,
        "message": "AI Forex Confluence Analyst is running. See /docs for the API.",
    }


@app.get("/health", tags=["health"])
async def health() -> dict:
    return {"status": "healthy"}


app.include_router(markets.router, prefix="/api/markets", tags=["markets"])
app.include_router(analysis.router, prefix="/api/analysis", tags=["analysis"])
app.include_router(signals.router, prefix="/api/signals", tags=["signals"])
app.include_router(tradingview.router, prefix="/api/tradingview", tags=["tradingview"])
app.include_router(performance.router, prefix="/api/performance", tags=["performance"])
app.include_router(backtests.router, prefix="/api/backtests", tags=["backtests"])
app.include_router(trading_control.router, prefix="/api/trading-control", tags=["trading-control"])
app.include_router(notifications.router, prefix="/api/notifications", tags=["notifications"])
app.include_router(websocket.router, prefix="/ws", tags=["websocket"])
app.include_router(positions.router, prefix="/api/positions", tags=["positions"])
app.include_router(risk_calculator.router, prefix="/api/risk-calculator", tags=["risk-calculator"])
