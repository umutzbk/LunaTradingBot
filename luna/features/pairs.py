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


def spread_zscore(
    log_a: pd.Series, log_b: pd.Series, beta: pd.Series, span: int
) -> tuple[pd.Series, pd.Series]:
    """EWMA z-score and EWMA std of the spread ``log_a - beta_t * log_b`` at each bar t, where the
    whole history up to t is evaluated with that bar's ``beta_t``.

    Equals ``ewma_zscore(log_a - beta_t * log_b, span).iloc[t]`` for every t (EWMA mean/var are
    bilinear: mean = m_a - beta*m_b, var = v_a - 2*beta*c_ab + beta^2*v_b), so a moving rolling beta
    does not inject ``d(beta) * log_b`` jumps into the spread history. Causal; NaN in warm-up.
    """
    ew_a = log_a.ewm(span=span, min_periods=span, adjust=False)
    ew_b = log_b.ewm(span=span, min_periods=span, adjust=False)
    mean = ew_a.mean() - beta * ew_b.mean()
    var = ew_a.var() - 2.0 * beta * ew_a.cov(log_b) + beta**2 * ew_b.var()
    sigma = np.sqrt(var.clip(lower=0.0)).replace(0.0, np.nan)
    return (log_a - beta * log_b - mean) / sigma, sigma
