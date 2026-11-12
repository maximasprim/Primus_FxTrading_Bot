"""Hybrid (regime switch) entry style: it routes each kind of market to the one style built for it and stands aside otherwise."""
import numpy as np
import pytest
from pydantic import ValidationError

from app.scalper.config import ScalperConfig
from app.scalper.engine import ScalperEngine
from app.scalper.models import SignalResult
from app.scalper.styles import (STYLE_FUNCS, STYLE_INFO, breakout_signal, hybrid_signal, range_reversal_signal,
                                spike_fade_signal)
from tests.test_scalper_styles import PIP, chop, frame, sine_range


def spike_after(base, jump_pips, steps=3):
    """`base` closes followed by a quick `jump_pips` move over `steps` bars (all inside the last 3 minutes)."""
    last = base[-1]
    return list(base) + [last + jump_pips * PIP * (i + 1) / steps for i in range(steps)]


# ------------------------------------------------------------------ routing
def test_spike_in_a_sideways_market_is_faded_like_spike_fade():
    candles = frame(spike_after(chop(80), 9.0), wick=0.00003)
    res = hybrid_signal(candles, 0.8, PIP)
    ref = spike_fade_signal(candles, 0.8, PIP)
    assert ref.side == "SELL" and res.side == "SELL"                       # fades the up-spike
    assert res.score == ref.score and res.reasons[0] == "[spike regime]" and res.reasons[1:] == ref.reasons


def test_spike_inside_a_trend_is_not_faded():
    trend = list(1.1000 + np.arange(70) * 1.2 * PIP)
    candles = frame(spike_after(trend, 12.0), wick=0.00003)
    assert spike_fade_signal(candles, 0.8, PIP).side == "SELL"            # the single style WOULD fade it...
    res = hybrid_signal(candles, 0.8, PIP)
    assert res.side is None and "inside a trend" in res.reasons[0]        # ...the hybrid does not


def test_break_after_a_squeeze_goes_to_breakout():
    tight = frame(chop(60) + [1.1000 + (0.2 if i % 2 else -0.2) * PIP for i in range(10)] + [1.1011])
    ref = breakout_signal(tight, 0.8, PIP)
    res = hybrid_signal(tight, 0.8, PIP)
    assert ref.side == "BUY" and res.side == "BUY" and res.score == ref.score and res.reasons[0] == "[breakout regime]"


def test_a_squeeze_break_is_ridden_not_faded_even_though_it_looks_like_a_spike():
    """Volatility was tiny, so the break is many ATRs in 3 minutes: spike_fade alone would bet AGAINST it."""
    tight = frame(chop(60) + [1.1000 + (0.2 if i % 2 else -0.2) * PIP for i in range(10)] + [1.1011])
    assert spike_fade_signal(tight, 0.8, PIP).side == "SELL"            # the single style would fade the break ...
    assert hybrid_signal(tight, 0.8, PIP).side == "BUY"                  # ... the hybrid rides it


@pytest.mark.parametrize("seed", [100, 176])
def test_a_break_with_no_squeeze_and_no_spike_is_skipped(seed):
    """Seeded random-walk examples where the range breaks on ordinary (not compressed) volatility with no sharp move."""
    c = 1.1 + np.cumsum(np.random.default_rng(seed).normal(0, 2e-4, 120))
    candles = frame(c, wick=1e-4)
    assert breakout_signal(candles, 0.8, PIP).side is not None          # the single style takes it ...
    res = hybrid_signal(candles, 0.8, PIP)
    assert res.side is None and "standing aside" in res.reasons[0]       # ... the hybrid wants a squeeze or a spike first


def test_an_exhaustion_move_out_of_a_loose_range_is_faded():
    base = chop(60, amp=1.0) + chop(10, amp=3.0)
    candles = frame(base + [1.1000 + 0.0013])                            # sharp move, but no squeeze before it
    res = hybrid_signal(candles, 0.8, PIP)
    assert res.side == "SELL" and res.reasons[0] == "[spike regime]"


def test_range_edge_in_a_sideways_market_goes_to_range_reversal():
    candles = frame(sine_range(phase_end="bottom"), wick=0.00004)
    ref = range_reversal_signal(candles, 0.8, PIP)
    res = hybrid_signal(candles, 0.8, PIP)
    assert ref.side == "BUY"
    assert res.side == "BUY" and res.reasons[0] in ("[range regime]", "[spike regime]")   # either mean-reversion style agrees


def test_a_clean_trend_with_no_spike_or_break_is_left_alone():
    candles = frame(1.1000 + np.arange(80) * 0.25 * PIP, wick=0.00001)     # slow, steady drift
    res = hybrid_signal(candles, 0.8, PIP)
    assert res.side is None and res.score == 0


def test_the_middle_of_a_quiet_range_is_left_alone():
    assert hybrid_signal(frame(chop(80)), 0.8, PIP).side is None


# ------------------------------------------------------------------ same contract as every other style
def test_hybrid_refuses_short_history_and_quiet_markets():
    assert hybrid_signal(frame(chop(20)), 0.8, PIP).side is None
    assert hybrid_signal(None, 0.8, PIP).side is None
    quiet = hybrid_signal(frame([1.1 + 0.000001 * (i % 2) for i in range(100)], wick=0.0000005), 3.0, PIP)
    assert quiet.side is None


@pytest.mark.parametrize("seed", range(40))
def test_scores_and_sides_are_valid_on_random_data_and_never_fire_when_no_regime_matches(seed):
    rng = np.random.default_rng(seed)
    c = 1.1 + np.cumsum(rng.normal(0, 2e-4, 160))
    candles = frame(c, wick=1e-4)
    r = hybrid_signal(candles, 0.8, PIP)
    assert isinstance(r, SignalResult) and r.side in (None, "BUY", "SELL") and 0 <= r.score <= 100
    if r.side is not None:                                               # a trade always comes from one of the three styles, tagged
        assert r.reasons[0] in ("[spike regime]", "[breakout regime]", "[range regime]")
        inner = {"[spike regime]": spike_fade_signal, "[breakout regime]": breakout_signal, "[range regime]": range_reversal_signal}[r.reasons[0]]
        ref = inner(candles, 0.8, PIP)
        assert (r.side, r.score) == (ref.side, ref.score)                # same decision as that style would make alone


def test_it_never_fires_unless_one_of_the_styles_it_combines_would():
    """It is a dispatcher: it can only ever take (or skip) a trade one of its three styles proposes. It adds no signals of its own."""
    for seed in range(25):
        rng = np.random.default_rng(100 + seed)
        c = 1.1 + np.cumsum(rng.normal(0, 2e-4, 400))
        for end in range(120, 400, 7):
            window = frame(c[:end], wick=1e-4)
            if hybrid_signal(window, 0.8, PIP).side is not None:
                assert any(f(window, 0.8, PIP).side is not None for f in (spike_fade_signal, breakout_signal, range_reversal_signal))


# ------------------------------------------------------------------ plumbing
def test_hybrid_is_registered_selectable_and_does_not_change_the_default():
    assert STYLE_FUNCS["hybrid"] is hybrid_signal and "hybrid" in STYLE_INFO
    assert STYLE_INFO["hybrid"]["label"] and STYLE_INFO["hybrid"]["hint"] and 20 <= STYLE_INFO["hybrid"]["suggested_min_score"] <= 95
    assert ScalperConfig().entry_style == "momentum"                      # existing users see no difference
    assert ScalperConfig(entry_style="hybrid").entry_style == "hybrid"
    with pytest.raises(ValidationError):
        ScalperConfig(entry_style="hybrid2")


def test_engine_and_backtester_pick_the_hybrid_function():
    cfg = ScalperConfig(entry_style="hybrid")
    eng = ScalperEngine.__new__(ScalperEngine)
    eng._signal_fn = None
    assert eng._style_fn(cfg) is hybrid_signal
    from app.scalper.backtest import runner
    assert runner.STYLE_FUNCS[cfg.entry_style] is hybrid_signal
