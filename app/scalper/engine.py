"""
The scalper engine: one asyncio loop that
  1. reads open positions, 2. enforces the bot-level limits, 3. manages exits
  (profit targets, break-even, trailing, time stop, basket take-profit),
  4. scans instruments for entries and opens up to N trades per signal.

Safety properties
-----------------
* Every order is sent WITH a broker-side stop-loss and take-profit, so if this
  process dies the positions are still protected.
* STOPPED at every process start; never auto-resumes.
* Refuses to start on a real-money account unless `allow_live_account` is on,
  when the terminal's Algo Trading is off, or when the account isn't connected.
* Respects the Control Center's emergency stop (no new entries).
* Only touches positions carrying the scalper's magic number.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

from app.config import get_settings
from app.scalper.config import ScalperConfig, scalper_config_store
from app.scalper.gateway import ScalperGateway
from app.scalper.guard import SessionGuard, consecutive_losses
from app.scalper.journal import TradeJournal, infer_exit_reason, journal as default_journal
from app.scalper.models import AccountSnapshot, Deal, ScalpPosition, SessionPlan, SymbolSpec
from app.scalper.signals import compute_signal
from app.scalper.styles import STYLE_FUNCS

logger = logging.getLogger(__name__)

STOPPED, RUNNING, HALTED = "STOPPED", "RUNNING", "HALTED"
_STALE_TICK_SECONDS = 120
_MAX_CONSECUTIVE_ERRORS = 10
_MAX_REJECTS_IN_A_ROW = 8      # broker refusing orders: stop the bot instead of retrying forever
_BACKOFF_FIRST_SECONDS = 5.0   # wait after a rejected order on a pair; doubles each time, capped below
_BACKOFF_MAX_SECONDS = 120.0
_RULE_KEYS = ("max_hold_seconds", "trade_profit_target_money", "breakeven_after_pips", "trailing_enabled",
              "trailing_start_pips", "trailing_distance_pips", "trailing_step_pips")


class ScalperStartError(RuntimeError):
    pass


def _floor_to_step(value: float, step: float) -> float:
    return round(math.floor(value / step + 1e-9) * step, 8)


class ScalperEngine:
    def __init__(
        self,
        gateway_factory: Callable[[ScalperConfig], ScalperGateway] | None = None,
        config_store=scalper_config_store,
        clock: Callable[[], float] = time.time,
        log_path: Path | None = None,
        signal_fn: Callable | None = None,
        persist_log: bool = True,
        journal: TradeJournal | None = None,
        preset_label_fn: Callable[[ScalperConfig], tuple[str, str | None]] | None = None,
    ) -> None:
        self._gateway_factory = gateway_factory or _default_gateway_factory
        self._store = config_store
        self._clock = clock
        self._log_path = log_path
        self._signal_fn = signal_fn          # None -> app.scalper.signals.compute_signal
        self._persist_log = persist_log      # False for backtests (no disk writes)
        self.observer: Callable[[str, dict[str, Any]], None] | None = None  # e.g. backtester records entry scores
        self.state = STOPPED
        self.halt_reason: str | None = None
        self.last_error: str | None = None
        self.gateway: ScalperGateway | None = None
        self.guard: SessionGuard | None = None
        self.account: AccountSnapshot | None = None
        self.events: deque[dict[str, Any]] = deque(maxlen=200)
        self.signals: dict[str, dict[str, Any]] = {}
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._known: dict[str, ScalpPosition] = {}
        self._last_modify: dict[str, float] = {}
        self._deals: list[Deal] = []
        self._errors = 0
        self._reject_streak = 0
        self._inst_fails: dict[str, int] = {}
        self._backoff_until: dict[str, float] = {}
        self._last_reject: tuple[str, float] = ("", 0.0)
        self._started_at = 0.0
        self._opened_total = 0
        self._net = {"realized": 0.0, "floating": 0.0}
        # --- trade history, per-trade rules and optional session limits (all additive) ---
        if not persist_log:
            self.journal: TradeJournal | None = None                      # backtests write nothing
        elif journal is not None:
            self.journal = journal
        elif log_path is not None:
            self.journal = TradeJournal(Path(log_path).parent / "scalper_history.jsonl")
        else:
            self.journal = default_journal
        if preset_label_fn is not None:
            self._label_fn = preset_label_fn
        elif persist_log and log_path is None:
            from app.scalper.presets import preset_store
            self._label_fn = lambda cfg: preset_store.current_label(cfg)
        else:
            self._label_fn = lambda cfg: ("Custom", None)
        self.plan: SessionPlan | None = None
        self.session_end_reason: str | None = None
        self._ending = False
        self._exits: dict[str, dict[str, Any]] = {}       # trades closed but not yet written to the history
        self._trade_rules: dict[str, dict[str, Any]] = {}
        self._trade_meta: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------ logging
    def _event(self, kind: str, message: str, level: str = "info", **data: Any) -> None:
        ev = {"ts": self._clock(), "kind": kind, "level": level, "message": message, **data}
        self.events.append(ev)
        getattr(logger, "warning" if level != "info" else "info")("scalper %s: %s", kind, message)
        if not self._persist_log:
            return
        try:
            path = self._log_path or Path(get_settings().scalper_state_dir) / "scalper_trades.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(ev, default=str) + "\n")
        except OSError:
            pass

    # ------------------------------------------------------------------ lifecycle
    async def start(self, plan: SessionPlan | None = None) -> dict[str, Any]:
        async with self._lock:
            if self.state == RUNNING:
                raise ScalperStartError("The scalper is already running.")
            self.plan, self._ending, self.session_end_reason = plan, False, None
            try:
                await self._begin_session()
            except Exception:
                self.plan = None
                raise
            self._task = asyncio.create_task(self._run(), name="scalper-loop")
            return self.status()

    async def _begin_session(self) -> None:
        """Connect, run the safety checks and open a fresh session (no background task)."""
        cfg = self._store.get()
        gateway = self._gateway_factory(cfg)
        account = await gateway.account()
        problem = self._start_problem(cfg, account)
        if problem:
            await gateway.aclose()
            raise ScalperStartError(problem)
        self.gateway, self.account = gateway, account
        self.new_session(account.equity)
        self._errors = 0
        self._event("start", f"Scalper started on {account.backend} account {account.login} "
                    f"({'DEMO' if account.is_demo else 'LIVE'}), equity {account.equity:.2f} {account.currency}.")
        if self.plan:
            bits = []
            if self.plan.ends_at:
                bits.append("ends " + time.strftime("%H:%M", time.localtime(self.plan.ends_at)))
            if self.plan.profit_target_money > 0:
                bits.append(f"stops at +{self.plan.profit_target_money:.2f} profit")
            self._event("start", f"Session plan '{self.plan.label or self.plan.source}': " + (", ".join(bits) or "no extra limits") + ".")
        await self._reconcile_journal()

    def new_session(self, equity: float) -> None:
        """Reset session counters/limits (used at start, and by the backtester at each new trading day)."""
        now = self._clock()
        self.guard = SessionGuard(equity, now)
        self._started_at = now
        self.halt_reason = self.last_error = None
        self._known, self._deals, self._opened_total = {}, [], 0
        self._net = {"realized": 0.0, "floating": 0.0}
        self._reject_streak, self._inst_fails, self._backoff_until = 0, {}, {}
        self.state = RUNNING

    @staticmethod
    def _start_problem(cfg: ScalperConfig, a: AccountSnapshot) -> str | None:
        if not a.connected:
            return a.error or "Not connected to the broker."
        if not a.is_demo and not cfg.allow_live_account:
            return ("This is a REAL-money account and live trading is switched off. Test on a demo account first, "
                    "or enable 'Allow live account' in the scalper settings if you accept the risk.")
        if not a.algo_trading_enabled:
            return "Algo Trading is OFF in the MT5 terminal. Click the 'Algo Trading' button in the toolbar so it turns green."
        if not a.trade_allowed:
            return "The broker/account doesn't allow automated trading (trade_expert=false). Ask your broker or use another account."
        return None

    async def stop(self, close_positions: bool = False) -> dict[str, Any]:
        async with self._lock:
            await self._stop_locked(close_positions, "Stopped by user.", STOPPED)
            return self.status()

    async def _stop_locked(self, close_positions: bool, reason: str, final_state: str, exit_code: str | None = None) -> None:
        current = asyncio.current_task()
        if self._task and self._task is not current:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._task = None
        if close_positions and self.gateway:
            await self._flatten(reason, exit_code or ("halt_rule" if final_state == HALTED else "user_stop"), force=True)
        self.plan = None
        self.state = final_state
        if final_state == HALTED:
            self.halt_reason = reason
        self._event("stop" if final_state == STOPPED else "halt", reason, "info" if final_state == STOPPED else "warning")

    async def flatten(self, reason: str = "Flatten requested by user.") -> int:
        if not self.gateway:
            gw = self._gateway_factory(self._store.get())
            self.gateway = gw
        return await self._flatten(reason, "manual", force=True)

    async def _flatten(self, reason: str, code: str = "manual", force: bool = False) -> int:
        assert self.gateway is not None
        closed = 0
        for p in await self.gateway.positions():
            res = await self.gateway.close(p.ticket, reason=reason)
            if res.ok:
                closed += 1
                self._note_exit(p, code, reason)
                self._event("close", f"Closed {p.side} {p.instrument} #{p.ticket} ({reason})", ticket=p.ticket, pnl=p.profit)
            else:
                self._event("error", f"Could not close #{p.ticket}: {res.reason}", "error", ticket=p.ticket)
        self._known = {}
        await self._finalize_exits(force=force)
        return closed

    async def _halt(self, reason: str, flatten: bool) -> None:
        async with self._lock:
            if self.state != RUNNING:
                return
            await self._stop_locked(flatten, reason, HALTED)

    # ------------------------------------------------------------------ loop
    async def _run(self) -> None:
        while self.state == RUNNING:
            started = time.monotonic()
            cfg = self._store.get()
            try:
                await self._cycle(cfg)
                self._errors = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self._errors += 1
                self.last_error = f"{type(exc).__name__}: {exc}"
                self._event("error", self.last_error, "error")
                if self._errors >= _MAX_CONSECUTIVE_ERRORS:
                    # Can't see or manage the market. Positions keep their broker-side SL/TP.
                    asyncio.create_task(self._halt(f"Too many consecutive errors; last: {self.last_error}", False))
                    return
            await asyncio.sleep(max(0.05, cfg.loop_interval_ms / 1000 - (time.monotonic() - started)))

    async def _cycle(self, cfg: ScalperConfig) -> None:
        gw, guard = self.gateway, self.guard
        assert gw is not None and guard is not None
        self.account = await gw.account()
        if not self.account.connected:
            raise RuntimeError(self.account.error or "Lost connection to the broker.")

        positions = await gw.positions()
        self._deals = await gw.deals_since(guard.started_at)
        self._report_closed(positions)
        await self._finalize_exits()
        floating = sum(p.profit for p in positions)
        realized = sum(d.profit for d in self._deals)
        self._net = {"realized": realized, "floating": floating}

        reason = guard.halt_reason(cfg, realized + floating, self._deals)
        if reason:
            asyncio.create_task(self._halt(reason, cfg.flatten_on_halt))
            return

        plan = self.plan
        if plan and not self._ending:
            ended = None
            if plan.ends_at and self._clock() >= plan.ends_at:
                ended = ("Scheduled session finished (time is up).", "session_end")
            elif plan.profit_target_money > 0 and realized + floating >= plan.profit_target_money:
                ended = (f"Session profit target reached ({realized + floating:+.2f} >= {plan.profit_target_money:.2f}).", "session_target")
            if ended:
                self._ending = True
                asyncio.create_task(self._end_plan(*ended))
                return

        if cfg.basket_take_profit_money > 0 and positions and floating >= cfg.basket_take_profit_money:
            n = await self._flatten(f"basket take-profit {floating:+.2f}", "basket")
            self._event("basket", f"Basket take-profit hit ({floating:+.2f}); closed {n} position(s).")
            return

        for p in positions:
            await self._manage(p, cfg)

        if self._entries_allowed():
            await self._look_for_entries(cfg, positions)

    async def _end_plan(self, reason: str, code: str) -> None:
        """A session plan's time or profit target was reached: stop cleanly (this is a normal finish, not a fault)."""
        async with self._lock:
            if self.state != RUNNING:
                return
            plan = self.plan
            self.session_end_reason = reason
            await self._stop_locked(bool(plan and plan.close_positions_at_end), reason, STOPPED, exit_code=code)

    def _style_fn(self, cfg: ScalperConfig):
        if self._signal_fn:
            return self._signal_fn
        if cfg.entry_style == "momentum":
            return compute_signal  # resolved from this module at call time (tests patch it)
        return STYLE_FUNCS[cfg.entry_style]

    def _entries_allowed(self) -> bool:
        try:
            from app.core.trading_mode import trading_mode_manager
            return not trading_mode_manager.state.emergency_stop_active
        except Exception:  # noqa: BLE001
            return True

    def _report_closed(self, positions: list[ScalpPosition]) -> None:
        now_open = {p.ticket for p in positions}
        by_ticket = {d.ticket: d for d in self._deals}
        for ticket, old in list(self._known.items()):
            if ticket not in now_open:
                d = by_ticket.get(ticket)
                pnl = f"{d.profit:+.2f}" if d else "?"
                self._event("close", f"{old.side} {old.instrument} #{ticket} closed by broker (TP/SL/stop-out), P&L {pnl}",
                            ticket=ticket, pnl=d.profit if d else None)
                if ticket not in self._exits:
                    self._exits[ticket] = {"code": None, "pos": old, "note": ""}
        self._known = {p.ticket: p for p in positions}

    # ------------------------------------------------------------------ exits
    async def _manage(self, p: ScalpPosition, cfg: ScalperConfig) -> None:
        gw = self.gateway
        assert gw is not None
        spec = await gw.spec(p.instrument)
        tick = await gw.tick(p.instrument)
        if tick is None:
            return
        price = tick.bid if p.side == "BUY" else tick.ask
        pips = ((price - p.price_open) if p.side == "BUY" else (p.price_open - price)) / spec.pip_size
        now = self._clock()

        rules = self._rules_for(p, cfg)
        if rules.trade_profit_target_money > 0 and p.profit >= rules.trade_profit_target_money:
            await self._close(p, f"profit target {p.profit:+.2f}", "profit_target")
            return
        if rules.max_hold_seconds > 0 and now - p.opened_at >= rules.max_hold_seconds:
            await self._close(p, f"time stop after {rules.max_hold_seconds}s ({p.profit:+.2f})", "time_stop")
            return
        if now - self._last_modify.get(p.ticket, 0) < 1.0:
            return

        new_sl: float | None = None
        sign = 1 if p.side == "BUY" else -1
        if rules.breakeven_after_pips > 0 and pips >= rules.breakeven_after_pips:
            be = p.price_open + sign * spec.pip_size * 0.5
            if not p.sl or sign * (be - p.sl) > 0:
                new_sl = be
        if rules.trailing_enabled and pips >= rules.trailing_start_pips:
            trail = price - sign * rules.trailing_distance_pips * spec.pip_size
            current = new_sl if new_sl is not None else p.sl
            if not current or sign * (trail - current) >= rules.trailing_step_pips * spec.pip_size:
                new_sl = trail
        if new_sl is not None:
            min_gap = spec.stops_level_points * spec.point
            if sign * (price - new_sl) > min_gap:
                self._last_modify[p.ticket] = now
                if await gw.modify(p.ticket, round(new_sl, spec.digits), None):
                    self._event("modify", f"Moved stop on {p.instrument} #{p.ticket} to {new_sl:.{spec.digits}f}", ticket=p.ticket)

    async def _close(self, p: ScalpPosition, why: str, code: str = "manual") -> None:
        res = await self.gateway.close(p.ticket, reason=why)  # type: ignore[union-attr]
        if res.ok:
            self._note_exit(p, code, why)
            self._event("close", f"Closed {p.side} {p.instrument} #{p.ticket}: {why}", ticket=p.ticket, pnl=p.profit)
        else:
            self._event("error", f"Close #{p.ticket} failed: {res.reason}", "error", ticket=p.ticket)

    # ------------------------------------------------------------------ entries
    async def _look_for_entries(self, cfg: ScalperConfig, positions: list[ScalpPosition]) -> None:
        gw, guard, account = self.gateway, self.guard, self.account
        assert gw and guard and account
        open_now = list(positions)
        for instrument in cfg.instruments:
            if len(open_now) >= cfg.max_concurrent_positions:
                break
            n_inst = sum(1 for p in open_now if p.instrument == instrument)
            wait = self._backoff_until.get(instrument, 0.0) - self._clock()
            if wait > 0:
                self.signals.setdefault(instrument, {})["blocked"] = f"backing off after a rejected order ({wait:.0f}s)"
                continue
            blocked = guard.entry_block(cfg, instrument, len(open_now), n_inst, self._clock(), account.hedging)
            if blocked:
                self.signals.setdefault(instrument, {})["blocked"] = blocked
                continue
            try:
                spec = await gw.spec(instrument)
                tick = await gw.tick(instrument)
            except Exception as exc:  # noqa: BLE001
                self.signals[instrument] = {"blocked": f"data error: {exc}"}
                continue
            if tick is None or abs(self._clock() - tick.time) > _STALE_TICK_SECONDS or not spec.trade_allowed:
                self.signals[instrument] = {"blocked": "market closed / stale price"}
                continue
            spread = (tick.ask - tick.bid) / spec.pip_size
            candles = await gw.candles(instrument, 120)
            sig = self._style_fn(cfg)(candles, spread, spec.pip_size)
            self.signals[instrument] = {
                "side": sig.side, "score": sig.score, "spread_pips": round(spread, 2),
                "atr_pips": round(sig.atr_pips, 2), "reasons": sig.reasons, "blocked": None,
            }
            if spread > cfg.max_spread_pips:
                self.signals[instrument]["blocked"] = f"spread {spread:.1f} > max {cfg.max_spread_pips}"
                continue
            if sig.side is None or sig.score < cfg.min_signal_score:
                continue

            cap_inst = cfg.max_positions_per_instrument if account.hedging else 1
            n_new = min(cfg.entries_per_signal if account.hedging else 1,
                        cap_inst - n_inst, cfg.max_concurrent_positions - len(open_now))
            for i in range(max(n_new, 0)):
                placed = await self._open_one(cfg, spec, tick, sig, i, open_now, account)
                if placed is None:
                    break
                open_now.append(placed)

    async def _open_one(self, cfg, spec: SymbolSpec, tick, sig, index: int, open_now, account) -> ScalpPosition | None:
        gw, guard = self.gateway, self.guard
        assert gw and guard
        side, pip = sig.side, spec.pip_size
        spread = (tick.ask - tick.bid) / pip
        if cfg.exit_mode == "atr":
            tp_pips, sl_pips = sig.atr_pips * cfg.tp_atr_mult, sig.atr_pips * cfg.sl_atr_mult
        else:
            tp_pips, sl_pips = cfg.take_profit_pips, cfg.stop_loss_pips
        if cfg.stagger_targets:
            tp_pips *= 1 + 0.5 * index
        min_pips = spec.stops_level_points * spec.point * 1.1 / pip + spread
        tp_pips, sl_pips = max(tp_pips, min_pips), max(sl_pips, min_pips)
        if tp_pips < cfg.min_tp_to_spread_ratio * spread:
            self.signals[spec.instrument]["blocked"] = f"target {tp_pips:.1f}p < {cfg.min_tp_to_spread_ratio}x spread {spread:.1f}p"
            return None

        entry = tick.ask if side == "BUY" else tick.bid
        sign = 1 if side == "BUY" else -1
        sl = round(entry - sign * sl_pips * pip, spec.digits)
        tp = round(entry + sign * tp_pips * pip, spec.digits)

        volume, why = await self._volume(cfg, spec, side, entry, sl, account.equity)
        if volume is None:
            self.signals[spec.instrument]["blocked"] = why
            return None

        risk_ok, why = await self._risk_ok(cfg, spec, side, volume, entry, sl, open_now, account.equity)
        if not risk_ok:
            self.signals[spec.instrument]["blocked"] = why
            return None

        res = await gw.send_market(spec.instrument, side, volume, sl, tp)
        if not res.ok:
            self._on_reject(spec.instrument, side, res.reason or "unknown reason")
            self.signals[spec.instrument]["blocked"] = f"order rejected: {res.reason}"
            return None
        self._reject_streak = 0
        self._inst_fails[spec.instrument] = 0
        now = self._clock()
        guard.record_entry(spec.instrument, now)
        self._opened_total += 1
        if self.observer:
            self.observer("open", {"ticket": res.ticket, "instrument": spec.instrument, "side": side,
                                   "score": sig.score, "volume": volume, "time": now})
        self._event("open", f"{side} {volume} {spec.instrument} @ {res.price} SL {sl} TP {tp} (score {sig.score}: {', '.join(sig.reasons[:3])})",
                    ticket=res.ticket, score=sig.score)
        pos = ScalpPosition(res.ticket or "?", spec.instrument, side, volume, res.price or entry, sl, tp, 0.0, now)
        self._known[pos.ticket] = pos
        self._journal_open(cfg, pos, spec, sig, spread, tp_pips, sl_pips, account)
        return pos

    def _on_reject(self, instrument: str, side: str, reason: str) -> None:
        """A rejected order must not be retried every cycle: back off per pair, log once, and stop after a streak."""
        now = self._clock()
        self._reject_streak += 1
        fails = self._inst_fails.get(instrument, 0) + 1
        self._inst_fails[instrument] = fails
        delay = min(_BACKOFF_FIRST_SECONDS * 2 ** (fails - 1), _BACKOFF_MAX_SECONDS)
        self._backoff_until[instrument] = now + delay
        msg = f"Order rejected {side} {instrument}: {reason}"
        last_msg, last_at = self._last_reject
        if msg != last_msg or now - last_at > 30:  # don't repeat the identical line every cycle
            self._event("error", f"{msg} (retrying in {delay:.0f}s)", "warning")
            self._last_reject = (msg, now)
        if self._reject_streak >= _MAX_REJECTS_IN_A_ROW:
            asyncio.create_task(self._halt(
                f"The broker rejected {self._reject_streak} orders in a row, so the bot stopped. Nothing was opened. "
                f"Last reason: {reason}", False))

    async def _volume(self, cfg, spec: SymbolSpec, side, entry, sl, equity) -> tuple[float | None, str]:
        if cfg.lot_mode == "fixed":
            vol = cfg.fixed_lot
        else:
            per_lot = await self.gateway.loss_money(spec.instrument, side, 1.0, entry, sl)  # type: ignore[union-attr]
            if not per_lot:
                return None, "can't compute risk per lot"
            vol = equity * cfg.risk_per_trade_pct / 100.0 / per_lot
        vol = _floor_to_step(min(vol, cfg.max_lot_per_trade, spec.volume_max), spec.volume_step)
        if vol < spec.volume_min - 1e-12:
            # Deliberately NOT rounded up to the broker minimum: that would silently risk more than configured.
            return None, f"lot {vol} is below the broker minimum {spec.volume_min}; skipped (raise risk % or lot size)"
        return vol, ""

    async def _risk_ok(self, cfg, spec, side, volume, entry, sl, open_now, equity) -> tuple[bool, str]:
        gw = self.gateway
        assert gw
        total = await gw.loss_money(spec.instrument, side, volume, entry, sl)
        if total is None:
            return False, "can't compute trade risk"
        for p in open_now:
            if not p.sl:
                return False, f"open position #{p.ticket} has no stop-loss"
            r = await gw.loss_money(p.instrument, p.side, p.volume, p.price_open, p.sl)
            total += r or 0.0
        cap = equity * cfg.max_total_risk_pct / 100.0
        if total > cap:
            return False, f"total open risk {total:.2f} would exceed cap {cap:.2f} ({cfg.max_total_risk_pct}% of equity)"
        return True, ""

    # ------------------------------------------------------------------ history & per-trade rules
    def _rules_for(self, p: ScalpPosition, cfg: ScalperConfig):
        """Exit rules for this trade: the ones it was opened under (default), or the live settings."""
        if not getattr(cfg, "lock_rules_per_trade", True):
            return cfg
        stored = self._trade_rules.get(p.ticket)
        if stored is None:                                   # opened in an earlier run: recover from the history
            stored = {}
            try:
                rec = self.journal.open_record(p.ticket) if self.journal else None
                stored = dict((rec or {}).get("rules") or {})
            except Exception:  # noqa: BLE001
                stored = {}
            self._trade_rules[p.ticket] = stored
        if not stored:
            return cfg
        return SimpleNamespace(**{k: stored.get(k, getattr(cfg, k)) for k in _RULE_KEYS})

    def _journal_open(self, cfg, pos: ScalpPosition, spec: SymbolSpec, sig, spread: float, tp_pips: float, sl_pips: float, account) -> None:
        rules = {k: getattr(cfg, k) for k in _RULE_KEYS}
        self._trade_rules[pos.ticket] = rules
        self._trade_meta[pos.ticket] = {"pip_size": spec.pip_size, "entry": pos.price_open, "sl": pos.sl}
        if not self.journal:
            return
        try:
            label, preset_id = self._label_fn(cfg)
            self.journal.record_open(
                ticket=pos.ticket, opened_at=pos.opened_at, instrument=spec.instrument, side=pos.side, volume=pos.volume,
                entry=pos.price_open, sl=pos.sl, tp=pos.tp, pip_size=spec.pip_size, spread_pips=round(spread, 2),
                score=sig.score, reasons=list(sig.reasons[:6]), entry_style=cfg.entry_style, preset=label, preset_id=preset_id,
                session_id=self._started_at, account=account.login, demo=account.is_demo, currency=account.currency,
                planned_tp_pips=round(tp_pips, 2), planned_sl_pips=round(sl_pips, 2), rules=rules,
                plan=(self.plan.label or self.plan.source) if self.plan else None)
        except Exception:  # noqa: BLE001  (the history must never interrupt trading)
            logger.exception("could not record trade open in the history")

    def _note_exit(self, p: ScalpPosition, code: str, note: str) -> None:
        self._exits[p.ticket] = {"code": code, "pos": p, "note": note}

    def _meta_for(self, ticket: str) -> dict[str, Any]:
        meta = self._trade_meta.get(ticket)
        if meta is None:
            rec = None
            try:
                rec = self.journal.open_record(ticket) if self.journal else None
            except Exception:  # noqa: BLE001
                rec = None
            meta = {"pip_size": (rec or {}).get("pip_size") or 0.0001, "entry": (rec or {}).get("entry"), "sl": (rec or {}).get("sl")}
            self._trade_meta[ticket] = meta
        return meta

    async def _finalize_exits(self, force: bool = False) -> None:
        """Write close lines to the history once the broker's closing deal (real P&L, exit price) is visible."""
        if not self._exits or not self.journal:
            self._exits = {} if not self.journal else self._exits
            return
        try:
            deals = self._deals
            if force and self.gateway is not None:
                deals = await self.gateway.deals_since((self._started_at or self._clock()) - 60)
            by_ticket = {d.ticket: d for d in deals}
            for ticket, info in list(self._exits.items()):
                d, pos = by_ticket.get(ticket), info["pos"]
                if d is None and not force:
                    continue                                  # deal not visible yet; try again next cycle
                meta = self._meta_for(ticket)
                code = info["code"]
                if code is None:
                    code = infer_exit_reason(pos.side, meta.get("entry") or pos.price_open, pos.sl, pos.tp,
                                             d.price if d else None, meta["pip_size"], meta.get("sl"))
                self.journal.record_close(
                    ticket, closed_at=d.time if d else self._clock(), exit=d.price if d else None,
                    pnl=d.profit if d else pos.profit, pnl_estimated=d is None, reason=code, note=info.get("note", ""))
                del self._exits[ticket]
        except Exception:  # noqa: BLE001
            logger.exception("could not finalize history close lines")

    async def _reconcile_journal(self) -> None:
        """Trades that were open when the bot last stopped may have closed since (take-profit, stop-loss, by hand)."""
        if not self.journal or not self.gateway:
            return
        try:
            pending = self.journal.unclosed()
            if not pending:
                return
            open_now = {p.ticket for p in await self.gateway.positions()}
            waiting = [r for r in pending if str(r["ticket"]) not in open_now]
            if not waiting:
                return
            since = min(float(r.get("opened_at") or 0) for r in waiting) - 60
            by_ticket = {d.ticket: d for d in await self.gateway.deals_since(since)}
            for r in waiting:
                d = by_ticket.get(str(r["ticket"]))
                if d is None:
                    continue
                code = infer_exit_reason(r["side"], r["entry"], r.get("sl"), r.get("tp"), d.price, r.get("pip_size") or 0.0001, r.get("sl"))
                self.journal.record_close(r["ticket"], closed_at=d.time, exit=d.price, pnl=d.profit, pnl_estimated=False,
                                          reason="closed_while_stopped" if code == "broker" else code)
        except Exception:  # noqa: BLE001
            logger.exception("could not reconcile the trade history")

    # ------------------------------------------------------------------ status
    def status(self) -> dict[str, Any]:
        a = self.account
        net = self._net["realized"] + self._net["floating"]
        wins = sum(1 for d in self._deals if d.profit > 0)
        positions = [
            {"ticket": p.ticket, "instrument": p.instrument, "side": p.side, "volume": p.volume,
             "price_open": p.price_open, "sl": p.sl, "tp": p.tp, "profit": round(p.profit, 2),
             "age_seconds": int(self._clock() - p.opened_at)}
            for p in self._known.values()
        ]
        return {
            "state": self.state,
            "halt_reason": self.halt_reason,
            "last_error": self.last_error,
            "started_at": self._started_at or None,
            "plan": None if not self.plan else {
                "label": self.plan.label, "source": self.plan.source, "ends_at": self.plan.ends_at,
                "profit_target_money": self.plan.profit_target_money, "close_positions_at_end": self.plan.close_positions_at_end},
            "session_end_reason": self.session_end_reason,
            "preset": self._label_fn(self._store.get())[0],
            "account": None if a is None else {
                "backend": a.backend, "login": a.login, "server": a.server, "currency": a.currency,
                "balance": round(a.balance, 2), "equity": round(a.equity, 2), "is_demo": a.is_demo,
                "hedging": a.hedging, "algo_trading_enabled": a.algo_trading_enabled,
            },
            "session": {
                "start_equity": None if not self.guard else round(self.guard.start_equity, 2),
                "realized": round(self._net["realized"], 2), "floating": round(self._net["floating"], 2),
                "net": round(net, 2), "trades_opened": self._opened_total, "closed_trades": len(self._deals),
                "wins": wins, "losses": len(self._deals) - wins,
                "consecutive_losses": consecutive_losses(self._deals),
                "trades_last_hour": 0 if not self.guard else self.guard.trades_last_hour,
            },
            "positions": positions,
            "signals": self.signals,
            "events": list(self.events)[-40:][::-1],
        }


def _default_gateway_factory(cfg: ScalperConfig) -> ScalperGateway:
    if cfg.backend == "simulation":
        from app.scalper.sim_gateway import SimGateway
        return SimGateway()
    from app.mt5.client import get_mt5_client
    from app.scalper.mt5_gateway import MT5Gateway
    return MT5Gateway(get_mt5_client(), get_settings().mt5_symbol_suffix)


scalper_engine = ScalperEngine()
