"""
Pydantic schemas — the data contracts moving between engines, services, and the API.

These mirror the structures described in the spec:
  * §11 Confluence Scoring Engine
  * §12 Setup Classification
  * §13 Three-Stage Alert System
  * §14 Trade Setup Output
  * §15 Scenario-Based Analysis
"""
from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field


class Direction(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    WAIT = "WAIT"
    NO_TRADE = "NO_TRADE"


class SetupGrade(str, Enum):
    A_PLUS = "A+"
    A = "A"
    B = "B"
    NO_TRADE = "NO_TRADE"


class SetupStatus(str, Enum):
    SETUP_FORMING = "SETUP_FORMING"
    ENTRY_ZONE_REACHED = "ENTRY_ZONE_REACHED"
    CONFIRMED = "CONFIRMED"
    INVALIDATED = "INVALIDATED"
    COMPLETED = "COMPLETED"


class MarketBias(str, Enum):
    STRONG_BULLISH = "Strong Bullish"
    WEAK_BULLISH = "Weak Bullish"
    RANGING = "Ranging"
    WEAK_BEARISH = "Weak Bearish"
    STRONG_BEARISH = "Strong Bearish"
    UNCLEAR = "Unclear"


class MarketRegime(str, Enum):
    STRONG_TREND = "Strong Trend"
    WEAK_TREND = "Weak Trend"
    RANGE = "Range"
    HIGH_VOLATILITY = "High Volatility"
    LOW_VOLATILITY = "Low Volatility"
    BREAKOUT = "Breakout"
    CONSOLIDATION = "Consolidation"
    REVERSAL = "Reversal Environment"


class Timeframe(str, Enum):
    MN = "1M_monthly"
    W1 = "1W"
    D1 = "1D"
    H4 = "4H"
    H1 = "1H"
    M15 = "15M"
    M5 = "5M"
    M1 = "1M"


class EntryZone(BaseModel):
    min: float
    max: float


class ScoreBreakdown(BaseModel):
    """Mirrors spec §11 point allocation. Max total = 100."""

    market_structure: int = Field(ge=0, le=20)
    higher_timeframe_alignment: int = Field(ge=0, le=20)
    liquidity_confirmation: int = Field(ge=0, le=15)
    entry_zone_quality: int = Field(ge=0, le=15)
    momentum: int = Field(ge=0, le=10)
    risk_reward: int = Field(ge=0, le=10)
    news_and_conditions: int = Field(ge=0, le=10)
    # Explainability (spec §11 companion): one human-readable line per
    # category explaining *why* it scored what it did. Keyed by the same
    # field names as above. Optional/defaulted so existing callers that
    # construct a ScoreBreakdown without it (e.g. tests) keep working.
    reasons: dict[str, str] = Field(default_factory=dict)

    @property
    def total(self) -> int:
        return (
            self.market_structure
            + self.higher_timeframe_alignment
            + self.liquidity_confirmation
            + self.entry_zone_quality
            + self.momentum
            + self.risk_reward
            + self.news_and_conditions
        )


class ScenarioAnalysis(BaseModel):
    """Spec §15 — every serious setup includes both sides of the argument."""

    primary_scenario: str
    alternative_scenario: str
    confirmation_required: str
    invalidation_condition: str


class TradeSetup(BaseModel):
    """The structured output described in spec §14."""

    instrument: str
    direction: Direction
    setup_type: str
    entry_zone: EntryZone | None = None
    stop_loss: float | None = None
    take_profit_1: float | None = None
    take_profit_2: float | None = None
    risk_reward: float | None = None
    confluence_score: int = Field(ge=0, le=100)
    score_breakdown: ScoreBreakdown
    grade: SetupGrade
    confidence_level: str = Field(
        default="Moderate",
        description="Plain-language confidence label derived from the total "
        "confluence score (e.g. Very High / High / Moderate / Low / Very Low). "
        "Explainability companion to `grade` — see confluence_scoring.confidence_level().",
    )
    market_regime: MarketRegime
    timeframe: Timeframe
    higher_timeframe_bias: MarketBias
    scenario: ScenarioAnalysis
    status: SetupStatus
    reasoning: str = Field(
        description="Human-readable explanation of why this setup was (or wasn't) flagged."
    )
    generated_at: datetime = Field(default_factory=datetime.utcnow)

    def is_alertable(self, minimum_entry_score: int) -> bool:
        return (
            self.status == SetupStatus.CONFIRMED
            and self.direction in (Direction.BUY, Direction.SELL)
            and self.confluence_score >= minimum_entry_score
        )


class TradingViewWebhookPayload(BaseModel):
    """Inbound payload from a TradingView alert webhook (spec §4, §25)."""

    secret: str
    instrument: str
    event: str  # e.g. "approaching_zone", "pine_condition", "technical_condition"
    timeframe: Timeframe
    price: float
    pine_condition_name: str | None = None
    raw_payload: dict | None = None


class NewsEvent(BaseModel):
    title: str
    currency: str
    impact: str  # "low" | "medium" | "high"
    event_time: datetime
    forecast: str | None = None
    previous: str | None = None
    actual: str | None = None
