"""Session rules for the scalper: when to stop the bot, and when to skip an entry.
Pure logic (no I/O) so every rule is unit-tested."""
from __future__ import annotations

from collections import deque

from app.scalper.config import ScalperConfig
from app.scalper.models import Deal


def consecutive_losses(deals: list[Deal]) -> int:
    count = 0
    for d in reversed(deals):
        if d.profit < 0:
            count += 1
        else:
            break
    return count


class SessionGuard:
    def __init__(self, start_equity: float, started_at: float) -> None:
        self.start_equity = start_equity
        self.started_at = started_at
        self._opened: deque[float] = deque()
        self._last_entry: dict[str, float] = {}

    # -- bot-level halts ------------------------------------------------
    def halt_reason(self, cfg: ScalperConfig, net_pnl: float, deals: list[Deal]) -> str | None:
        if cfg.session_profit_target_money > 0 and net_pnl >= cfg.session_profit_target_money:
            return f"Session profit target reached ({net_pnl:+.2f} >= {cfg.session_profit_target_money:.2f})."
        if cfg.session_max_loss_money > 0 and net_pnl <= -cfg.session_max_loss_money:
            return f"Session loss limit hit ({net_pnl:+.2f} <= -{cfg.session_max_loss_money:.2f})."
        if cfg.session_max_loss_pct > 0 and self.start_equity > 0:
            limit = self.start_equity * cfg.session_max_loss_pct / 100.0
            if net_pnl <= -limit:
                return f"Session loss limit hit ({net_pnl:+.2f} <= -{limit:.2f}, {cfg.session_max_loss_pct}% of equity)."
        if cfg.max_consecutive_losses > 0:
            streak = consecutive_losses(deals)
            if streak >= cfg.max_consecutive_losses:
                return f"{streak} losing trades in a row (limit {cfg.max_consecutive_losses})."
        return None

    # -- per-entry checks ---------------------------------------------------
    def entry_block(
        self, cfg: ScalperConfig, instrument: str, n_open: int, n_open_instrument: int, now: float, hedging: bool = True,
    ) -> str | None:
        per_instrument_cap = cfg.max_positions_per_instrument if hedging else 1
        if n_open >= cfg.max_concurrent_positions:
            return f"max {cfg.max_concurrent_positions} concurrent positions"
        if n_open_instrument >= per_instrument_cap:
            return f"max {per_instrument_cap} positions on {instrument}" + ("" if hedging else " (netting account)")
        last = self._last_entry.get(instrument)
        if last is not None and now - last < cfg.cooldown_seconds:
            return f"cooldown ({cfg.cooldown_seconds - (now - last):.0f}s left)"
        while self._opened and now - self._opened[0] > 3600:
            self._opened.popleft()
        if len(self._opened) >= cfg.max_trades_per_hour:
            return f"max {cfg.max_trades_per_hour} trades/hour"
        return None

    def record_entry(self, instrument: str, now: float) -> None:
        self._opened.append(now)
        self._last_entry[instrument] = now

    @property
    def trades_last_hour(self) -> int:
        return len(self._opened)
