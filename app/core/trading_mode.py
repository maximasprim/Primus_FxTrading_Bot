"""
Trading Mode Manager (execution-feature spec §2, §5, §7-8, §21).

This is the single source of truth for "is the system allowed to place a
trade right now, and how." Every other module (execution_service,
safety_shutdown, API routes) reads from here — never from a local flag.

Safety invariants enforced by this module:
  - Auto-trading always starts OFF (spec §21), regardless of what was
    persisted before a restart. `auto_trading_enabled` is deliberately never
    read from or written to the database — see load_state()/persist_*()
    below, and GlobalTradingModeState in app/models/db_models.py, which has
    no such column at all.
  - Enabling auto-trading requires the two-step confirm flow (spec §8).
  - Every mode change is recorded via `TradingModeChangeAudit` — callers
    must supply `changed_by`.
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.execution_schemas import (
    GlobalTradingMode,
    InstrumentTradingMode,
    TradingModeChangeAudit,
)


@dataclass
class PendingActivation:
    requested_by: str
    requested_at: datetime
    ip_or_device: str | None


@dataclass
class TradingModeState:
    global_mode: GlobalTradingMode
    auto_trading_enabled: bool
    paper_trading_enabled: bool
    instrument_modes: dict[str, InstrumentTradingMode] = field(default_factory=dict)
    audit_log: list[TradingModeChangeAudit] = field(default_factory=list)
    emergency_stop_active: bool = False


class TradingModeManager:
    """In-memory manager, optionally backed by a DB for the *persistable*
    parts of its state (global_mode, paper_trading_enabled, per-instrument
    modes). `auto_trading_enabled` and `emergency_stop_active` are NEVER
    persisted — they always start at their safe in-process defaults
    (False / False) on every restart, per spec §21.
    # TODO: this is still a single-process singleton — a Redis-backed shared
    # state would be needed to run multiple backend workers safely.
    """

    def __init__(self):
        settings = get_settings()
        self._state = TradingModeState(
            global_mode=GlobalTradingMode(settings.default_global_trading_mode),
            auto_trading_enabled=settings.default_auto_trading_enabled,  # always False by default
            paper_trading_enabled=settings.default_paper_trading_enabled,
        )
        self._pending_activation: PendingActivation | None = None

    @property
    def state(self) -> TradingModeState:
        return self._state

    # ---------------- DB persistence (execution-feature spec §21) ----------------

    async def load_state(self, db: AsyncSession) -> None:
        """Call once at app startup. Restores global_mode/paper_trading_enabled
        and per-instrument modes from the DB — but deliberately does NOT touch
        auto_trading_enabled, which must always come back OFF (it's already
        initialized False in __init__ and there's no DB column for it at all)."""
        from app.models.db_models import GlobalTradingModeState, InstrumentConfig

        result = await db.execute(select(GlobalTradingModeState).where(GlobalTradingModeState.id == 1))
        row = result.scalar_one_or_none()
        if row is not None:
            self._state.global_mode = GlobalTradingMode(row.global_mode)
            self._state.paper_trading_enabled = row.paper_trading_enabled

        instrument_result = await db.execute(select(InstrumentConfig))
        for cfg in instrument_result.scalars().all():
            self._state.instrument_modes[cfg.instrument] = InstrumentTradingMode(cfg.trading_mode)

    async def _persist_global_state(self, db: AsyncSession) -> None:
        from app.models.db_models import GlobalTradingModeState

        result = await db.execute(select(GlobalTradingModeState).where(GlobalTradingModeState.id == 1))
        row = result.scalar_one_or_none()
        if row is None:
            row = GlobalTradingModeState(id=1)
            db.add(row)
        row.global_mode = self._state.global_mode.value
        row.paper_trading_enabled = self._state.paper_trading_enabled
        row.updated_at = datetime.utcnow()
        await db.commit()

    async def _persist_instrument_mode(self, db: AsyncSession, instrument: str) -> None:
        from app.models.db_models import InstrumentConfig

        instrument = instrument.upper()
        result = await db.execute(select(InstrumentConfig).where(InstrumentConfig.instrument == instrument))
        row = result.scalar_one_or_none()
        if row is None:
            row = InstrumentConfig(instrument=instrument)
            db.add(row)
        row.trading_mode = self._state.instrument_modes[instrument].value
        await db.commit()

    # ---------------- Global mode ----------------

    async def set_global_mode(
        self, new_mode: GlobalTradingMode, changed_by: str, ip_or_device: str | None = None, db: AsyncSession | None = None
    ) -> None:
        previous = self._state.global_mode
        self._state.global_mode = new_mode
        self._state.audit_log.append(
            TradingModeChangeAudit(
                changed_by=changed_by,
                previous_mode=previous.value,
                new_mode=new_mode.value,
                ip_or_device=ip_or_device,
            )
        )
        # Switching away from FULL_AUTO must also drop the auto-trading flag —
        # never leave it "on" under a mode that shouldn't allow it.
        if new_mode != GlobalTradingMode.FULL_AUTO:
            self._state.auto_trading_enabled = False
        if db is not None:
            await self._persist_global_state(db)

    # ---------------- Two-step auto-trading activation (spec §8) ----------------

    def request_enable_auto_trading(self, requested_by: str, ip_or_device: str | None = None) -> str:
        """Step 1. Returns a confirmation token/message; does NOT enable anything yet."""
        self._pending_activation = PendingActivation(
            requested_by=requested_by, requested_at=datetime.utcnow(), ip_or_device=ip_or_device
        )
        return (
            "WARNING: Automatic trading is enabled will place trades automatically "
            "once confirmed. Call confirm_enable_auto_trading() within 5 minutes to proceed."
        )

    def confirm_enable_auto_trading(self, confirmed_by: str, ip_or_device: str | None = None) -> bool:
        """Step 2. Only succeeds if a pending request exists and hasn't expired,
        and if it's confirmed by the same actor who requested it."""
        if self._pending_activation is None:
            return False
        if datetime.utcnow() - self._pending_activation.requested_at > timedelta(minutes=5):
            self._pending_activation = None
            return False
        if confirmed_by != self._pending_activation.requested_by:
            return False

        previous = "OFF" if not self._state.auto_trading_enabled else "ON"
        self._state.auto_trading_enabled = True
        self._state.global_mode = GlobalTradingMode.FULL_AUTO
        self._state.audit_log.append(
            TradingModeChangeAudit(
                changed_by=confirmed_by, previous_mode=previous, new_mode="ON", ip_or_device=ip_or_device
            )
        )
        self._pending_activation = None
        return True

    # ---------------- Emergency controls (spec §9) ----------------

    def stop_new_trades(self, changed_by: str) -> None:
        """Immediately prevents new trades; existing positions are untouched.
        This is intentionally a *separate* action from closing positions."""
        self._state.emergency_stop_active = True
        self._state.auto_trading_enabled = False
        self._state.audit_log.append(
            TradingModeChangeAudit(changed_by=changed_by, previous_mode="ON", new_mode="EMERGENCY_STOP")
        )

    def clear_emergency_stop(self, changed_by: str) -> None:
        self._state.emergency_stop_active = False
        self._state.audit_log.append(
            TradingModeChangeAudit(changed_by=changed_by, previous_mode="EMERGENCY_STOP", new_mode="CLEARED")
        )

    # ---------------- Per-instrument overrides (spec §5) ----------------

    async def set_instrument_mode(
        self, instrument: str, mode: InstrumentTradingMode, db: AsyncSession | None = None
    ) -> None:
        self._state.instrument_modes[instrument.upper()] = mode
        if db is not None:
            await self._persist_instrument_mode(db, instrument)

    def get_instrument_mode(self, instrument: str) -> InstrumentTradingMode:
        return self._state.instrument_modes.get(instrument.upper(), InstrumentTradingMode.ALERT_ONLY)

    # ---------------- Decision helper ----------------

    def is_auto_execution_allowed(self, instrument: str) -> bool:
        if self._state.emergency_stop_active:
            return False
        if self._state.global_mode != GlobalTradingMode.FULL_AUTO:
            return False
        if not self._state.auto_trading_enabled:
            return False
        return self.get_instrument_mode(instrument) == InstrumentTradingMode.AUTO

    def is_approval_required(self, instrument: str) -> bool:
        if self._state.emergency_stop_active:
            return False
        if self._state.global_mode == GlobalTradingMode.MANUAL_APPROVAL:
            return True
        return self.get_instrument_mode(instrument) == InstrumentTradingMode.APPROVAL


# Module-level singleton — mirrors how `get_settings()` is used elsewhere.
# # TODO: replace with a proper dependency-injected/shared instance once this
# runs across multiple worker processes (see class docstring).
trading_mode_manager = TradingModeManager()
