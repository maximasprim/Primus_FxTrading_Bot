"""Scalper extras: trade history, setting presets, scheduled sessions and "start with limits".

A separate router (same /api/scalper prefix) so the original scalper routes stay exactly as they were.
Mounted behind the API-key dependency in main.py.
"""
from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Body, HTTPException, Query, Response
from pydantic import BaseModel, Field, ValidationError

from app.scalper import journal as journal_mod
from app.scalper.config import scalper_config_store
from app.scalper.engine import ScalperStartError, scalper_engine
from app.scalper.models import SessionPlan
from app.scalper.presets import PRESET_KEYS, PresetError, preset_store
from app.scalper.schedule import scalper_scheduler, schedule_store

router = APIRouter()


def _validation_detail(exc: ValidationError) -> list[dict[str, str]]:
    return [{"field": ".".join(str(p) for p in e["loc"]) or "config", "message": str(e["msg"]).removeprefix("Value error, ")}
            for e in exc.errors()]


# ----------------------------------------------------------------------------- history
def _filtered(since, until, instrument, result, preset, reason, status):
    return journal_mod.filter_trades(journal_mod.journal.trades(), since=since, until=until, instrument=instrument,
                                     result=result, preset=preset, reason=reason, status=status)


@router.get("/history")
async def history(
    limit: int = Query(100, ge=1, le=1000), offset: int = Query(0, ge=0),
    since: float | None = None, until: float | None = None, instrument: str | None = None,
    result: str | None = Query(None, pattern="^(win|loss|flat)$"), preset: str | None = None,
    reason: str | None = None, status: str | None = Query(None, pattern="^(open|closed|unknown)$"),
    tz: str = "UTC",
) -> dict[str, Any]:
    """Newest-first trades, each with a plain-English summary, plus totals for the same filter."""
    trades = _filtered(since, until, instrument, result, preset, reason, status)
    return {"total": len(trades), "trades": trades[offset: offset + limit],
            "summary": journal_mod.summarize(trades, tz),
            "filters": {"instruments": sorted({t["instrument"] for t in journal_mod.journal.trades()}),
                        "presets": sorted({t.get("preset") or "Custom" for t in journal_mod.journal.trades()}),
                        "reasons": journal_mod.EXIT_LABELS}}


@router.get("/history/export")
async def history_export(since: float | None = None, until: float | None = None, instrument: str | None = None,
                         result: str | None = Query(None, pattern="^(win|loss|flat)$"), preset: str | None = None,
                         reason: str | None = None, status: str | None = Query(None, pattern="^(open|closed|unknown)$")) -> Response:
    csv_text = journal_mod.to_csv(_filtered(since, until, instrument, result, preset, reason, status))
    return Response(content=csv_text, media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="scalper_trades_{time.strftime("%Y%m%d")}.csv"'})


# ----------------------------------------------------------------------------- presets
class PresetBody(BaseModel):
    name: str = Field(..., min_length=1, max_length=60)
    description: str = Field("", max_length=300)
    values: dict[str, Any] | None = None     # omitted = "save my current settings"


class PresetPatch(BaseModel):
    name: str | None = Field(None, max_length=60)
    description: str | None = Field(None, max_length=300)
    values: dict[str, Any] | None = None
    use_current_settings: bool = False


@router.get("/presets")
async def list_presets() -> dict[str, Any]:
    return {"presets": preset_store.list(), "active": preset_store.active(), "keys": PRESET_KEYS}


@router.post("/presets")
async def create_preset(body: PresetBody) -> dict[str, Any]:
    try:
        preset = preset_store.create(body.name, body.description, body.values)
    except PresetError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"preset": preset}


@router.put("/presets/{preset_id}")
async def update_preset(preset_id: str, body: PresetPatch) -> dict[str, Any]:
    values = body.values
    if body.use_current_settings:
        cur = scalper_config_store.get().model_dump()
        values = {k: cur[k] for k in PRESET_KEYS}
    try:
        return {"preset": preset_store.update(preset_id, body.name, body.description, values)}
    except PresetError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="No such preset.") from exc


@router.delete("/presets/{preset_id}")
async def delete_preset(preset_id: str) -> dict[str, Any]:
    try:
        preset_store.delete(preset_id)
    except PresetError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="No such preset.") from exc
    return {"deleted": preset_id, "active": preset_store.active()}


@router.post("/presets/{preset_id}/apply")
async def apply_preset(preset_id: str) -> dict[str, Any]:
    """Apply a preset to the live settings. Open trades keep the exit rules they were opened with."""
    try:
        res = preset_store.apply(preset_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="No such preset.") from exc
    except PresetError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=_validation_detail(exc)) from exc
    return {**res, "active": preset_store.active(), "applied_live": scalper_engine.state == "RUNNING"}


# ----------------------------------------------------------------------------- schedule
@router.get("/schedule")
async def get_schedule() -> dict[str, Any]:
    return {**scalper_scheduler.view(), "plan": scalper_engine.status().get("plan")}


@router.put("/schedule")
async def put_schedule(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    try:
        schedule_store.replace(body)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=_validation_detail(exc)) from exc
    return {**scalper_scheduler.view(), "plan": scalper_engine.status().get("plan")}


class TimedStart(BaseModel):
    duration_minutes: int = Field(0, ge=0, le=1440)          # 0 = no time limit
    profit_target_money: float = Field(0.0, ge=0)
    close_positions_at_end: bool = True


@router.post("/start-timed")
async def start_timed(body: TimedStart) -> dict[str, Any]:
    """Start now, with an optional time limit and/or profit target for this run only."""
    plan = SessionPlan(label="Manual run", source="manual",
                       ends_at=time.time() + body.duration_minutes * 60 if body.duration_minutes else None,
                       profit_target_money=body.profit_target_money, close_positions_at_end=body.close_positions_at_end)
    try:
        return await scalper_engine.start(plan)
    except ScalperStartError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"Could not start: {type(exc).__name__}: {exc}") from exc
