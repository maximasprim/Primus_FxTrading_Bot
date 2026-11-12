"""
Trade history ("journal") for the scalper.

Every trade gets two lines in an append-only file (`scalper_history.jsonl`): one when it OPENS (what the bot saw and
which rules it used) and one when it CLOSES (what happened and why). Reading them back and joining on the ticket gives
a complete, plain-English record of each trade. Because it is append-only, a crash can never corrupt older history.

Nothing in here may ever stop the bot from trading: every write is wrapped so a disk problem only logs a warning.
"""
from __future__ import annotations

import csv
import io
import json
import logging
import math
import threading
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

# code -> text shown to the user
EXIT_LABELS: dict[str, str] = {
    "take_profit": "Hit its take-profit",
    "stop_loss": "Hit its stop-loss",
    "trailing_stop": "Closed by the trailing / break-even stop",
    "time_stop": "Held too long (time limit)",
    "profit_target": "Reached the per-trade profit target",
    "basket": "Basket take-profit closed everything",
    "manual": "Closed by you",
    "user_stop": "Closed when you pressed Stop & close all",
    "halt_rule": "Closed because a safety rule stopped the bot",
    "session_end": "Closed at the end of the scheduled session",
    "session_target": "Closed when the session profit target was reached",
    "closed_while_stopped": "Closed while the bot was off (take-profit, stop-loss or by hand)",
    "broker": "Closed by the broker (take-profit, stop-loss or by hand)",
    "unknown": "Outcome was not recorded",
}
_UNKNOWN_AFTER_SECONDS = 3 * 86400  # an "open" trade this old with no close record is shown as "not recorded"


def infer_exit_reason(side: str, entry: float, sl: float | None, tp: float | None, exit_price: float | None,
                      pip_size: float, original_sl: float | None = None) -> str:
    """Work out WHY a trade the broker closed ended, from where it ended relative to its stop and target."""
    if not exit_price:
        return "broker"
    refs = [abs(entry - x) for x in (sl, tp) if x]
    tol = max(pip_size * 1.5, 0.12 * min(refs)) if refs else pip_size * 1.5
    d_tp = abs(exit_price - tp) if tp else None
    d_sl = abs(exit_price - sl) if sl else None
    if d_tp is not None and d_tp <= tol and (d_sl is None or d_tp <= d_sl):
        return "take_profit"
    if d_sl is not None and d_sl <= tol:
        moved = original_sl is not None and sl is not None and abs(sl - original_sl) > pip_size * 0.2
        return "trailing_stop" if moved else "stop_loss"
    return "broker"


def _round_price(x: Any, pip: float | None) -> Any:
    """Show prices at the pair's precision (one digit finer than a pip), e.g. 5 decimals on EUR/USD."""
    if x is None or not pip:
        return x
    return round(float(x), max(0, round(-math.log10(pip)) + 1))


def _fmt_duration(seconds: float | None) -> str:
    if seconds is None:
        return "?"
    s = int(max(seconds, 0))
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m}m {s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m"


class TradeJournal:
    def __init__(self, path: Path | None = None) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._awaiting: set[str] | None = None    # tickets whose latest open line has no close line yet (lazy)

    # ------------------------------------------------------------------ file
    @property
    def path(self) -> Path:
        if self._path is None:
            from app.config import get_settings
            self._path = Path(get_settings().scalper_state_dir) / "scalper_history.jsonl"
        return self._path

    def _append(self, rec: dict[str, Any]) -> None:
        try:
            with self._lock:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(rec, default=str) + "\n")
        except OSError as exc:
            logger.warning("Could not write trade history: %s", exc)

    def _read_lines(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        try:
            with self._lock, self.path.open(encoding="utf-8") as fh:
                for line in fh:
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue            # a half-written last line must never break reading
                    if isinstance(rec, dict) and rec.get("ticket") is not None:
                        out.append(rec)
        except OSError:
            pass
        return out

    def _instances(self) -> list[dict[str, Any]]:
        """Trades in the order they opened, each {"open": line, "close": line|None}.

        Joined by walking the file in order: a close line belongs to the latest still-open line with that ticket.
        (Sequence, not ticket alone, because the simulator reuses ticket numbers after a restart.)
        """
        out: list[dict[str, Any]] = []
        latest: dict[str, dict[str, Any]] = {}
        for r in self._read_lines():
            ticket = str(r["ticket"])
            if r.get("type") == "open":
                inst = {"open": r, "close": None}
                out.append(inst)
                latest[ticket] = inst
            elif r.get("type") == "close":
                inst = latest.get(ticket)
                if inst is not None and inst["close"] is None:
                    inst["close"] = r
        return out

    def _awaiting_set(self) -> set[str]:
        if self._awaiting is None:
            self._awaiting = {str(i["open"]["ticket"]) for i in self._instances() if i["close"] is None}
        return self._awaiting

    # ------------------------------------------------------------------ writing
    def record_open(self, **fields: Any) -> None:
        self._awaiting_set().add(str(fields["ticket"]))
        self._append({"type": "open", "ts": time.time(), **fields, "ticket": str(fields["ticket"])})

    def record_close(self, ticket: str, **fields: Any) -> bool:
        """Write the close line once per ticket. Returns False if it was already recorded."""
        ticket = str(ticket)
        awaiting = self._awaiting_set()
        if ticket not in awaiting:          # already closed, or the bot never saw it open
            return False
        awaiting.discard(ticket)
        code = fields.get("reason") or "unknown"
        self._append({"type": "close", "ts": time.time(), "ticket": ticket,
                      "reason_label": EXIT_LABELS.get(code, code), **fields})
        return True

    # ------------------------------------------------------------------ reading
    def open_record(self, ticket: str) -> dict[str, Any] | None:
        """The latest open line for this ticket (used to recover a trade's rules after a restart)."""
        ticket = str(ticket)
        found = None
        for inst in self._instances():
            if str(inst["open"]["ticket"]) == ticket:
                found = inst["open"]
        return found

    def unclosed(self) -> list[dict[str, Any]]:
        """Open lines that have no close line yet."""
        return [i["open"] for i in self._instances() if i["close"] is None]

    def trades(self, now: float | None = None) -> list[dict[str, Any]]:
        """All trades, newest first, with the open and close lines joined and plain-English fields added."""
        now = time.time() if now is None else now
        out = [_build_trade(i["open"], i["close"], now) for i in self._instances()]
        out.sort(key=lambda t: t["opened_at"] or 0, reverse=True)
        return out


def _build_trade(o: dict[str, Any], c: dict[str, Any] | None, now: float) -> dict[str, Any]:
    pip = float(o.get("pip_size") or 0.0) or None
    side, entry = o.get("side"), o.get("entry")
    t: dict[str, Any] = {k: o.get(k) for k in (
        "ticket", "instrument", "side", "volume", "entry", "sl", "tp", "spread_pips", "score", "reasons", "entry_style",
        "preset", "preset_id", "session_id", "account", "demo", "currency", "planned_tp_pips", "planned_sl_pips", "rules", "plan")}
    for k in ("entry", "sl", "tp"):
        t[k] = _round_price(t[k], pip)
    entry = t["entry"]
    t["opened_at"] = o.get("opened_at")
    t["status"], t["closed_at"], t["exit"], t["pnl"] = "open", None, None, None
    t["pips"], t["duration_seconds"], t["result"] = None, None, None
    t["exit_reason"], t["exit_reason_label"] = None, None
    if c is not None:
        t["status"] = "closed"
        t["closed_at"], t["exit"], t["pnl"] = c.get("closed_at"), _round_price(c.get("exit"), pip), c.get("pnl")
        t["exit_reason"] = c.get("reason") or "unknown"
        t["exit_reason_label"] = c.get("reason_label") or EXIT_LABELS.get(t["exit_reason"], t["exit_reason"])
        t["pnl_estimated"] = bool(c.get("pnl_estimated"))
        if t["closed_at"] and t["opened_at"]:
            t["duration_seconds"] = max(0.0, float(t["closed_at"]) - float(t["opened_at"]))
        if t["exit"] and entry and pip and side in ("BUY", "SELL"):
            move = (float(t["exit"]) - float(entry)) if side == "BUY" else (float(entry) - float(t["exit"]))
            t["pips"] = round(move / pip, 1)
        pnl = t["pnl"]
        if pnl is not None:
            t["result"] = "win" if pnl > 0.005 else "loss" if pnl < -0.005 else "flat"
    elif o.get("opened_at") and now - float(o["opened_at"]) > _UNKNOWN_AFTER_SECONDS:
        t["status"], t["exit_reason"], t["exit_reason_label"] = "unknown", "unknown", EXIT_LABELS["unknown"]
    t["summary"] = _summary(t)
    return t


def _summary(t: dict[str, Any]) -> str:
    cur = t.get("currency") or ""
    verb = "Bought" if t["side"] == "BUY" else "Sold"
    head = f"{verb} {t['volume']} lots of {t['instrument']} at {t['entry']}."
    if t["status"] == "open":
        return head + " Still open."
    if t["status"] == "unknown":
        return head + " The bot has no record of how it ended (check MT5's history)."
    tail = f" {t['exit_reason_label']} after {_fmt_duration(t['duration_seconds'])}"
    if t["exit"]:
        tail += f" at {t['exit']}"
    tail += "."
    if t["pnl"] is not None:
        word = {"win": "Won", "loss": "Lost", "flat": "Broke even:"}[t["result"]]
        amount = f"{abs(t['pnl']):.2f} {cur}".strip() if t["result"] != "flat" else f"{t['pnl']:+.2f} {cur}".strip()
        pips = f" ({t['pips']:+.1f} pips)" if t["pips"] is not None else ""
        tail += f" {word} {amount}{pips}."
    return head + tail


# ---------------------------------------------------------------------- filtering & statistics
def filter_trades(trades: list[dict[str, Any]], *, since: float | None = None, until: float | None = None,
                  instrument: str | None = None, result: str | None = None, preset: str | None = None,
                  reason: str | None = None, status: str | None = None) -> list[dict[str, Any]]:
    out = []
    for t in trades:
        ts = t.get("opened_at") or 0
        if since is not None and ts < since:
            continue
        if until is not None and ts > until:
            continue
        if instrument and t.get("instrument") != instrument:
            continue
        if result and t.get("result") != result:
            continue
        if preset and (t.get("preset") or "Custom") != preset:
            continue
        if reason and t.get("exit_reason") != reason:
            continue
        if status and t.get("status") != status:
            continue
        out.append(t)
    return out


def _agg(group: list[dict[str, Any]]) -> dict[str, Any]:
    pn = [t["pnl"] for t in group if t.get("pnl") is not None]
    wins = [x for x in pn if x > 0.005]
    return {"trades": len(group), "net": round(sum(pn), 2),
            "win_rate": round(100 * len(wins) / len(pn), 1) if pn else None}


def summarize(trades: list[dict[str, Any]], tz: str = "UTC") -> dict[str, Any]:
    closed = [t for t in trades if t["status"] == "closed" and t.get("pnl") is not None]
    open_n = sum(1 for t in trades if t["status"] == "open")
    pnls = [t["pnl"] for t in closed]
    wins = [p for p in pnls if p > 0.005]
    losses = [p for p in pnls if p < -0.005]
    gross_win, gross_loss = sum(wins), -sum(losses)
    chrono = sorted(closed, key=lambda t: t.get("closed_at") or 0)
    streak = worst = 0
    for t in chrono:
        streak = streak + 1 if t["result"] == "loss" else 0
        worst = max(worst, streak)
    durations = [t["duration_seconds"] for t in closed if t.get("duration_seconds") is not None]
    try:
        zone = ZoneInfo(tz)
    except Exception:  # noqa: BLE001
        zone = ZoneInfo("UTC")
    by_day: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_inst: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_reason: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_preset: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for t in closed:
        day = datetime.fromtimestamp(t.get("closed_at") or t["opened_at"], zone).strftime("%Y-%m-%d")
        by_day[day].append(t)
        by_inst[t["instrument"]].append(t)
        by_reason[t["exit_reason"]].append(t)
        by_preset[t.get("preset") or "Custom"].append(t)
    return {
        "closed": len(closed), "open": open_n,
        "wins": len(wins), "losses": len(losses), "flat": len(closed) - len(wins) - len(losses),
        "win_rate": round(100 * len(wins) / len(closed), 1) if closed else None,
        "net": round(sum(pnls), 2),
        "gross_win": round(gross_win, 2), "gross_loss": round(gross_loss, 2),
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else None,
        "avg_win": round(gross_win / len(wins), 2) if wins else None,
        "avg_loss": round(-gross_loss / len(losses), 2) if losses else None,
        "expectancy": round(sum(pnls) / len(pnls), 3) if pnls else None,
        "best": round(max(pnls), 2) if pnls else None, "worst": round(min(pnls), 2) if pnls else None,
        "avg_duration_seconds": round(sum(durations) / len(durations)) if durations else None,
        "longest_losing_streak": worst,
        "by_day": [{"day": d, **_agg(g)} for d, g in sorted(by_day.items())],
        "by_instrument": [{"instrument": k, **_agg(g)} for k, g in sorted(by_inst.items())],
        "by_reason": [{"reason": k, "label": EXIT_LABELS.get(k, k), **_agg(g)} for k, g in
                      sorted(by_reason.items(), key=lambda kv: -len(kv[1]))],
        "by_preset": [{"preset": k, **_agg(g)} for k, g in sorted(by_preset.items())],
        "exit_counts": dict(Counter(t["exit_reason"] for t in closed)),
    }


CSV_COLUMNS = ["opened_at_utc", "closed_at_utc", "ticket", "instrument", "side", "lots", "entry", "exit", "stop_loss",
               "take_profit", "pips", "pnl", "currency", "result", "how_it_ended", "held_seconds", "signal_score",
               "entry_style", "preset", "spread_pips", "summary"]


def to_csv(trades: list[dict[str, Any]]) -> str:
    def iso(ts: Any) -> str:
        return datetime.fromtimestamp(float(ts), timezone.utc).strftime("%Y-%m-%d %H:%M:%S") if ts else ""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(CSV_COLUMNS)
    for t in trades:
        w.writerow([iso(t["opened_at"]), iso(t["closed_at"]), t["ticket"], t["instrument"], t["side"], t["volume"],
                    t["entry"], t["exit"] or "", t["sl"] or "", t["tp"] or "", "" if t["pips"] is None else t["pips"],
                    "" if t["pnl"] is None else round(t["pnl"], 2), t.get("currency") or "", t["result"] or t["status"],
                    t["exit_reason_label"] or "", "" if t["duration_seconds"] is None else int(t["duration_seconds"]),
                    t["score"] if t["score"] is not None else "", t["entry_style"] or "", t.get("preset") or "Custom",
                    t["spread_pips"] if t["spread_pips"] is not None else "", t["summary"]])
    return buf.getvalue()


journal = TradeJournal()
