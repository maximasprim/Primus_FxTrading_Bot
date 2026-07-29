"""Tests for app/execution/duplicate_protection.py (execution-feature spec §13)."""
from datetime import datetime, timezone

import pytest

from app.execution.duplicate_protection import DuplicateTradeGuard, generate_signal_id
from app.models.schemas import (
    Direction,
    EntryZone,
    MarketBias,
    MarketRegime,
    ScenarioAnalysis,
    ScoreBreakdown,
    SetupGrade,
    SetupStatus,
    Timeframe,
    TradeSetup,
)


def _make_setup(instrument="EUR/USD") -> TradeSetup:
    return TradeSetup(
        instrument=instrument,
        direction=Direction.BUY,
        setup_type="Liquidity Sweep + MSS + FVG Retest",
        entry_zone=EntryZone(min=1.0810, max=1.0820),
        stop_loss=1.0780,
        take_profit_1=1.0870,
        confluence_score=91,
        score_breakdown=ScoreBreakdown(
            market_structure=20, higher_timeframe_alignment=20, liquidity_confirmation=15,
            entry_zone_quality=15, momentum=10, risk_reward=7, news_and_conditions=4,
        ),
        grade=SetupGrade.A_PLUS,
        market_regime=MarketRegime.STRONG_TREND,
        timeframe=Timeframe.M5,
        higher_timeframe_bias=MarketBias.STRONG_BULLISH,
        scenario=ScenarioAnalysis(
            primary_scenario="x", alternative_scenario="y",
            confirmation_required="z", invalidation_condition="break below 1.0780",
        ),
        status=SetupStatus.CONFIRMED,
        reasoning="test",
        generated_at=datetime(2026, 7, 27, tzinfo=timezone.utc),
    )


def test_signal_id_format():
    setup = _make_setup()
    sid = generate_signal_id(setup)
    assert sid.startswith("EURUSD-20260727-5M-")


@pytest.mark.asyncio
async def test_duplicate_detection_in_memory_fallback():
    """No redis_client injected and REDIS_URL not reachable in the test env
    -> exercises the in-memory fallback path."""
    guard = DuplicateTradeGuard()
    setup = _make_setup()
    sid = generate_signal_id(setup)

    assert await guard.is_duplicate(sid) is False
    await guard.mark_processed(sid, executed=True)
    assert await guard.is_duplicate(sid) is True


@pytest.mark.asyncio
async def test_duplicate_detection_via_fakeredis():
    """Injects a fakeredis client directly so the actual Redis code path
    (not just the in-memory fallback) is genuinely exercised."""
    fakeredis = pytest.importorskip("fakeredis")
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)

    guard = DuplicateTradeGuard(redis_client=client)
    setup = _make_setup()
    sid = generate_signal_id(setup)

    assert await guard.is_duplicate(sid) is False
    await guard.mark_processed(sid, executed=True)
    assert await guard.is_duplicate(sid) is True

    # Confirm it's really in Redis, not the in-memory dict.
    assert await client.exists(guard._REDIS_KEY_PREFIX + sid)
    assert guard._seen == {}


def test_active_trade_check_scoped_to_instrument():
    guard = DuplicateTradeGuard()
    open_ids = ["EURUSD-20260727-5M-LIQUIDITY-001"]
    assert guard.has_active_trade_for_instrument("EUR/USD", open_ids) is True
    assert guard.has_active_trade_for_instrument("GBP/USD", open_ids) is False
