"""Pre-committed V2 parameter grids (plan P1-P4), defined in time and converted to bars per timeframe.

A grid point is dropped when its shortest length is under ``MIN_BARS`` bars of the timeframe.
ATR window = max(1 day, 14 bars). Time stops are max(30 days, 2 x the slow / channel length),
counted in 15m bars because the engine counts held bars on the 15m index.
"""

from __future__ import annotations

import pandas as pd

from luna.types import AccountKind
from luna.v2.timeframe import TF, Timeframe, bars_for

_D = pd.Timedelta(days=1)

MIN_BARS = 3
TSMOM_SPANS: tuple[tuple[pd.Timedelta, pd.Timedelta], ...] = (
    (1 * _D, 5 * _D),
    (4 * _D, 20 * _D),
    (12 * _D, 60 * _D),
)
STOP_ATR = (2.0, 3.0)
BREAKOUT_N: tuple[pd.Timedelta, ...] = (2 * _D, 10 * _D, 30 * _D)
XSEC_LOOKBACK_DAYS = (14, 30, 90)
XSEC_K = (1, 2)
RISK_GRID = (
    {"gross": 1.0, "vol_target": 0.10},
    {"gross": 1.0, "vol_target": 0.15},
    {"gross": 1.5, "vol_target": 0.10},
    {"gross": 1.5, "vol_target": 0.15},
)
FAMILY_TIMEFRAMES: dict[AccountKind, dict[str, tuple[Timeframe, ...]]] = {
    "perp": {"tsmom": ("15m", "1h", "4h", "1d"), "breakout": ("15m", "1h", "4h", "1d"), "xsec": ("1d",)},
    "spot": {"tsmom": ("1h", "4h", "1d"), "breakout": ("1h", "4h", "1d"), "xsec": ("1d",)},
}


def atr_bars(tf: Timeframe) -> int:
    return max(bars_for(_D, tf), 14)


def max_hold_bars(lookback: pd.Timedelta) -> int:
    """Time stop in 15m bars: max(30 days, 2 x lookback)."""
    return bars_for(max(30 * _D, 2 * pd.Timedelta(lookback)), "15m")


def _too_short(length: pd.Timedelta, tf: Timeframe) -> bool:
    return length < MIN_BARS * TF[tf]


def tsmom_grid(tf: Timeframe) -> list[dict]:
    return [
        {
            "fast": bars_for(f, tf),
            "slow": bars_for(s, tf),
            "atr_n": atr_bars(tf),
            "stop_atr": k,
            "max_hold": max_hold_bars(s),
        }
        for f, s in TSMOM_SPANS
        if not _too_short(f, tf)
        for k in STOP_ATR
    ]


def breakout_grid(tf: Timeframe) -> list[dict]:
    return [
        {"donchian": bars_for(n, tf), "atr_n": atr_bars(tf), "k_atr": k, "max_hold": max_hold_bars(n)}
        for n in BREAKOUT_N
        if not _too_short(n, tf)
        for k in STOP_ATR
    ]


def xsec_grid(kind: AccountKind) -> list[dict]:
    base = [{"lookback_days": L, "k": k} for L in XSEC_LOOKBACK_DAYS for k in XSEC_K]
    if kind == "spot":
        return base
    return [{**b, **r} for b in base for r in RISK_GRID]
