"""
Scheduled scalper sessions: "start at 15:30 every weekday, run for 90 minutes or until +10 profit".

How it stays safe
-----------------
* Nothing is scheduled until you switch the schedule ON; it is OFF by default.
* A scheduled start goes through the exact same checks as pressing Start (connected, demo/live rule, Algo Trading,
  Control Center emergency stop...). The scheduler cannot bypass any of them.
* It starts a session only within a short grace window after the start time (10 min). If the server was down or MT5
  wasn't ready in that window, that day's run is marked "missed" and NOT started late, so a restart never resumes
  trading hours later by surprise.
* A session's end time is fixed on the clock (start time + duration), not "duration from whenever it actually started".
* Each slot runs at most once per local day.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from app.scalper.models import SessionPlan

logger = logging.getLogger(__name__)

GRACE_SECONDS = 600
MAX_SLOTS = 6
POLL_SECONDS = 10


class ScheduleSlot(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:8])
    name: str = Field("Daily session", max_length=40)
    enabled: bool = True
    time: str = "15:30"                                   # local time in the schedule's timezone, 24h "HH:MM"
    days: list[int] = Field(default_factory=lambda: [0, 1, 2, 3, 4])   # 0 = Monday ... 6 = Sunday
    duration_minutes: int = Field(60, ge=0, le=1440)      # 0 = no time limit (only the stop rules end it)
    profit_target_money: float = Field(0.0, ge=0.0)       # stop once the session is up this much (0 = off)
    close_positions_at_end: bool = True
    require_market_open: bool = True
    preset_id: str | None = None                          # apply this preset right before starting (None = use current settings)
    last_run_date: str | None = None                      # local date this slot last started / was handled
    last_result: str | None = None

    @field_validator("time")
    @classmethod
    def _valid_time(cls, v: str) -> str:
        try:
            h, m = v.strip().split(":")
            hh, mm = int(h), int(m)
            if not (0 <= hh <= 23 and 0 <= mm <= 59):
                raise ValueError
        except ValueError as exc:
            raise ValueError("Use 24-hour time like 15:30.") from exc
        return f"{hh:02d}:{mm:02d}"

    @field_validator("days")
    @classmethod
    def _valid_days(cls, v: list[int]) -> list[int]:
        days = sorted(set(v))
        if not days or any(d < 0 or d > 6 for d in days):
            raise ValueError("Pick at least one day (0 = Monday ... 6 = Sunday).")
        return days

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        return v.strip() or "Daily session"


class ScheduleConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    enabled: bool = False
    timezone: str = "UTC"
    slots: list[ScheduleSlot] = Field(default_factory=list, max_length=MAX_SLOTS)

    @field_validator("timezone")
    @classmethod
    def _valid_tz(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"Unknown timezone '{v}'. Use a name like Africa/Nairobi or Europe/London.") from exc
        return v

    @model_validator(mode="after")
    def _unique_ids(self) -> "ScheduleConfig":
        ids = [s.id for s in self.slots]
        if len(ids) != len(set(ids)):
            raise ValueError("Duplicate slot ids.")
        return self


class ScheduleStore:
    def __init__(self, path: Path | None = None) -> None:
        self._path = path
        self._cfg: ScheduleConfig | None = None

    @property
    def path(self) -> Path:
        if self._path is None:
            from app.config import get_settings
            self._path = Path(get_settings().scalper_state_dir) / "scalper_schedule.json"
        return self._path

    def get(self) -> ScheduleConfig:
        if self._cfg is None:
            try:
                self._cfg = ScheduleConfig.model_validate(json.loads(self.path.read_text(encoding="utf-8"))) if self.path.exists() else ScheduleConfig()
            except (OSError, ValueError, ValidationError) as exc:
                logger.warning("Ignoring unreadable schedule file %s (%s); schedule stays OFF.", self.path, exc)
                self._cfg = ScheduleConfig()
        return self._cfg

    def replace(self, new: dict[str, Any]) -> ScheduleConfig:
        """Validate and save a whole new schedule. The server-owned run history of a slot id is kept."""
        old = {s.id: s for s in self.get().slots}
        slots = []
        for raw in new.get("slots", []):
            raw = dict(raw)
            prev = old.get(raw.get("id", ""))
            raw["last_run_date"] = prev.last_run_date if prev else None     # never writable from the dashboard
            raw["last_result"] = prev.last_result if prev else None
            slots.append(raw)
        cfg = ScheduleConfig.model_validate({**new, "slots": slots})
        self._cfg = cfg
        self._save()
        return cfg

    def mark(self, slot_id: str, local_date: str | None, result: str) -> None:
        for s in self.get().slots:
            if s.id == slot_id:
                if local_date is not None:
                    s.last_run_date = local_date
                s.last_result = result
        self._save()

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self.get().model_dump(), fh, indent=2)
            os.replace(tmp, self.path)
        except OSError as exc:
            logger.warning("Could not save the schedule: %s", exc)


def _default_market_open(ts: float) -> tuple[bool, str | None]:
    from app.core.market_hours import get_market_status
    st = get_market_status(datetime.fromtimestamp(ts, timezone.utc))
    return st.open, st.reason


def _start_dt(slot: ScheduleSlot, day: datetime) -> datetime:
    h, m = (int(x) for x in slot.time.split(":"))
    return day.replace(hour=h, minute=m, second=0, microsecond=0)


def next_run(cfg: ScheduleConfig, slot: ScheduleSlot, now: float) -> dict[str, Any] | None:
    """When the slot will next start (local time), for the dashboard's countdown."""
    zone = ZoneInfo(cfg.timezone)
    now_local = datetime.fromtimestamp(now, zone)
    for add in range(0, 9):
        day = now_local + timedelta(days=add)
        if day.weekday() not in slot.days:
            continue
        start = _start_dt(slot, day)
        if slot.last_run_date == start.date().isoformat():
            continue
        if (now - start.timestamp()) > GRACE_SECONDS:
            continue
        return {"at": start.isoformat(), "in_seconds": int(start.timestamp() - now), "ts": start.timestamp()}
    return None


class Scheduler:
    def __init__(self, store: ScheduleStore | None = None, engine=None, presets=None,
                 clock: Callable[[], float] = time.time,
                 market_open: Callable[[float], tuple[bool, str | None]] = _default_market_open) -> None:
        self.store = store or schedule_store
        self._engine = engine
        self._presets = presets
        self._clock = clock
        self._market_open = market_open
        self._task: asyncio.Task | None = None

    @property
    def engine(self):
        if self._engine is None:
            from app.scalper.engine import scalper_engine
            return scalper_engine
        return self._engine

    @property
    def presets(self):
        if self._presets is None:
            from app.scalper.presets import preset_store
            return preset_store
        return self._presets

    # ------------------------------------------------------------------ one pass
    async def tick(self, now: float | None = None) -> list[str]:
        """Check every slot once. Returns a short log of what it did (handy for tests and the activity log)."""
        now = self._clock() if now is None else now
        cfg = self.store.get()
        done: list[str] = []
        if not cfg.enabled:
            return done
        zone = ZoneInfo(cfg.timezone)
        now_local = datetime.fromtimestamp(now, zone)
        for slot in cfg.slots:
            if not slot.enabled or now_local.weekday() not in slot.days:
                continue
            start = _start_dt(slot, now_local)
            today = start.date().isoformat()
            if slot.last_run_date == today:
                continue
            late = now - start.timestamp()
            if late < 0:
                continue                                            # not time yet
            if late > GRACE_SECONDS:
                self.store.mark(slot.id, today, "Missed: the server wasn't ready within 10 minutes of the start time"
                                + (f" ({slot.last_result})" if slot.last_result and slot.last_result.startswith("Could not") else ""))
                done.append(f"{slot.name}: missed")
                continue
            ends_at = start.timestamp() + slot.duration_minutes * 60 if slot.duration_minutes > 0 else None
            if self.engine.state == "RUNNING":
                self.store.mark(slot.id, today, "Skipped: the scalper was already running")
                done.append(f"{slot.name}: skipped (already running)")
                continue
            if slot.require_market_open:
                is_open, why = self._market_open(now)
                if not is_open:
                    self.store.mark(slot.id, today, f"Skipped: market closed ({why})")
                    done.append(f"{slot.name}: skipped (market closed)")
                    continue
            if slot.preset_id:
                try:
                    self.presets.apply(slot.preset_id)
                except Exception as exc:  # noqa: BLE001  (missing preset, invalid combination...)
                    self.store.mark(slot.id, today, f"Not started: couldn't apply the preset ({exc})")
                    done.append(f"{slot.name}: preset problem")
                    continue
            plan = SessionPlan(label=slot.name, ends_at=ends_at, profit_target_money=slot.profit_target_money,
                               close_positions_at_end=slot.close_positions_at_end, source="schedule")
            try:
                await self.engine.start(plan)
            except Exception as exc:  # noqa: BLE001  (ScalperStartError: MT5 not ready, live account, ...)
                # Not final: retry on the next pass until the grace window closes.
                self.store.mark(slot.id, None, f"Could not start yet: {exc}")
                done.append(f"{slot.name}: could not start ({exc})")
                continue
            stamp = datetime.fromtimestamp(now, zone).strftime("%H:%M")
            self.store.mark(slot.id, today, f"Started at {stamp}" + (f", ends {datetime.fromtimestamp(ends_at, zone).strftime('%H:%M')}" if ends_at else ""))
            done.append(f"{slot.name}: started")
        return done

    # ------------------------------------------------------------------ background loop
    async def _loop(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001  (the scheduler must never die)
                logger.exception("scalper scheduler pass failed")
            await asyncio.sleep(POLL_SECONDS)

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop(), name="scalper-scheduler")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None

    # ------------------------------------------------------------------ for the dashboard
    def view(self) -> dict[str, Any]:
        cfg = self.store.get()
        now = self._clock()
        try:
            is_open, why = self._market_open(now)
        except Exception:  # noqa: BLE001
            is_open, why = None, None
        zone = ZoneInfo(cfg.timezone)
        slots = []
        for s in cfg.slots:
            d = s.model_dump()
            d["next_run"] = next_run(cfg, s, now) if cfg.enabled and s.enabled else None
            slots.append(d)
        return {"enabled": cfg.enabled, "timezone": cfg.timezone, "slots": slots,
                "now_local": datetime.fromtimestamp(now, zone).isoformat(), "market_open": is_open, "market_reason": why,
                "grace_minutes": GRACE_SECONDS // 60}


schedule_store = ScheduleStore()
scalper_scheduler = Scheduler(schedule_store)
