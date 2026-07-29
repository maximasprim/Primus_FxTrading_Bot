"""Shared pip-size convention, used by both the paper adapter and the
backtester so cost modeling is consistent across both."""


def get_pip_size(instrument: str) -> float:
    if "JPY" in instrument:
        return 0.01
    if "XAU" in instrument or "XAG" in instrument:
        return 0.1
    return 0.0001
