"""Causal feature library: the value at bar t uses only bars <= t. NaN during warm-up."""

from __future__ import annotations

import numpy as np
import pandas as pd


def ema(x: pd.Series, span: int) -> pd.Series:
    return x.ewm(span=span, min_periods=span, adjust=False).mean()


def atr(bars: pd.DataFrame, n: int) -> pd.Series:
    """Wilder ATR (alpha = 1/n) on high/low/close; NaN for the first n-1 bars."""
    prev_close = bars["close"].shift(1)
    tr = pd.concat(
        [
            bars["high"] - bars["low"],
            (bars["high"] - prev_close).abs(),
            (bars["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)  # first bar: prev_close is NaN, so TR = high - low
    return tr.ewm(alpha=1.0 / n, min_periods=n, adjust=False).mean()


def ewma_zscore(x: pd.Series, span: int) -> pd.Series:
    ew = x.ewm(span=span, min_periods=span, adjust=False)
    std = ew.std().replace(0.0, np.nan)
    return (x - ew.mean()) / std


def realized_vol(close: pd.Series, window: int) -> pd.Series:
    return np.log(close).diff().rolling(window, min_periods=window).std()


def trend_strength(
    close: pd.Series, bars: pd.DataFrame, fast: int = 96, slow: int = 480, atr_n: int = 96
) -> pd.Series:
    """(ema_fast - ema_slow) / ATR, signed."""
    a = atr(bars, atr_n).replace(0.0, np.nan)
    return (ema(close, fast) - ema(close, slow)) / a


def rolling_corr(a: pd.Series, b: pd.Series, window: int) -> pd.Series:
    """Rolling correlation of log returns."""
    ra = np.log(a).diff()
    rb = np.log(b).diff()
    return ra.rolling(window, min_periods=window).corr(rb)


def rolling_beta(log_a: pd.Series, log_b: pd.Series, window: int) -> pd.Series:
    """Trailing OLS slope of log_a on log_b (cov / var over `window` bars)."""
    cov = log_a.rolling(window, min_periods=window).cov(log_b)
    var = log_b.rolling(window, min_periods=window).var()
    return cov / var.replace(0.0, np.nan)
