"""
Scalper settings -- everything the dashboard's "Scalper" tab can change.

Design rules
------------
* Every field has hard min/max bounds that hold no matter what the UI sends.
* A session loss limit is REQUIRED (money or % of equity). The bot refuses to
  run with no way to stop itself.
* Defaults are conservative: `backend="simulation"`, `allow_live_account=False`.
  Pointing the bot at MT5 and at a real-money account are deliberate, separate
  switches.
* Settings persist to JSON, but the bot itself always starts STOPPED after a
  restart (same principle as spec §21 for auto-trading).
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from app.config import get_settings

logger = logging.getLogger(__name__)


class ScalperConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    # --- where it trades ---
    backend: Literal["mt5", "simulation"] = "simulation"
    allow_live_account: bool = False
    instruments: list[str] = Field(default_factory=lambda: ["EUR/USD", "GBP/USD", "XAU/USD"])
    loop_interval_ms: int = Field(500, ge=200, le=5000)

    # --- how many / how often ---
    max_concurrent_positions: int = Field(3, ge=1, le=20)        # total open at once
    max_positions_per_instrument: int = Field(2, ge=1, le=10)
    entries_per_signal: int = Field(1, ge=1, le=5)               # trades opened together on one signal
    cooldown_seconds: int = Field(15, ge=0, le=600)              # per-instrument gap between entry bursts
    max_trades_per_hour: int = Field(60, ge=1, le=600)

    # --- how picky ---
    min_signal_score: int = Field(65, ge=20, le=95)              # lower = looser / more trades
    max_spread_pips: float = Field(3.0, ge=0.1, le=50.0)
    min_tp_to_spread_ratio: float = Field(3.0, ge=1.0, le=10.0)  # skip if the target is < N x spread

    # --- size ---
    lot_mode: Literal["fixed", "risk_pct"] = "fixed"
    fixed_lot: float = Field(0.01, ge=0.01, le=5.0)
    risk_per_trade_pct: float = Field(0.25, ge=0.05, le=2.0)
    max_lot_per_trade: float = Field(0.5, ge=0.01, le=10.0)
    max_total_risk_pct: float = Field(2.0, ge=0.1, le=10.0)      # sum of open stop-loss risk, % of equity

    # --- exits ---
    exit_mode: Literal["atr", "fixed"] = "atr"
    tp_atr_mult: float = Field(1.2, ge=0.2, le=5.0)
    sl_atr_mult: float = Field(1.2, ge=0.2, le=5.0)
    take_profit_pips: float = Field(6.0, ge=0.5, le=500.0)       # used when exit_mode="fixed" (XAU: 1 pip = 0.10)
    stop_loss_pips: float = Field(8.0, ge=0.5, le=500.0)
    stagger_targets: bool = True                                 # later entries aim a bit further
    trade_profit_target_money: float = Field(0.0, ge=0.0)        # close a trade once it's up this much (0 = off)
    breakeven_after_pips: float = Field(0.0, ge=0.0, le=500.0)   # 0 = off
    trailing_enabled: bool = False
    trailing_start_pips: float = Field(4.0, ge=0.5, le=500.0)
    trailing_distance_pips: float = Field(2.5, ge=0.5, le=500.0)
    trailing_step_pips: float = Field(0.5, ge=0.1, le=100.0)
    max_hold_seconds: int = Field(600, ge=0, le=86_400)          # time stop (0 = off)

    # --- the bot's own off-switches ---
    basket_take_profit_money: float = Field(0.0, ge=0.0)         # close ALL when open P&L >= this (keeps running)
    session_profit_target_money: float = Field(0.0, ge=0.0)      # stop the bot when session P&L >= this (0 = off)
    session_max_loss_money: float = Field(20.0, ge=0.0)          # stop the bot when session P&L <= -this
    session_max_loss_pct: float = Field(1.0, ge=0.0, le=50.0)    # ...or this % of starting equity
    max_consecutive_losses: int = Field(5, ge=0, le=50)          # 0 = off
    flatten_on_halt: bool = True                                 # close everything when a limit stops the bot

    @field_validator("instruments")
    @classmethod
    def _valid_instruments(cls, v: list[str]) -> list[str]:
        settings = get_settings()
        allowed = set(settings.initial_instruments) | set(settings.future_instruments)
        cleaned: list[str] = []
        for item in v:
            norm = item.strip().upper().replace("-", "/")
            if norm not in allowed:
                raise ValueError(f"Unsupported instrument '{item}'. Allowed: {sorted(allowed)}")
            if norm not in cleaned:
                cleaned.append(norm)
        if not cleaned:
            raise ValueError("Pick at least one instrument.")
        return cleaned

    @model_validator(mode="after")
    def _safety_rules(self) -> "ScalperConfig":
        if self.session_max_loss_money <= 0 and self.session_max_loss_pct <= 0:
            raise ValueError(
                "Set a session loss limit (money or %). The scalper won't run without a way to stop itself."
            )
        if self.max_positions_per_instrument > self.max_concurrent_positions:
            self.max_positions_per_instrument = self.max_concurrent_positions
        return self


class ScalperConfigStore:
    """Holds the live config, validates updates, persists to disk atomically."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path
        self._config = ScalperConfig()
        self._loaded = False

    @property
    def path(self) -> Path:
        if self._path is None:
            self._path = Path(get_settings().scalper_state_dir) / "scalper_config.json"
        return self._path

    def get(self) -> ScalperConfig:
        if not self._loaded:
            self._load()
        return self._config

    def _load(self) -> None:
        self._loaded = True
        try:
            if self.path.exists():
                self._config = ScalperConfig.model_validate(json.loads(self.path.read_text(encoding="utf-8")))
        except (OSError, ValueError, ValidationError) as exc:
            logger.warning("Ignoring unreadable scalper config at %s (%s); using safe defaults.", self.path, exc)
            self._config = ScalperConfig()

    def update(self, changes: dict[str, Any]) -> ScalperConfig:
        """Validate `current + changes` as a whole, then persist. Raises ValidationError."""
        merged = {**self.get().model_dump(), **changes}
        new_config = ScalperConfig.model_validate(merged)
        self._config = new_config
        self._save()
        return new_config

    def reset(self) -> ScalperConfig:
        self._config = ScalperConfig()
        self._loaded = True
        self._save()
        return self._config

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self._config.model_dump(), fh, indent=2)
            os.replace(tmp, self.path)
        except OSError as exc:
            logger.warning("Could not save scalper config: %s", exc)


scalper_config_store = ScalperConfigStore()
