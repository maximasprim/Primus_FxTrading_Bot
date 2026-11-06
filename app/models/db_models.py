"""
SQLAlchemy ORM models.

Two storage concerns:
  1. Market data (candles) - recommended to live in a TimescaleDB hypertable for
     efficient time-series queries. See docker-compose.yml (uses the timescaledb image).
  2. Signal tracking (spec §20) - every alert recorded with enough detail to later
     answer "do high-score setups actually outperform lower-score setups?" etc.

Run `alembic revision --autogenerate -m "init"` once this is wired to a real DB
to generate the migration (not included in this scaffold - see PHASES.md).
"""
from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Integer, JSON, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Candle(Base):
    __tablename__ = "candles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    instrument: Mapped[str] = mapped_column(String(16), index=True)
    timeframe: Mapped[str] = mapped_column(String(16), index=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    open: Mapped[float] = mapped_column(Float)
    high: Mapped[float] = mapped_column(Float)
    low: Mapped[float] = mapped_column(Float)
    close: Mapped[float] = mapped_column(Float)
    volume: Mapped[float] = mapped_column(Float, default=0.0)
    bid: Mapped[float | None] = mapped_column(Float, nullable=True)
    ask: Mapped[float | None] = mapped_column(Float, nullable=True)
    source: Mapped[str] = mapped_column(String(32), default="unknown")


class Signal(Base):
    """One row per alert ever generated - the core of signal performance tracking."""

    __tablename__ = "signals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    instrument: Mapped[str] = mapped_column(String(16), index=True)
    direction: Mapped[str] = mapped_column(String(8))
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    timeframe: Mapped[str] = mapped_column(String(16))

    entry_zone_min: Mapped[float | None] = mapped_column(Float, nullable=True)
    entry_zone_max: Mapped[float | None] = mapped_column(Float, nullable=True)
    actual_entry: Mapped[float | None] = mapped_column(Float, nullable=True)
    stop_loss: Mapped[float | None] = mapped_column(Float, nullable=True)
    take_profit_1: Mapped[float | None] = mapped_column(Float, nullable=True)
    take_profit_2: Mapped[float | None] = mapped_column(Float, nullable=True)

    confluence_score: Mapped[int] = mapped_column(Integer)
    score_breakdown: Mapped[dict] = mapped_column(JSON)
    grade: Mapped[str] = mapped_column(String(8))
    market_regime: Mapped[str] = mapped_column(String(32))
    strategy_components: Mapped[dict] = mapped_column(JSON, default=dict)
    news_conditions: Mapped[str | None] = mapped_column(String(64), nullable=True)

    status: Mapped[str] = mapped_column(String(32), default="SETUP_FORMING")
    reasoning: Mapped[str] = mapped_column(Text, default="")

    result: Mapped[str | None] = mapped_column(String(16), nullable=True)  # WIN/LOSS/BE/OPEN
    max_favorable_excursion: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_adverse_excursion: Mapped[float | None] = mapped_column(Float, nullable=True)
    time_to_target_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    time_to_stop_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)

    events: Mapped[list["SignalEvent"]] = relationship(back_populates="signal")


class SignalEvent(Base):
    """Stage transitions for a signal (SETUP_FORMING -> ENTRY_ZONE_REACHED -> CONFIRMED -> ...)."""

    __tablename__ = "signal_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    signal_id: Mapped[int] = mapped_column(ForeignKey("signals.id"), index=True)
    stage: Mapped[str] = mapped_column(String(32))
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)

    signal: Mapped[Signal] = relationship(back_populates="events")


class TradeJournalEntry(Base):
    """Spec §19 - every trade (manual, approved, automatic, or paper) is recorded here,
    distinct from `Signal` (which tracks every *alert*, executed or not)."""

    __tablename__ = "trade_journal"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    signal_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    instrument: Mapped[str] = mapped_column(String(16), index=True)
    direction: Mapped[str] = mapped_column(String(8))
    strategy_combination: Mapped[str] = mapped_column(String(255), default="")
    confluence_score: Mapped[int] = mapped_column(Integer)

    entry: Mapped[float] = mapped_column(Float)
    stop_loss: Mapped[float] = mapped_column(Float)
    take_profit: Mapped[float] = mapped_column(Float)
    position_size: Mapped[float] = mapped_column(Float)
    risk_pct: Mapped[float] = mapped_column(Float)

    execution_source: Mapped[str] = mapped_column(String(32))  # MANUAL/APPROVED/AUTOMATIC/PAPER
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    broker_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    result: Mapped[str | None] = mapped_column(String(16), nullable=True)  # WIN/LOSS/BE/OPEN
    profit_loss: Mapped[float | None] = mapped_column(Float, nullable=True)
    r_multiple: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_favorable_excursion: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_adverse_excursion: Mapped[float | None] = mapped_column(Float, nullable=True)


class TradingModeAudit(Base):
    """Spec §8 - records every trading-mode / auto-trading toggle change."""

    __tablename__ = "trading_mode_audit"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    changed_by: Mapped[str] = mapped_column(String(64))
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    previous_mode: Mapped[str] = mapped_column(String(32))
    new_mode: Mapped[str] = mapped_column(String(32))
    ip_or_device: Mapped[str | None] = mapped_column(String(128), nullable=True)


class EquityCurvePoint(Base):
    """Running account-equity snapshots, used to compute real max drawdown
    (execution-feature spec §10) rather than inferring it from a single
    current-equity value with no history."""

    __tablename__ = "equity_curve"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow, index=True)
    equity: Mapped[float] = mapped_column(Float)
    reason: Mapped[str] = mapped_column(String(64), default="trade_closed")  # trade_closed | manual_snapshot | ...


class GlobalTradingModeState(Base):
    """Persists the global trading mode across restarts (execution-feature
    spec §21). Deliberately does NOT persist `auto_trading_enabled` - per
    spec §21, that must always come back OFF after a restart regardless of
    what it was before, so it's intentionally not a column here at all
    (TradingModeManager always initializes it False in-process)."""

    __tablename__ = "global_trading_mode_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)  # singleton row, id=1
    global_mode: Mapped[str] = mapped_column(String(32), default="ALERT_MODE")
    paper_trading_enabled: Mapped[bool] = mapped_column(default=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)


class InstrumentConfig(Base):
    """Spec §5 - per-instrument auto-trading mode and risk overrides."""

    __tablename__ = "instrument_config"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    instrument: Mapped[str] = mapped_column(String(16), unique=True, index=True)
    trading_mode: Mapped[str] = mapped_column(String(16), default="ALERT_ONLY")
    risk_per_trade_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    minimum_score: Mapped[int | None] = mapped_column(Integer, nullable=True)


class NotificationLog(Base):
    """Notification-feature spec §9 - tracks every delivery attempt through
    its lifecycle (created -> sent -> accepted -> delivered/failed -> read)."""

    __tablename__ = "notification_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_type: Mapped[str] = mapped_column(String(64), index=True)
    priority: Mapped[str] = mapped_column(String(16))
    channel: Mapped[str] = mapped_column(String(16))
    related_signal_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)

    status: Mapped[str] = mapped_column(String(32), default="CREATED")
    provider_message_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    fallback_from_channel: Mapped[str | None] = mapped_column(String(16), nullable=True)


class BacktestRun(Base):
    __tablename__ = "backtest_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    instrument: Mapped[str] = mapped_column(String(16))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    date_range_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    date_range_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    parameters: Mapped[dict] = mapped_column(JSON, default=dict)
    results: Mapped[dict] = mapped_column(JSON, default=dict)
