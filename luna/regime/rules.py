"""Rule-based causal regime classifier (spec 5.4). Fixed percentiles, never optimized."""

from __future__ import annotations

import numpy as np
import pandas as pd

from luna.features.core import realized_vol, trend_strength

VOL_WINDOW = 96  # 1 day of 15-minute bars


def _past_quantile(x: pd.Series, q: float, lookback_bars: int) -> pd.Series:
    """Quantile of x over the trailing window ending at t-1 (current bar excluded).

    min_periods = lookback_bars // 4 non-NaN past observations; NaN before that.
    """
    return x.shift(1).rolling(lookback_bars, min_periods=max(1, lookback_bars // 4)).quantile(q)


def classify_regime(
    bars: pd.DataFrame,
    trend_pct: float = 0.70,
    vol_pct: float = 0.90,
    lookback_bars: int = 8640,
) -> pd.Series:
    """Label each bar TREND_UP / TREND_DOWN / RANGE / HIGH_VOL / UNKNOWN.

    - HIGH_VOL: 1-day realized vol above its trailing `vol_pct` quantile (takes precedence).
    - TREND_UP/DOWN: |trend_strength| above its trailing `trend_pct` quantile, by sign.
    - RANGE otherwise. UNKNOWN when any input (trend, vol, either threshold) is NaN.
    Thresholds use only past values (window ends at t-1), so labels are causal.
    """
    close = bars["close"]
    ts = trend_strength(close, bars)
    vol = realized_vol(close, VOL_WINDOW)
    ts_thr = _past_quantile(ts.abs(), trend_pct, lookback_bars)
    vol_thr = _past_quantile(vol, vol_pct, lookback_bars)

    known = ts.notna() & vol.notna() & ts_thr.notna() & vol_thr.notna()
    high_vol = (vol > vol_thr).to_numpy()
    trending = (ts.abs() > ts_thr).to_numpy()
    up = (ts > 0).to_numpy()

    labels = np.where(
        high_vol,
        "HIGH_VOL",
        np.where(trending, np.where(up, "TREND_UP", "TREND_DOWN"), "RANGE"),
    )
    labels = np.where(known.to_numpy(), labels, "UNKNOWN")
    return pd.Series(labels, index=bars.index, dtype=object, name="regime")
