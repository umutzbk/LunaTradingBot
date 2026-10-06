"""Deterministic synthetic bar generators and a causality checker, shared by tests."""

from __future__ import annotations

from collections.abc import Callable
from typing import Literal

import numpy as np
import pandas as pd

BAR_FREQ = "15min"
_START = "2024-01-01"


def _index(n: int) -> pd.DatetimeIndex:
    return pd.date_range(_START, periods=n, freq=BAR_FREQ, tz="UTC")


def _bars_from_close(close: np.ndarray, rng: np.random.Generator, noise: float) -> pd.DataFrame:
    n = len(close)
    open_ = np.empty(n)
    open_[0] = close[0]
    open_[1:] = close[:-1]
    up = np.abs(rng.normal(0.0, noise, n)) * close
    dn = np.abs(rng.normal(0.0, noise, n)) * close
    hi_body = np.maximum(open_, close)
    lo_body = np.minimum(open_, close)
    high = hi_body + up
    low = np.maximum(lo_body - dn, lo_body * 0.5)  # stays positive and <= body
    return pd.DataFrame(
        {
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": rng.uniform(1.0, 100.0, n),
            "trades": rng.integers(1, 500, n).astype("int64"),
            "filled": np.zeros(n, dtype=bool),
        },
        index=_index(n),
    )


def synthetic_bars(
    n: int,
    kind: Literal["random_walk", "trend", "ou"],
    seed: int = 0,
    *,
    start_price: float = 100.0,
    sigma: float = 0.002,
    drift: float = 0.0005,
    theta: float = 0.05,
) -> pd.DataFrame:
    """Deterministic bars. random_walk: log-RW; trend: RW with drift; ou: mean-reverting log price."""
    rng = np.random.default_rng(seed)
    eps = rng.normal(0.0, sigma, n)
    if kind == "random_walk":
        logp = np.cumsum(eps)
    elif kind == "trend":
        logp = np.cumsum(eps + drift)
    elif kind == "ou":
        logp = np.zeros(n)
        for i in range(1, n):
            logp[i] = logp[i - 1] * (1.0 - theta) + eps[i]
    else:
        raise ValueError(f"unknown kind {kind!r}")
    close = start_price * np.exp(logp)
    return _bars_from_close(close, rng, noise=sigma / 2)


def synthetic_pair(
    n: int,
    kind: Literal["cointegrated", "independent", "constant_spread"],
    seed: int = 0,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return (A, B) bar frames. cointegrated: log A = 0.8 log B + OU noise + c."""
    rng = np.random.default_rng(seed)
    log_b = np.cumsum(rng.normal(0.0, 0.002, n)) + np.log(50.0)
    if kind == "cointegrated":
        eps = rng.normal(0.0, 0.001, n)
        ou = np.zeros(n)
        for i in range(1, n):
            ou[i] = ou[i - 1] * 0.95 + eps[i]
        log_a = 0.8 * log_b + ou + 0.3
    elif kind == "independent":
        log_a = np.cumsum(rng.normal(0.0, 0.002, n)) + np.log(100.0)
    elif kind == "constant_spread":
        log_a = log_b + np.log(2.0)
    else:
        raise ValueError(f"unknown kind {kind!r}")
    a = _bars_from_close(np.exp(log_a), np.random.default_rng(seed + 1), noise=0.001)
    b = _bars_from_close(np.exp(log_b), np.random.default_rng(seed + 2), noise=0.001)
    return a, b


def assert_causal(
    fn: Callable[[pd.DataFrame], pd.Series | pd.DataFrame],
    df: pd.DataFrame,
    cut_points: int = 5,
) -> None:
    """fn(df[:k]) must equal fn(df)[:k] (no look-ahead).

    Cut points are stratified (small values inside typical warm-ups, plus len-1) with
    `cut_points` random extras, so warm-up bfill and similar leaks are caught.
    """
    n = len(df)
    full = fn(df)
    fixed = {k for k in (2, 3, 5, 10, 25, 50, 100, 300, 500) if k < n} | {n - 1}
    rng = np.random.default_rng(12345)
    extras = {int(k) for k in rng.integers(2, n, size=cut_points)}
    for k in sorted(fixed | extras):
        part = fn(df.iloc[:k])
        expected = full.iloc[:k]
        if isinstance(expected, pd.DataFrame):
            pd.testing.assert_frame_equal(part, expected, check_freq=False, rtol=1e-9, atol=0)
        else:
            pd.testing.assert_series_equal(part, expected, check_freq=False, rtol=1e-9, atol=0)
