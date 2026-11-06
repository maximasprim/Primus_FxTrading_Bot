"""Pre-flight checklist for the dashboard: tells the user exactly what's wrong
with their MT5 setup instead of failing silently when they press Start."""
from __future__ import annotations

import time
from typing import Any

from app.scalper.config import ScalperConfig
from app.scalper.gateway import ScalperGateway


def _item(name: str, ok: bool, detail: str, severity: str = "error") -> dict[str, Any]:
    return {"name": name, "ok": ok, "detail": detail, "severity": "ok" if ok else severity}


async def run_diagnostics(cfg: ScalperConfig, gateway: ScalperGateway) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    try:
        a = await gateway.account()
    except Exception as exc:  # noqa: BLE001
        return {"ready": False, "backend": cfg.backend,
                "checks": [_item("Connection", False, f"{type(exc).__name__}: {exc}")]}

    checks.append(_item("Connected to broker", a.connected, a.error or f"{a.server} / account {a.login}"))
    if not a.connected:
        return {"ready": False, "backend": cfg.backend, "checks": checks}

    checks.append(_item(
        "Account type", a.is_demo or cfg.allow_live_account,
        "DEMO account" if a.is_demo else
        ("REAL-money account (live trading allowed in settings)" if cfg.allow_live_account
         else "REAL-money account - blocked until you enable 'Allow live account'"),
    ))
    checks.append(_item("Algo Trading button (terminal)", a.algo_trading_enabled,
                        "ON" if a.algo_trading_enabled else "OFF - click 'Algo Trading' in the MT5 toolbar so it turns green"))
    checks.append(_item("Account allows automated trading", a.trade_allowed,
                        "yes" if a.trade_allowed else "no - broker/account has trading by EAs/API disabled"))
    checks.append(_item(
        "Hedging account", a.hedging,
        "hedging - multiple trades per symbol allowed" if a.hedging else
        "NETTING account - the broker merges trades on one symbol, so the scalper will use 1 position per instrument",
        severity="warning",
    ))
    cents = a.currency.upper() in {"USC", "EUC", "GBC", "AUC", "CAC", "CHC", "JPC", "NZC"}
    checks.append(_item("Balance", a.equity > 0, f"equity {a.equity:.2f} {a.currency}"
                        + (f" (cents: that is {a.equity / 100:.2f} in whole currency)" if cents else "")))

    for instrument in cfg.instruments:
        try:
            spec = await gateway.spec(instrument)
            tick = await gateway.tick(instrument)
            if tick is None:
                checks.append(_item(f"{instrument} price", False, f"no price for {spec.symbol} (market closed?)", "warning"))
                continue
            age = abs(time.time() - tick.time)
            spread = (tick.ask - tick.bid) / spec.pip_size
            fresh = age < 120
            checks.append(_item(
                f"{instrument} ({spec.symbol})", spec.trade_allowed and fresh,
                f"spread {spread:.1f} pips, min lot {spec.volume_min}, tick age {age:.0f}s"
                + ("" if spec.trade_allowed else " - trading disabled for this symbol")
                + ("" if fresh else " - price is stale (market closed or clock/offset issue)"),
                severity="warning",
            ))
        except Exception as exc:  # noqa: BLE001
            checks.append(_item(f"{instrument}", False, str(exc)))
    if cfg.instruments and a.equity > 0:  # how big is one pip compared with this account? (works whatever the units)
        try:
            inst = cfg.instruments[0]
            spec, pv = await gateway.spec(inst), await gateway.pip_value(inst)
            if pv:
                per_pip = pv * spec.volume_min
                pct = per_pip / a.equity * 100
                stop10 = pct * 10
                checks.append(_item(
                    f"Pip value ({inst}, {spec.volume_min} lot)", stop10 <= 3.0,
                    f"1 pip = {per_pip:.4f} {a.currency} = {pct:.3f}% of your equity; a 10-pip stop-loss would lose about {stop10:.1f}%."
                    + ("" if stop10 <= 3.0 else " Too large for this balance even at the smallest lot: expect the risk cap to block most trades."),
                    severity="warning"))
        except Exception as exc:  # noqa: BLE001
            checks.append(_item("Pip value", False, f"could not measure: {exc}", "warning"))
    if cfg.instruments:  # can orders actually be placed? (MT5 order_check: validates, trades NOTHING)
        try:
            res = await gateway.check_order(cfg.instruments[0])
        except Exception as exc:  # noqa: BLE001
            res = {"ok": False, "detail": f"{type(exc).__name__}: {exc}"}
        if res is not None:
            checks.append(_item("Order placement (dry run, nothing is traded)", bool(res["ok"]), res["detail"],
                                "warning" if res.get("market_closed") else "error"))
    ready = all(c["ok"] or c["severity"] == "warning" for c in checks)
    return {"ready": ready, "backend": cfg.backend, "checks": checks}
