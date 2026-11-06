"""The fast numpy signal must give exactly the same answers as the original pandas version."""
import numpy as np
import pandas as pd
import pytest

from app.scalper.signals import compute_signal
from tests.reference.signals_pandas import compute_signal as reference


def _bars(seed, n, drift, vol):
    rng = np.random.default_rng(seed)
    c = 1.1 + np.cumsum(rng.normal(drift, vol, n))
    o = np.r_[c[0], c[:-1]]
    h = np.maximum(o, c) + abs(rng.normal(0, vol / 3, n))
    l = np.minimum(o, c) - abs(rng.normal(0, vol / 3, n))
    return pd.DataFrame({"open": o, "high": h, "low": l, "close": c})


@pytest.mark.parametrize("seed", range(60))
def test_identical_to_pandas_reference(seed):
    rng = np.random.default_rng(1000 + seed)
    n = int(rng.integers(60, 260))
    df = _bars(seed, n, float(rng.normal(0, 1e-4)), float(rng.uniform(2e-5, 4e-4)))
    spread = float(rng.uniform(0.2, 3.0))
    a, b = compute_signal(df, spread, 1e-4), reference(df, spread, 1e-4)
    assert (a.side, a.score, a.reasons) == (b.side, b.score, b.reasons)
    assert a.atr_pips == pytest.approx(b.atr_pips, rel=1e-9)


def test_much_faster_than_pandas():
    import time
    df = _bars(1, 120, 0.0, 2e-4)
    t = time.perf_counter(); [compute_signal(df, 0.8, 1e-4) for _ in range(300)]; fast = time.perf_counter() - t
    t = time.perf_counter(); [reference(df, 0.8, 1e-4) for _ in range(300)]; slow = time.perf_counter() - t
    assert fast * 5 < slow
