"""Tests for app/services/equity_tracking_service.py (execution-feature spec §10)."""
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models.db_models import Base
from app.services.equity_tracking_service import get_drawdown_status, record_equity_snapshot


@pytest_asyncio.fixture
async def db_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        yield session
    await engine.dispose()


@pytest.mark.asyncio
async def test_drawdown_zero_with_no_history(db_session: AsyncSession):
    status = await get_drawdown_status(db_session, current_equity=10_000.0)
    assert status.drawdown_pct == 0.0
    assert status.peak_equity == 10_000.0


@pytest.mark.asyncio
async def test_drawdown_computed_against_real_peak(db_session: AsyncSession):
    await record_equity_snapshot(db_session, 10_000.0)
    await record_equity_snapshot(db_session, 11_000.0)  # new peak
    await record_equity_snapshot(db_session, 10_500.0)  # pulled back

    status = await get_drawdown_status(db_session, current_equity=9_900.0)
    assert status.peak_equity == 11_000.0
    expected_dd = (11_000.0 - 9_900.0) / 11_000.0 * 100
    assert abs(status.drawdown_pct - expected_dd) < 0.01


@pytest.mark.asyncio
async def test_current_equity_can_itself_be_a_new_peak(db_session: AsyncSession):
    await record_equity_snapshot(db_session, 10_000.0)
    status = await get_drawdown_status(db_session, current_equity=12_000.0)
    assert status.peak_equity == 12_000.0
    assert status.drawdown_pct == 0.0
