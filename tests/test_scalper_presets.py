"""Setting presets: validity of the built-ins, applying, safety exclusions, and what gets stamped on trades."""
import pytest
from pydantic import ValidationError

from app.scalper.config import ScalperConfig, ScalperConfigStore
from app.scalper.presets import BUILTIN_PRESETS, NEVER_IN_A_PRESET, PRESET_KEYS, PresetError, PresetStore


@pytest.fixture
def stores(tmp_path):
    cfg = ScalperConfigStore(tmp_path / "c.json")
    return cfg, PresetStore(tmp_path / "p.json", cfg)


@pytest.mark.parametrize("preset", BUILTIN_PRESETS, ids=lambda p: p["id"])
def test_every_builtin_preset_is_valid_and_safe(preset):
    assert not (set(preset["values"]) & NEVER_IN_A_PRESET)
    ScalperConfig.model_validate({**ScalperConfig().model_dump(), **preset["values"]})   # passes every bound and safety rule
    assert preset["values"]["session_max_loss_pct"] > 0 or preset["values"]["session_max_loss_money"] > 0
    assert preset["values"]["fixed_lot"] <= 0.01      # starters are tiny


def test_presets_can_never_switch_backend_or_live_trading(stores):
    cfg, ps = stores
    assert "backend" not in PRESET_KEYS and "allow_live_account" not in PRESET_KEYS
    with pytest.raises(PresetError):
        ps.create("x", values={"allow_live_account": True})
    with pytest.raises(PresetError):
        ps.create("y", values={"backend": "mt5"})
    with pytest.raises(PresetError):
        ps.create("z", values={"not_a_setting": 1})


def test_applying_a_preset_changes_settings_and_reports_what_changed(stores):
    cfg, ps = stores
    cfg.update({"min_signal_score": 50, "backend": "simulation"})
    res = ps.apply("builtin-2")
    now = cfg.get()
    assert now.min_signal_score == 70 and now.tp_atr_mult == 5.0 and now.instruments == ["EUR/USD"]
    assert now.backend == "simulation" and now.allow_live_account is False       # untouched
    assert {c["key"] for c in res["changed"]} >= {"min_signal_score", "tp_atr_mult"}
    a = ps.active()
    assert a["id"] == "builtin-2" and a["modified"] is False
    assert ps.current_label()[0].startswith("2 ·")


def test_editing_after_applying_is_labelled_edited(stores):
    cfg, ps = stores
    ps.apply("builtin-1")
    cfg.update({"min_signal_score": 60})
    a = ps.active()
    assert a["modified"] and "min_signal_score" in a["changed_keys"]
    assert ps.current_label()[0].endswith("(edited)")


def test_save_current_settings_as_my_own_preset_and_reapply(stores):
    cfg, ps = stores
    cfg.update({"min_signal_score": 83, "cooldown_seconds": 42})
    mine = ps.create("My setup", "tried on Monday")
    cfg.update({"min_signal_score": 60, "cooldown_seconds": 5})
    ps.apply(mine["id"])
    assert cfg.get().min_signal_score == 83 and cfg.get().cooldown_seconds == 42
    assert mine["id"] in [p["id"] for p in ps.list()] and not mine["builtin"]
    # persisted to disk
    again = PresetStore(ps.path, cfg)
    assert [p["name"] for p in again.list() if not p["builtin"]] == ["My setup"]


def test_duplicate_names_and_builtin_edits_are_refused(stores):
    cfg, ps = stores
    ps.create("Mine")
    with pytest.raises(PresetError):
        ps.create("mine")
    with pytest.raises(PresetError):
        ps.create("1 · Safety first (tiny)")
    with pytest.raises(PresetError):
        ps.update("builtin-1", name="Hacked")
    with pytest.raises(PresetError):
        ps.delete("builtin-1")


def test_a_preset_with_an_invalid_combination_cannot_be_saved(stores):
    cfg, ps = stores
    with pytest.raises(PresetError):
        ps.create("bad", values={"min_signal_score": 9999})
    with pytest.raises(PresetError):                      # no way to stop itself
        ps.create("nolimit", values={"session_max_loss_money": 0.0, "session_max_loss_pct": 0.0})


def test_update_and_delete_user_presets_and_active_is_cleared(stores):
    cfg, ps = stores
    p = ps.create("Temp")
    ps.apply(p["id"])
    ps.update(p["id"], name="Renamed", description="d")
    assert ps.active()["name"] == "Renamed"
    ps.delete(p["id"])
    assert ps.active() is None and ps.current_label() == ("Custom", None)
    with pytest.raises(KeyError):
        ps.get(p["id"])


def test_applying_a_preset_that_no_longer_validates_leaves_settings_untouched(stores, monkeypatch):
    cfg, ps = stores
    p = ps.create("Soon broken")
    ps._load()["presets"][0]["values"]["min_signal_score"] = 9999      # e.g. hand-edited file
    before = cfg.get().model_dump()
    with pytest.raises(ValidationError):
        ps.apply(p["id"])
    assert cfg.get().model_dump() == before and ps.active() is None


def test_corrupt_preset_file_falls_back_to_builtins_only(tmp_path):
    (tmp_path / "p.json").write_text("{not json")
    ps = PresetStore(tmp_path / "p.json", ScalperConfigStore(tmp_path / "c.json"))
    assert [p["id"] for p in ps.list()] == [b["id"] for b in BUILTIN_PRESETS]
