"""Reference prices, HALTED and executable-bar rules (spec S-5.2, S-5.4, S-4.6; E10).

Frames are normalised kline frames (``open_ts`` ns, ``o,h,l,c,vol_base``, ``known_at``). 1-minute bars are
R5, 15-minute bars R3. No price is interpolated or invented; a missing price is ``None``.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd

from luna.v7.data.instances import Instance
from luna.v7.timeutil import MIN, H

BAR_15M = 15 * MIN


def halted(bars1m: pd.DataFrame, tau_ns: int) -> bool:
    """S-5.4: no volume > 0 1-minute bar opening in the half-open window [tau - 60 min, tau)."""
    t = bars1m["open_ts"].to_numpy(np.int64)
    v = bars1m["vol_base"].to_numpy(np.float64)
    return not bool(((t >= tau_ns - H) & (t < tau_ns) & (v > 0)).any())


def _open_at(bars: pd.DataFrame, tau_ns: int) -> float | None:
    hit = bars.loc[bars["open_ts"] == tau_ns, "o"]
    if len(hit) == 0 or not np.isfinite(float(hit.iloc[0])):
        return None
    return float(hit.iloc[0])


def P(bars1m: pd.DataFrame, bars15: pd.DataFrame, tau: int) -> float | None:  # noqa: N802
    """S-5.2: open of the 1m bar starting at tau; if that bar is absent or its open is unusable (NaN /
    non-positive), the open of the 15m bar starting at tau; else None."""
    v = _open_at(bars1m, tau)
    if v is None or v <= 0:
        v = _open_at(bars15, tau)  # 1m bar absent or its open unusable (NaN / non-positive)
    return v if v is not None and v > 0 else None


def first_bar_within(bars1m: pd.DataFrame, tau: int, window_ns: int = H) -> tuple[int, float] | None:
    """Data-gap rule (S-5.4): first 1m bar opening in [tau, tau + window]; (open_ts, open) or None."""
    sel = bars1m[(bars1m["open_ts"] >= tau) & (bars1m["open_ts"] <= tau + window_ns)]
    if len(sel) == 0:
        return None
    row = sel.loc[sel["open_ts"].idxmin()]
    return int(row["open_ts"]), float(row["o"])


def first_executable_after(
    bars1m: pd.DataFrame, tau: int, instance: Instance
) -> tuple[int, float] | None:
    """S-5.4 exit from a halted instrument: first volume > 0 1m bar of ``instance`` opening >= tau.

    Returns (fill_ts = bar close time, price = bar close). Bars outside the instance (E10) are ignored: the
    bar must open at/after ``first_bar_ts`` and close by ``last_trade_ts``. The instance end used is the
    reconstructed (retrospective) one; this is admissible because the fill is booked only once the bar has
    closed. Bars whose close is not finite and positive are not executable (skipped; no price is invented).
    No usable bar -> None (the caller applies the terminal / invalid rules).
    """
    t = bars1m["open_ts"].to_numpy(np.int64)
    ok = (t >= tau) & (bars1m["vol_base"].to_numpy(np.float64) > 0) & (t >= instance.first_bar_ts)
    if instance.last_trade_ts is not None:
        ok &= t + MIN <= instance.last_trade_ts
    c = bars1m["c"].to_numpy(np.float64)
    ok &= np.isfinite(c) & (c > 0)  # a bar without a usable close is not executable (no price invented)
    if not ok.any():
        return None
    row = bars1m[ok].sort_values("open_ts").iloc[0]
    return int(row["open_ts"]) + MIN, float(row["c"])


def _num(x: Any) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def mark_at_settle(
    funding_row: Any, mark15: pd.DataFrame, last15: pd.DataFrame, settle_ts: int
) -> float | None:
    """S-4.6 order: record mark -> mark-price 15m bar ending at settlement (close) -> last-price close."""
    m = _num(funding_row.get("mark_at_settle")) if funding_row is not None else None
    if m is not None:
        return m
    for bars in (mark15, last15):
        hit = bars.loc[bars["open_ts"] == settle_ts - BAR_15M, "c"]
        if len(hit):
            c = _num(hit.iloc[0])
            if c is not None:
                return c
    return None
