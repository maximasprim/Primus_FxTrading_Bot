"""Tests for app/engines/fvg.py"""
import pandas as pd

from app.engines.fvg import FVGDirection, MitigationStatus, detect_fvgs, fresh_fvgs


def _make_df(rows: list[dict]) -> pd.DataFrame:
    idx = pd.date_range("2026-01-01", periods=len(rows), freq="5min", tz="UTC")
    return pd.DataFrame(rows, index=idx)


def test_detects_bullish_fvg():
    # candle0.high(1.10) < candle2.low(1.13) -> bullish gap between them
    rows = [
        {"open": 1.09, "high": 1.10, "low": 1.08, "close": 1.095},
        {"open": 1.11, "high": 1.14, "low": 1.11, "close": 1.135},  # impulsive middle candle
        {"open": 1.135, "high": 1.15, "low": 1.13, "close": 1.145},
    ]
    df = _make_df(rows)
    gaps = detect_fvgs(df)
    assert len(gaps) == 1
    assert gaps[0].direction == FVGDirection.BULLISH
    assert gaps[0].bottom == 1.10
    assert gaps[0].top == 1.13


def test_detects_bearish_fvg():
    rows = [
        {"open": 1.15, "high": 1.155, "low": 1.14, "close": 1.145},
        {"open": 1.13, "high": 1.135, "low": 1.10, "close": 1.105},
        {"open": 1.105, "high": 1.11, "low": 1.09, "close": 1.095},
    ]
    df = _make_df(rows)
    gaps = detect_fvgs(df)
    assert len(gaps) == 1
    assert gaps[0].direction == FVGDirection.BEARISH


def test_no_gap_when_candles_overlap():
    rows = [
        {"open": 1.10, "high": 1.12, "low": 1.09, "close": 1.11},
        {"open": 1.11, "high": 1.13, "low": 1.10, "close": 1.12},
        {"open": 1.12, "high": 1.14, "low": 1.11, "close": 1.13},
    ]
    df = _make_df(rows)
    gaps = detect_fvgs(df)
    assert gaps == []


def test_mitigation_tracking():
    rows = [
        {"open": 1.09, "high": 1.10, "low": 1.08, "close": 1.095},
        {"open": 1.11, "high": 1.14, "low": 1.11, "close": 1.135},
        {"open": 1.135, "high": 1.15, "low": 1.13, "close": 1.145},
        {"open": 1.14, "high": 1.145, "low": 1.095, "close": 1.10},  # returns fully into the gap
    ]
    df = _make_df(rows)
    gaps = detect_fvgs(df)
    assert gaps[0].status == MitigationStatus.FULLY_MITIGATED
    assert fresh_fvgs(gaps) == []
