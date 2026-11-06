"""HTTP layer: API-key protection and the scalper endpoints."""
import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import app
from app.scalper.config import ScalperConfigStore, scalper_config_store

KEY = "k" * 32
H = {"X-API-Key": KEY}


@pytest.fixture
def client(tmp_path, monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "api_secret_key", KEY)
    monkeypatch.setattr(s, "api_auth_enabled", True)
    monkeypatch.setattr(s, "scalper_state_dir", str(tmp_path))
    monkeypatch.setattr(scalper_config_store, "_path", tmp_path / "c.json")
    monkeypatch.setattr(scalper_config_store, "_loaded", False)
    scalper_config_store.reset()
    return TestClient(app)  # no `with`: skips startup hooks (DB/Redis)


@pytest.mark.parametrize("method,path", [
    ("get", "/api/scalper/status"), ("put", "/api/scalper/config"), ("post", "/api/scalper/start"),
    ("post", "/api/execution/manual/enter"), ("post", "/api/trading-control/mode"), ("post", "/api/chat/message"),
    ("get", "/api/signals"), ("get", "/api/account/status"),
])
def test_routes_reject_missing_or_wrong_key(client, method, path):
    assert getattr(client, method)(path).status_code == 401
    assert getattr(client, method)(path, headers={"X-API-Key": "wrong"}).status_code == 401


def test_health_stays_public(client):
    assert client.get("/health").status_code == 200


def test_webhook_and_provider_callbacks_are_not_blocked_by_api_key(client):
    # They have their own verification; the point is they must NOT answer 401 for a missing X-API-Key.
    assert client.post("/api/tradingview/webhook", json={}).status_code != 401
    assert client.post("/api/notifications/whatsapp/inbound", json={}).status_code != 401
    assert client.post("/api/notifications/sms/inbound", data={}).status_code != 401


def test_websocket_needs_token(client):
    from starlette.websockets import WebSocketDisconnect
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws/alerts"):
            pass
    with client.websocket_connect(f"/ws/alerts?token={KEY}"):
        pass


def test_auth_can_be_disabled_explicitly(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "api_auth_enabled", False)
    assert client.get("/api/scalper/status").status_code == 200


def test_status_and_config_roundtrip(client):
    st = client.get("/api/scalper/status", headers=H).json()
    assert st["state"] == "STOPPED"
    cfg = client.get("/api/scalper/config", headers=H).json()
    assert cfg["config"]["backend"] == "simulation" and "EUR/USD" in cfg["allowed_instruments"]
    r = client.put("/api/scalper/config", headers=H, json={"max_concurrent_positions": 8, "session_profit_target_money": 50})
    assert r.status_code == 200 and r.json()["config"]["max_concurrent_positions"] == 8
    assert client.get("/api/scalper/config", headers=H).json()["config"]["session_profit_target_money"] == 50


def test_invalid_config_gets_readable_422_and_nothing_changes(client):
    r = client.put("/api/scalper/config", headers=H, json={"max_concurrent_positions": 500})
    assert r.status_code == 422 and r.json()["detail"][0]["field"] == "max_concurrent_positions"
    r = client.put("/api/scalper/config", headers=H, json={"session_max_loss_money": 0, "session_max_loss_pct": 0})
    assert r.status_code == 422 and "loss limit" in r.json()["detail"][0]["message"]
    assert client.get("/api/scalper/config", headers=H).json()["config"]["max_concurrent_positions"] == 3


def test_live_trading_needs_explicit_acknowledgement(client):
    r = client.put("/api/scalper/config", headers=H, json={"allow_live_account": True})
    assert r.status_code == 400
    r = client.put("/api/scalper/config?acknowledge_live_risk=true", headers=H, json={"allow_live_account": True})
    assert r.status_code == 200 and r.json()["config"]["allow_live_account"] is True


def test_diagnostics_on_simulation_is_ready(client):
    d = client.get("/api/scalper/diagnostics", headers=H).json()
    assert d["ready"] is True and d["backend"] == "simulation"
    assert any(c["name"] == "Algo Trading button (terminal)" and c["ok"] for c in d["checks"])


def test_diagnostics_for_mt5_on_a_machine_without_it_explains_itself(client, monkeypatch):
    import sys
    monkeypatch.setitem(sys.modules, "MetaTrader5", None)
    from app.mt5.client import get_mt5_client
    get_mt5_client.cache_clear()
    client.put("/api/scalper/config", headers=H, json={"backend": "mt5"})
    d = client.get("/api/scalper/diagnostics", headers=H).json()
    get_mt5_client.cache_clear()
    assert d["ready"] is False and "MetaTrader5" in d["checks"][0]["detail"]


def test_stop_and_flatten_when_idle_are_safe(client):
    assert client.post("/api/scalper/stop", headers=H, json={"close_positions": True}).json()["state"] == "STOPPED"
    assert client.post("/api/scalper/flatten", headers=H).json()["closed"] == 0


def test_backend_cannot_be_switched_while_running(client, monkeypatch):
    from app.scalper.engine import scalper_engine
    monkeypatch.setattr(scalper_engine, "state", "RUNNING")
    r = client.put("/api/scalper/config", headers=H, json={"backend": "mt5"})
    assert r.status_code == 409
    assert client.put("/api/scalper/config", headers=H, json={"max_concurrent_positions": 4}).status_code == 200  # other edits apply live
