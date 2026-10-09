"""Causal resampling of 15m bars to coarser timeframes."""

from __future__ import annotations

from typing import Literal

import pandas as pd

Timeframe = Literal["15m", "1h", "4h", "1d"]
TF: dict[Timeframe, pd.Timedelta] = {
    "15m": pd.Timedelta(minutes=15),
    "1h": pd.Timedelta(hours=1),
    "4h": pd.Timedelta(hours=4),
    "1d": pd.Timedelta(days=1),
}
BAR = TF["15m"]
COLUMNS = ["open", "high", "low", "close", "volume", "trades", "filled"]


def bars_for(duration: pd.Timedelta, tf: Timeframe) -> int:
    """Number of ``tf`` bars in ``duration``; the duration must be an exact multiple."""
    n, rem = divmod(pd.Timedelta(duration), TF[tf])
    if rem != pd.Timedelta(0) or n < 1:
        raise ValueError(f"{duration} is not a positive whole number of {tf} bars")
    return int(n)


def resample_causal(bars: pd.DataFrame, tf: Timeframe) -> pd.DataFrame:
    """Aggregate 15m bars into ``tf`` bars on UTC-midnight-aligned buckets.

    A bucket is emitted only when all its 15m rows are present; it is stamped with the open time
    of its last 15m bar (``bucket + TF - 15m``), the moment its close is known. ``filled`` is True
    only if every constituent bar is filled.
    """
    if tf == "15m":
        return bars.copy()
    n = TF[tf] // BAR
    bucket = bars.index.floor(TF[tf])
    g = bars.groupby(bucket)
    out = g.agg(
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
        volume=("volume", "sum"),
        trades=("trades", "sum"),
        filled=("filled", "all"),
    )
    out = out[g.size().reindex(out.index).to_numpy() == n]
    out.index = out.index + TF[tf] - BAR
    out.index.name = bars.index.name
    out["trades"] = out["trades"].astype("int64")
    out["filled"] = out["filled"].astype(bool)
    return out[COLUMNS]
