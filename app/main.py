"""
FastAPI application entrypoint.

Run with:
    uvicorn app.main:app --reload
"""
from fastapi import Depends, FastAPI

from app.api.routes import (
    account,
    ai_settings,
    analysis,
    backtests,
    chat,
    execution,
    market_hours,
    markets,
    notifications,
    performance,
    positions,
    risk_calculator,
    scalper,
    signals,
    trading_control,
    tradingview,
    websocket,
)
from app.config import get_settings
from app.core.auth import require_api_key, warn_if_weak_key
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
    from the DB. Deliberately does NOT restore auto_trading_enabled - see
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


@app.on_event("startup")
async def check_security_config() -> None:
    warn_if_weak_key()


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


# Every router below sits behind the API-key check, except the TradingView
# webhook (own shared secret) and the websocket (token query param, see
# app/api/routes/websocket.py). /api/notifications/*/inbound is skipped inside
# require_api_key itself because Meta/Twilio can't send our header.
_protected = [Depends(require_api_key)]

app.include_router(markets.router, prefix="/api/markets", tags=["markets"], dependencies=_protected)
app.include_router(analysis.router, prefix="/api/analysis", tags=["analysis"], dependencies=_protected)
app.include_router(signals.router, prefix="/api/signals", tags=["signals"], dependencies=_protected)
app.include_router(tradingview.router, prefix="/api/tradingview", tags=["tradingview"])
app.include_router(performance.router, prefix="/api/performance", tags=["performance"], dependencies=_protected)
app.include_router(backtests.router, prefix="/api/backtests", tags=["backtests"], dependencies=_protected)
app.include_router(
    trading_control.router, prefix="/api/trading-control", tags=["trading-control"], dependencies=_protected
)
app.include_router(notifications.router, prefix="/api/notifications", tags=["notifications"], dependencies=_protected)
app.include_router(websocket.router, prefix="/ws", tags=["websocket"])
app.include_router(positions.router, prefix="/api/positions", tags=["positions"], dependencies=_protected)
app.include_router(
    risk_calculator.router, prefix="/api/risk-calculator", tags=["risk-calculator"], dependencies=_protected
)
app.include_router(execution.router, prefix="/api/execution", tags=["execution"], dependencies=_protected)
app.include_router(chat.router, prefix="/api/chat", tags=["chat"], dependencies=_protected)
app.include_router(account.router, prefix="/api/account", tags=["account"], dependencies=_protected)
app.include_router(ai_settings.router, prefix="/api/ai-settings", tags=["ai-settings"], dependencies=_protected)
app.include_router(market_hours.router, prefix="/api/market-hours", tags=["market-hours"], dependencies=_protected)
app.include_router(scalper.router, prefix="/api/scalper", tags=["scalper"], dependencies=_protected)
