"""
WebSocket endpoint for the live dashboard alert feed.

Wires up app/alerts/dashboard_ws.py's connection registry (previously built
but never actually exposed as a route - see the old frontend/README.md's
TODO) so the frontend can subscribe to real-time alerts as they're
dispatched by app/services/notification_service.py.
"""
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.alerts.dashboard_ws import register, unregister
from app.core.auth import authorize_websocket

router = APIRouter()


@router.websocket("/alerts")
async def websocket_alerts(websocket: WebSocket):
    if not await authorize_websocket(websocket):
        return
    await register(websocket)
    try:
        while True:
            # We don't expect inbound messages, but need to keep the
            # connection open and detect disconnects.
            await websocket.receive_text()
    except WebSocketDisconnect:
        unregister(websocket)
