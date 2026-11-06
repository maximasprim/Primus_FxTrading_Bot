"""
Dashboard broadcast channel - pushes alerts to connected websocket clients
so the web dashboard (spec §23) updates live.

# TODO: wire this to an actual FastAPI WebSocket endpoint + connection
# manager once the frontend dashboard is built. For now this just tracks
# connections in memory and no-ops the broadcast if there are none.
"""
from fastapi import WebSocket

from app.models.schemas import TradeSetup

_active_connections: list[WebSocket] = []


async def register(ws: WebSocket) -> None:
    await ws.accept()
    _active_connections.append(ws)


def unregister(ws: WebSocket) -> None:
    if ws in _active_connections:
        _active_connections.remove(ws)


async def broadcast_alert(message: str, setup: TradeSetup) -> dict:
    payload = {"message": message, "setup": setup.model_dump(mode="json")}
    return await _broadcast(payload)


async def broadcast_notification(message: str, event_type: str) -> dict:
    """Low-level sender used directly by the notification engine for
    system-level events (broker failure, safety shutdowns, reports, etc.)
    that don't have an associated TradeSetup."""
    payload = {"message": message, "event_type": event_type}
    return await _broadcast(payload)


async def _broadcast(payload: dict) -> dict:
    delivered = 0
    for ws in list(_active_connections):
        try:
            await ws.send_json(payload)
            delivered += 1
        except Exception:  # noqa: BLE001 - drop dead connections silently
            unregister(ws)
    return {"sent": delivered > 0, "connections_notified": delivered}
