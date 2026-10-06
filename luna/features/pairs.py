"""Pair-trading features: spread and mean-reversion half-life."""

from __future__ import annotations

import numpy as np
import pandas as pd

_MIN_POINTS = 10


def spread(log_a: pd.Series, log_b: pd.Series, beta: pd.Series | float) -> pd.Series:
    return log_a - beta * log_b


def half_life(s: pd.Series) -> float:
    """Half-life in bars from AR(1) on ds = a + b * s_{t-1}; inf if b >= 0 or too little data."""
    v = s.dropna().to_numpy(dtype=float)
    if len(v) < _MIN_POINTS:
        return float("inf")
    lag = v[:-1]
    delta = np.diff(v)
    lag_c = lag - lag.mean()
    denom = float(lag_c @ lag_c)
    if denom == 0.0:
        return float("inf")
    b = float(lag_c @ (delta - delta.mean())) / denom
    if b >= 0.0:
        return float("inf")
    if b <= -1.0:
        return 0.0  # reverts fully within one bar
    return float(-np.log(2.0) / np.log1p(b))
