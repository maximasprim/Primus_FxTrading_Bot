"""
Setting presets: named bundles of scalper settings you can switch between in one click.

* A preset only stores the rules it names; applying it validates `current settings + preset` as a whole through the
  normal config store, so every bound and safety rule still applies.
* Presets can NEVER change `backend` (simulation vs MT5) or `allow_live_account`. Those stay deliberate, separate switches.
* The built-in presets are starting points, not recommendations: none of them is proven profitable (see SCALPER.md).
* Your own presets are saved in `scalper_presets.json` next to the other scalper state.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from app.scalper.config import ScalperConfig, ScalperConfigStore, scalper_config_store

logger = logging.getLogger(__name__)

NEVER_IN_A_PRESET = {"backend", "allow_live_account"}
PRESET_KEYS = [k for k in ScalperConfig.model_fields if k not in NEVER_IN_A_PRESET]
MAX_USER_PRESETS = 30

BUILTIN_PRESETS: list[dict[str, Any]] = [
    {
        "id": "builtin-1", "name": "1 · Safety first (tiny)", "builtin": True,
        "description": "One trade at a time, 0.01 lots, picky entries, strict stop rules. Built to limit damage while you learn how the bot behaves. Not a profit setting.",
        "values": {
            "instruments": ["EUR/USD"], "entry_style": "momentum", "min_signal_score": 75, "fixed_lot": 0.01, "lot_mode": "fixed",
            "max_concurrent_positions": 1, "max_positions_per_instrument": 1, "entries_per_signal": 1, "cooldown_seconds": 120,
            "max_trades_per_hour": 20, "max_spread_pips": 1.2, "min_tp_to_spread_ratio": 4.0, "exit_mode": "atr",
            "tp_atr_mult": 2.0, "sl_atr_mult": 1.5, "max_hold_seconds": 900, "stagger_targets": False,
            "session_max_loss_money": 0.0, "session_max_loss_pct": 2.0, "max_consecutive_losses": 3, "flatten_on_halt": True,
        },
    },
    {
        "id": "builtin-2", "name": "2 · Big targets, few trades (EUR/USD)", "builtin": True,
        "description": "The least-costly setup in your backtests: fewer trades with large targets so the spread is a small share of each one. It still lost slightly in testing (about -0.16 pips per trade), so treat it as a way to learn, not to earn.",
        "values": {
            "instruments": ["EUR/USD"], "entry_style": "momentum", "min_signal_score": 70, "fixed_lot": 0.01, "lot_mode": "fixed",
            "max_concurrent_positions": 1, "max_positions_per_instrument": 1, "entries_per_signal": 1, "cooldown_seconds": 600,
            "max_trades_per_hour": 20, "max_spread_pips": 1.2, "min_tp_to_spread_ratio": 10.0, "exit_mode": "atr",
            "tp_atr_mult": 5.0, "sl_atr_mult": 2.5, "max_hold_seconds": 7200, "stagger_targets": False,
            "session_max_loss_money": 0.0, "session_max_loss_pct": 3.0, "max_consecutive_losses": 5, "flatten_on_halt": True,
        },
    },
    {
        "id": "builtin-3", "name": "3 · Spike fade (EUR/USD)", "builtin": True,
        "description": "Bets against sudden over-stretched moves. In your backtests this was the only style that was roughly flat BEFORE costs, but costs still made it lose.",
        "values": {
            "instruments": ["EUR/USD"], "entry_style": "spike_fade", "min_signal_score": 70, "fixed_lot": 0.01, "lot_mode": "fixed",
            "max_concurrent_positions": 2, "max_positions_per_instrument": 1, "entries_per_signal": 1, "cooldown_seconds": 60,
            "max_trades_per_hour": 30, "max_spread_pips": 1.2, "min_tp_to_spread_ratio": 3.0, "exit_mode": "atr",
            "tp_atr_mult": 2.0, "sl_atr_mult": 1.0, "max_hold_seconds": 600, "stagger_targets": False,
            "session_max_loss_money": 0.0, "session_max_loss_pct": 3.0, "max_consecutive_losses": 5, "flatten_on_halt": True,
        },
    },
    {
        "id": "builtin-4", "name": "4 · Quick scalps (original style)", "builtin": True,
        "description": "Many small, fast trades on the major pairs. This is the style that lost most in backtests because spread eats a large share of each small target. Included for comparison only.",
        "values": {
            "instruments": ["EUR/USD", "GBP/USD"], "entry_style": "momentum", "min_signal_score": 65, "fixed_lot": 0.01, "lot_mode": "fixed",
            "max_concurrent_positions": 3, "max_positions_per_instrument": 1, "entries_per_signal": 1, "cooldown_seconds": 15,
            "max_trades_per_hour": 60, "max_spread_pips": 1.5, "min_tp_to_spread_ratio": 3.0, "exit_mode": "atr",
            "tp_atr_mult": 1.2, "sl_atr_mult": 1.2, "max_hold_seconds": 600, "stagger_targets": False,
            "session_max_loss_money": 0.0, "session_max_loss_pct": 3.0, "max_consecutive_losses": 5, "flatten_on_halt": True,
        },
    },
]


class PresetError(ValueError):
    pass


def _clean(values: dict[str, Any]) -> dict[str, Any]:
    bad = [k for k in values if k in NEVER_IN_A_PRESET]
    if bad:
        raise PresetError(f"A preset can't change {', '.join(bad)} (that switch is always set by hand).")
    unknown = [k for k in values if k not in PRESET_KEYS]
    if unknown:
        raise PresetError(f"Unknown setting(s): {', '.join(unknown)}.")
    return dict(values)


class PresetStore:
    def __init__(self, path: Path | None = None, config_store: ScalperConfigStore | None = None) -> None:
        self._path = path
        self._cfg_store = config_store
        self._state: dict[str, Any] | None = None

    @property
    def cfg_store(self) -> ScalperConfigStore:
        return self._cfg_store or scalper_config_store

    @property
    def path(self) -> Path:
        if self._path is None:
            from app.config import get_settings
            self._path = Path(get_settings().scalper_state_dir) / "scalper_presets.json"
        return self._path

    # ------------------------------------------------------------------ storage
    def _load(self) -> dict[str, Any]:
        if self._state is None:
            state: dict[str, Any] = {"presets": [], "active": None}
            try:
                if self.path.exists():
                    raw = json.loads(self.path.read_text(encoding="utf-8"))
                    if isinstance(raw, dict):
                        state["presets"] = [p for p in raw.get("presets", []) if isinstance(p, dict) and p.get("id") and isinstance(p.get("values"), dict)]
                        state["active"] = raw.get("active") if isinstance(raw.get("active"), dict) else None
            except (OSError, ValueError) as exc:
                logger.warning("Ignoring unreadable presets file %s (%s).", self.path, exc)
            self._state = state
        return self._state

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self._load(), fh, indent=2)
            os.replace(tmp, self.path)
        except OSError as exc:
            logger.warning("Could not save presets: %s", exc)

    # ------------------------------------------------------------------ reading
    def list(self) -> list[dict[str, Any]]:
        user = [{**p, "builtin": False} for p in self._load()["presets"]]
        return [dict(b) for b in BUILTIN_PRESETS] + user

    def get(self, preset_id: str) -> dict[str, Any]:
        for p in self.list():
            if p["id"] == preset_id:
                return p
        raise KeyError(preset_id)

    def active(self) -> dict[str, Any] | None:
        a = self._load()["active"]
        if not a:
            return None
        try:
            preset = self.get(a["id"])
        except KeyError:
            return None
        cfg = self.cfg_store.get().model_dump()
        changed = [k for k, v in preset["values"].items() if cfg.get(k) != v]
        return {"id": preset["id"], "name": preset["name"], "modified": bool(changed), "changed_keys": changed,
                "applied_at": a.get("applied_at")}

    def current_label(self, cfg: ScalperConfig | None = None) -> tuple[str, str | None]:
        """(label, preset_id) to stamp on a trade: the preset name, '(edited)' if tweaked since, or 'Custom'."""
        a = self.active()
        if not a:
            return "Custom", None
        return (f"{a['name']} (edited)" if a["modified"] else a["name"]), a["id"]

    # ------------------------------------------------------------------ writing
    def _check_valid(self, values: dict[str, Any]) -> None:
        try:
            ScalperConfig.model_validate({**ScalperConfig().model_dump(), **values})
        except ValidationError as exc:
            msgs = "; ".join(f"{'.'.join(str(x) for x in e['loc'])}: {str(e['msg']).removeprefix('Value error, ')}" for e in exc.errors())
            raise PresetError(f"Those settings aren't valid: {msgs}") from exc

    def create(self, name: str, description: str = "", values: dict[str, Any] | None = None) -> dict[str, Any]:
        name = (name or "").strip()
        if not name:
            raise PresetError("Give the preset a name.")
        if len(name) > 60:
            raise PresetError("Keep the name under 60 characters.")
        if any(p["name"].lower() == name.lower() for p in self.list()):
            raise PresetError(f"A preset called '{name}' already exists.")
        state = self._load()
        if len(state["presets"]) >= MAX_USER_PRESETS:
            raise PresetError(f"You can keep up to {MAX_USER_PRESETS} presets. Delete one first.")
        if values is None:  # "save what I have now"
            cur = self.cfg_store.get().model_dump()
            values = {k: cur[k] for k in PRESET_KEYS}
        values = _clean(values)
        self._check_valid(values)
        preset = {"id": f"user-{uuid.uuid4().hex[:8]}", "name": name, "description": (description or "").strip()[:300],
                  "values": values, "created_at": time.time()}
        state["presets"].append(preset)
        self._save()
        return {**preset, "builtin": False}

    def update(self, preset_id: str, name: str | None = None, description: str | None = None,
               values: dict[str, Any] | None = None) -> dict[str, Any]:
        if preset_id.startswith("builtin-"):
            raise PresetError("The built-in presets can't be changed. Use 'Save as new preset' to make your own copy.")
        state = self._load()
        for p in state["presets"]:
            if p["id"] == preset_id:
                if name is not None:
                    name = name.strip()
                    if not name:
                        raise PresetError("Give the preset a name.")
                    if any(o["name"].lower() == name.lower() and o["id"] != preset_id for o in self.list()):
                        raise PresetError(f"A preset called '{name}' already exists.")
                    p["name"] = name[:60]
                if description is not None:
                    p["description"] = description.strip()[:300]
                if values is not None:
                    values = _clean(values)
                    self._check_valid(values)
                    p["values"] = values
                self._save()
                return {**p, "builtin": False}
        raise KeyError(preset_id)

    def delete(self, preset_id: str) -> None:
        if preset_id.startswith("builtin-"):
            raise PresetError("The built-in presets can't be deleted.")
        state = self._load()
        before = len(state["presets"])
        state["presets"] = [p for p in state["presets"] if p["id"] != preset_id]
        if len(state["presets"]) == before:
            raise KeyError(preset_id)
        if state["active"] and state["active"].get("id") == preset_id:
            state["active"] = None
        self._save()

    def apply(self, preset_id: str) -> dict[str, Any]:
        """Apply a preset to the live settings. Raises PresetError / pydantic ValidationError on a bad combination."""
        preset = self.get(preset_id)
        values = _clean(preset["values"])
        before = self.cfg_store.get().model_dump()
        new = self.cfg_store.update(values).model_dump()
        self._load()["active"] = {"id": preset_id, "applied_at": time.time()}
        self._save()
        diff = [{"key": k, "from": before.get(k), "to": new.get(k)} for k in values if before.get(k) != new.get(k)]
        return {"config": new, "changed": diff, "preset": {"id": preset["id"], "name": preset["name"]}}

    def clear_active(self) -> None:
        self._load()["active"] = None
        self._save()


preset_store = PresetStore()
