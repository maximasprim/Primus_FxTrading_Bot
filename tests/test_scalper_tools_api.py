"""HTTP layer for history, presets, schedule and start-with-limits."""
import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import app
from app.scalper import journal as journal_mod
from app.scalper.config import scalper_config_store
from app.scalper.engine import scalper_engine
from app.scalper.journal import TradeJournal
from app.scalper.presets import preset_store
from app.scalper.schedule import schedule_store

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
    monkeypatch.setattr(journal_mod, "journal", TradeJournal(tmp_path / "h.jsonl"))
    monkeypatch.setattr(preset_store, "_path", tmp_path / "p.json")
    monkeypatch.setattr(preset_store, "_state", None)
    monkeypatch.setattr(schedule_store, "_path", tmp_path / "s.json")
    monkeypatch.setattr(schedule_store, "_cfg", None)
    return TestClient(app)


@pytest.mark.parametrize("method,path", [
    ("get", "/api/scalper/history"), ("get", "/api/scalper/history/export"), ("get", "/api/scalper/presets"),
    ("post", "/api/scalper/presets"), ("post", "/api/scalper/presets/builtin-1/apply"), ("get", "/api/scalper/schedule"),
    ("put", "/api/scalper/schedule"), ("post", "/api/scalper/start-timed"),
])
def test_new_routes_need_the_api_key(client, method, path):
    assert getattr(client, method)(path).status_code == 401


def _seed(client):
    j = journal_mod.journal
    for i, (pnl, why) in enumerate([(0.4, "take_profit"), (-0.2, "stop_loss")]):
        j.record_open(ticket=str(i), opened_at=1000.0 + i * 60, instrument="EUR/USD", side="BUY", volume=0.01, entry=1.1,
                      sl=1.099, tp=1.102, pip_size=0.0001, spread_pips=0.8, score=75, reasons=[], entry_style="momentum",
                      preset="Custom", preset_id=None, session_id=1.0, account="1", demo=True, currency="USD")
        j.record_close(str(i), closed_at=1030.0 + i * 60, exit=1.102 if pnl > 0 else 1.099, pnl=pnl, reason=why)


def test_history_lists_trades_with_summary_and_filters(client):
    assert client.get("/api/scalper/history", headers=H).json()["total"] == 0
    _seed(client)
    r = client.get("/api/scalper/history", headers=H).json()
    assert r["total"] == 2 and r["summary"]["net"] == 0.2 and r["summary"]["win_rate"] == 50.0
    assert r["trades"][0]["summary"] and r["filters"]["instruments"] == ["EUR/USD"]
    assert client.get("/api/scalper/history?result=loss", headers=H).json()["total"] == 1
    assert client.get("/api/scalper/history?result=bogus", headers=H).status_code == 422
    assert client.get("/api/scalper/history?limit=1&offset=1", headers=H).json()["trades"][0]["ticket"] == "0"


def test_history_csv_export(client):
    _seed(client)
    r = client.get("/api/scalper/history/export", headers=H)
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers["content-disposition"] and len(r.text.strip().splitlines()) == 3


def test_presets_full_cycle_over_http(client):
    r = client.get("/api/scalper/presets", headers=H).json()
    assert [p["id"] for p in r["presets"]][:4] == ["builtin-1", "builtin-2", "builtin-3", "builtin-4"] and r["active"] is None
    ap = client.post("/api/scalper/presets/builtin-2/apply", headers=H).json()
    assert ap["config"]["tp_atr_mult"] == 5.0 and ap["active"]["name"].startswith("2 ·") and not ap["active"]["modified"]
    assert client.get("/api/scalper/config", headers=H).json()["config"]["tp_atr_mult"] == 5.0
    mine = client.post("/api/scalper/presets", headers=H, json={"name": "Mine", "description": "d"}).json()["preset"]
    assert client.put(f"/api/scalper/presets/{mine['id']}", headers=H, json={"name": "Mine v2"}).json()["preset"]["name"] == "Mine v2"
    assert client.post("/api/scalper/presets", headers=H, json={"name": "Mine v2"}).status_code == 422
    assert client.put("/api/scalper/presets/builtin-1", headers=H, json={"name": "x"}).status_code == 422
    assert client.delete("/api/scalper/presets/builtin-1", headers=H).status_code == 422
    assert client.post("/api/scalper/presets/nope/apply", headers=H).status_code == 404
    assert client.delete(f"/api/scalper/presets/{mine['id']}", headers=H).status_code == 200
    assert client.post("/api/scalper/presets", headers=H, json={"name": "Bad", "values": {"backend": "mt5"}}).status_code == 422


def test_schedule_roundtrip_and_validation(client):
    s = client.get("/api/scalper/schedule", headers=H).json()
    assert s["enabled"] is False and s["slots"] == [] and "now_local" in s
    body = {"enabled": True, "timezone": "Africa/Nairobi",
            "slots": [{"id": "a", "name": "Afternoon", "time": "15:30", "days": [0, 1, 2, 3, 4], "duration_minutes": 90, "profit_target_money": 5}]}
    r = client.put("/api/scalper/schedule", headers=H, json=body)
    assert r.status_code == 200 and r.json()["slots"][0]["next_run"] is not None
    assert client.put("/api/scalper/schedule", headers=H, json={**body, "timezone": "Nope/Nope"}).status_code == 422
    assert client.put("/api/scalper/schedule", headers=H, json={"slots": [{"time": "99:99"}]}).status_code == 422
    assert client.get("/api/scalper/schedule", headers=H).json()["slots"][0]["name"] == "Afternoon"


def test_start_timed_validates_and_passes_the_limits(client, monkeypatch):
    seen = {}

    async def fake_start(plan=None):
        seen["plan"] = plan
        return {"state": "RUNNING"}

    monkeypatch.setattr(scalper_engine, "start", fake_start)
    r = client.post("/api/scalper/start-timed", headers=H, json={"duration_minutes": 45, "profit_target_money": 3, "close_positions_at_end": False})
    assert r.status_code == 200
    p = seen["plan"]
    assert p.profit_target_money == 3 and p.close_positions_at_end is False and p.ends_at is not None and p.source == "manual"
    assert client.post("/api/scalper/start-timed", headers=H, json={"duration_minutes": 99999}).status_code == 422


def test_existing_scalper_routes_still_work_alongside(client):
    assert client.get("/api/scalper/status", headers=H).status_code == 200
    st = client.get("/api/scalper/status", headers=H).json()
    assert st["plan"] is None and "preset" in st
    assert client.get("/api/scalper/config", headers=H).json()["config"]["lock_rules_per_trade"] is True
