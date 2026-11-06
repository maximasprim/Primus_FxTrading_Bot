"""
Schemas for the trade execution / trading-mode feature set
(execution-feature spec §2, §11, §13, §17-19).
"""
from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field


class GlobalTradingMode(str, Enum):
    ANALYSIS_ONLY = "ANALYSIS_ONLY"
    ALERT_MODE = "ALERT_MODE"
    MANUAL_APPROVAL = "MANUAL_APPROVAL"
    FULL_AUTO = "FULL_AUTO"


class InstrumentTradingMode(str, Enum):
    """Per-instrument override (execution-feature spec §5)."""

    AUTO = "AUTO"
    APPROVAL = "APPROVAL"
    ALERT_ONLY = "ALERT_ONLY"
    DISABLED = "DISABLED"


class ExecutionSource(str, Enum):
    """Distinguishes trade origin in the trade journal (spec §19)."""

    MANUAL = "MANUAL_TRADE"
    APPROVED = "APPROVED_TRADE"
    AUTOMATIC = "AUTOMATIC_TRADE"
    PAPER = "PAPER_TRADE"


class ApprovalStatus(str, Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


class ExecutionValidationFailure(BaseModel):
    stage: str
    reason: str


class ExecutionResult(BaseModel):
    """Result of running a TradeSetup through the execution pipeline."""

    signal_id: str
    executed: bool
    execution_source: ExecutionSource
    broker_order_id: str | None = None
    failure: ExecutionValidationFailure | None = None
    position_size: float | None = None
    executed_at: datetime = Field(default_factory=datetime.utcnow)


class TradeApprovalRequest(BaseModel):
    signal_id: str
    instrument: str
    direction: str
    confluence_score: int
    entry_zone_min: float
    entry_zone_max: float
    stop_loss: float
    take_profit_1: float
    risk_pct: float
    status: ApprovalStatus = ApprovalStatus.PENDING
    requested_at: datetime = Field(default_factory=datetime.utcnow)
    expires_at: datetime
    decided_at: datetime | None = None
    decided_by: str | None = None


class TradingModeChangeAudit(BaseModel):
    """Spec §8 - every mode change must be recorded."""

    changed_by: str
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    previous_mode: str
    new_mode: str
    ip_or_device: str | None = None
