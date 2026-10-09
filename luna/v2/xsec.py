"""Cross-sectional momentum: rebalance clock, eligibility and raw targets (spec §2.1, plan P4/P5).

Ranking uses daily closes from ``resample_causal(…, "1d")`` (index = the day's decision stamp,
23:45 UTC), read only up to the decision stamp. The L-day return of an asset at decision ``t`` is
``close[t] / close[t - L days] - 1``; an asset whose close at either stamp is missing is ineligible.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

import numpy as np
import pandas as pd

from luna.types import AccountKind
from luna.v2.timeframe import resample_causal

MIN_DAILY_HISTORY = 30


@dataclass(frozen=True)
class XSecParams:
    lookback_days: int
    k: int
    gross: float = 1.0
    vol_target: float | None = None
    rebalance: Literal["weekly", "daily"] = "weekly"

    @property
    def params_id(self) -> str:
        pid = f"L{self.lookback_days}_k{self.k}_g{self.gross:g}_v{self.vol_target or 0:g}"
        return pid + ("_daily" if self.rebalance == "daily" else "")


def rebalance_stamps(index: pd.DatetimeIndex, rebalance: Literal["weekly", "daily"]) -> pd.DatetimeIndex:
    """15m bars whose close ends a UTC day (23:45 open); weekly keeps Sundays only (P5)."""
    sel = (index.hour == 23) & (index.minute == 45)
    if rebalance == "weekly":
        sel &= index.dayofweek == 6
    return index[sel]


def daily_closes(panel: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Daily closes (complete UTC days only) per asset, indexed by the day's decision stamp. A day
    made only of forward-filled bars (no trade at all) is NaN: it is neither history nor a return."""
    cols = {}
    for a, df in panel.items():
        d = resample_causal(df, "1d")
        cols[a] = d["close"].where(~d["filled"].astype(bool))
    return pd.DataFrame(cols).sort_index()


def lookback_returns(daily_close: pd.DataFrame, t: pd.Timestamp, lookback_days: int) -> pd.Series:
    past = t - pd.Timedelta(days=lookback_days)
    if t not in daily_close.index or past not in daily_close.index:
        return pd.Series(math.nan, index=daily_close.columns)
    return daily_close.loc[t] / daily_close.loc[past] - 1.0


def eligible(
    daily_close: pd.DataFrame,
    real_at: dict[str, bool],
    universe: list[str],
    lookback_days: int,
    t: pd.Timestamp,
) -> list[str]:
    """Universe assets with a real 15m bar at ``t``, a finite L-day return at ``t``, at least
    max(lookback + 1, 31) daily closes and at least 30 finite daily returns up to ``t`` (lookback
    return and the 30-day covariance; a missing day removes two returns)."""
    hist = daily_close.loc[daily_close.index <= t]
    rets = lookback_returns(daily_close, t, lookback_days)
    need = max(lookback_days + 1, MIN_DAILY_HISTORY + 1)
    out = []
    for a in universe:
        if a not in daily_close.columns or not real_at.get(a, False):
            continue
        if int(hist[a].notna().sum()) < need or not math.isfinite(float(rets.get(a, math.nan))):
            continue
        if int(np.log(hist[a]).diff().notna().sum()) < MIN_DAILY_HISTORY:  # covariance needs 30 returns
            continue
        out.append(a)
    return out


def raw_targets(
    daily_close: pd.DataFrame,
    assets: list[str],
    params: XSecParams,
    kind: AccountKind,
    t: pd.Timestamp,
) -> pd.Series:
    """Raw weights (fraction of equity) before the risk projection.

    Perp: k_eff = min(k, n // 2) longs (highest return) at +gross / (2 k_eff) and k_eff shorts
    (lowest) at -gross / (2 k_eff). Spot: k_eff = min(k, n) longs at 1 / k_eff. Ties: asset name.
    """
    out = pd.Series(0.0, index=list(assets), dtype=float)
    if not assets:
        return out
    rets = lookback_returns(daily_close, t, params.lookback_days)[list(assets)]
    n = len(assets)
    desc = sorted(assets, key=lambda a: (-float(rets[a]), a))
    if kind == "spot":
        k = min(params.k, n)
        for a in desc[:k]:
            out[a] = 1.0 / k
        return out
    k = min(params.k, n // 2)
    if k == 0:
        return out
    asc = sorted(assets, key=lambda a: (float(rets[a]), a))
    for a in desc[:k]:
        out[a] = params.gross / (2 * k)
    for a in asc[:k]:
        out[a] = -params.gross / (2 * k)
    return out
