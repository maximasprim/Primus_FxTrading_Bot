"""Scalper control API. Mounted behind the API-key dependency in main.py."""
import shutil
from typing import Any

from fastapi import APIRouter, Body, HTTPException, Query
from pydantic import BaseModel, ValidationError

from app.config import get_settings
from app.scalper.backtest import jobs as bt_jobs
from app.scalper.backtest.data import DataError
from app.scalper.config import ScalperConfig, scalper_config_store
from app.scalper.diagnostics import run_diagnostics
from app.scalper.engine import ScalperStartError, _default_gateway_factory, scalper_engine

router = APIRouter()


class StopBody(BaseModel):
    close_positions: bool = False


def _validation_detail(exc: ValidationError) -> list[dict[str, str]]:
    return [{"field": ".".join(str(p) for p in e["loc"]) or "config", "message": str(e["msg"]).removeprefix("Value error, ")}
            for e in exc.errors()]


@router.get("/status")
async def get_status() -> dict[str, Any]:
    return scalper_engine.status()


@router.get("/config")
async def get_config() -> dict[str, Any]:
    settings = get_settings()
    return {
        "config": scalper_config_store.get().model_dump(),
        "allowed_instruments": sorted(set(settings.initial_instruments) | set(settings.future_instruments)),
        "running": scalper_engine.state == "RUNNING",
    }


@router.put("/config")
async def update_config(
    changes: dict[str, Any] = Body(...),
    acknowledge_live_risk: bool = Query(False, description="Required when turning on allow_live_account."),
) -> dict[str, Any]:
    current = scalper_config_store.get()
    if changes.get("allow_live_account") and not current.allow_live_account and not acknowledge_live_risk:
        raise HTTPException(status_code=400, detail=[{"field": "allow_live_account",
                            "message": "Enabling live trading needs explicit acknowledgement of the risk."}])
    if scalper_engine.state == "RUNNING" and "backend" in changes and changes["backend"] != current.backend:
        raise HTTPException(status_code=409, detail=[{"field": "backend",
                            "message": "Stop the scalper before switching between simulation and MT5."}])
    try:
        new = scalper_config_store.update(changes)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=_validation_detail(exc)) from exc
    return {"config": new.model_dump(), "applied_live": scalper_engine.state == "RUNNING"}


@router.post("/config/reset")
async def reset_config() -> dict[str, Any]:
    return {"config": scalper_config_store.reset().model_dump()}


@router.post("/start")
async def start() -> dict[str, Any]:
    try:
        return await scalper_engine.start()
    except ScalperStartError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"Could not start: {type(exc).__name__}: {exc}") from exc


@router.post("/stop")
async def stop(body: StopBody | None = None) -> dict[str, Any]:
    return await scalper_engine.stop(close_positions=bool(body and body.close_positions))


@router.post("/flatten")
async def flatten() -> dict[str, Any]:
    try:
        n = await scalper_engine.flatten()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"Could not flatten: {type(exc).__name__}: {exc}") from exc
    return {"closed": n, **scalper_engine.status()}


@router.get("/diagnostics")
async def diagnostics() -> dict[str, Any]:
    cfg: ScalperConfig = scalper_config_store.get()
    gateway = scalper_engine.gateway if scalper_engine.state == "RUNNING" and scalper_engine.gateway else _default_gateway_factory(cfg)
    return await run_diagnostics(cfg, gateway)


# ----------------------------------------------------------------------------- backtests
@router.post("/backtest")
async def start_backtest(req: bt_jobs.BacktestRequest) -> dict[str, Any]:
    """Replay the REAL engine over history, using the currently saved scalper settings (plus optional overrides)."""
    if bt_jobs.running_job_id():
        raise HTTPException(status_code=409, detail="A backtest is already running. Wait for it or cancel it first.")
    try:
        job_id, cfg = bt_jobs.create_job(req, scalper_config_store.get())
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=_validation_detail(exc)) from exc
    try:
        data_info = await bt_jobs.prepare_data(job_id, req, cfg)
    except DataError as exc:
        shutil.rmtree(bt_jobs.jobs_root() / job_id, ignore_errors=True)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    bt_jobs.spawn_workers(job_id)
    return {"job_id": job_id, "data": data_info}


@router.get("/backtest")
async def list_backtests() -> list[dict[str, Any]]:
    return bt_jobs.list_jobs()


@router.get("/backtest/{job_id}")
async def get_backtest(job_id: str) -> dict[str, Any]:
    try:
        return bt_jobs.job_status(job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="No such backtest.") from exc


@router.post("/backtest/{job_id}/cancel")
async def cancel_backtest(job_id: str) -> dict[str, Any]:
    try:
        bt_jobs.cancel_job(job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="No such backtest.") from exc
    return bt_jobs.job_status(job_id)
