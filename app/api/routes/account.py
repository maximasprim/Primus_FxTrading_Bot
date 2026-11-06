"""GET /api/account/status - backs the "Account" tab: is the bot actually
connected to a (paper or real) broker account, and what's in it. Read-only,
no side effects."""
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.trading_mode import trading_mode_manager
from app.db.session import get_db_session
from app.execution.execution_service import get_broker
from app.services.trade_journal_service import get_open_trades

router = APIRouter()


@router.get("/status")
async def account_status(db: AsyncSession = Depends(get_db_session)):
    settings = get_settings()
    broker = get_broker()

    try:
        info = await broker.get_account_info()
    except Exception as exc:  # noqa: BLE001 - connection problems are a normal, displayable result here
        return {
            "adapter": settings.broker_adapter,
            "connected": False,
            "error": str(exc),
        }

    try:
        healthy = await broker.is_connection_healthy()
    except Exception:  # noqa: BLE001
        healthy = False

    open_trades = await get_open_trades(db)

    return {
        "adapter": settings.broker_adapter,
        "connected": info.connected and healthy,
        "broker_name": info.broker_name,
        "account_id": info.account_id,
        "currency": info.currency,
        "balance": info.balance,
        "equity": info.equity,
        "margin_used": info.margin_used,
        "margin_free": info.margin_free,
        "leverage": info.leverage,
        "server": info.server,
        "error": info.error,
        "open_positions_count": len(open_trades),
        "emergency_stop_active": trading_mode_manager.state.emergency_stop_active,
        "auto_trading_enabled": trading_mode_manager.state.auto_trading_enabled,
    }
