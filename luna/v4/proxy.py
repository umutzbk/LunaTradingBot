"""Premium proxy diagnostics (spec R3, §7.3 proxy period 2023 -> 2025-10-06).

``premium_proxy`` = (mark close - index close) / index close on 1-hour bars. It is NOT funding: it is
never written to a funding column, never calibrated to a funding level, never used to fill a missing
funding hour and never produces a cash flow. Only rank / sign statistics are computed here:
- proxy-rank persistence (the proxy version of D-PERSIST): weekly cross-sectional Spearman between
  the trailing 7-day mean proxy and the next 7 days' mean proxy;
- sign persistence of the daily mean proxy per asset;
- rank / sign based signal counts.

A static test guarantees that no simulation, statistics or runner module can import this module.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

WEEK = pd.Timedelta(days=7)


def premium_proxy(mark: pd.DataFrame, index: pd.DataFrame) -> pd.Series:
    """Hourly (mark - index) / index from the last 15m close of each hour (hours with both)."""
    if mark is None or index is None or mark.empty or index.empty:
        return pd.Series(dtype="float64", name="premium_proxy", index=pd.DatetimeIndex([], tz="UTC"))
    m = mark["close"].groupby(mark.index.floor("1h")).last()
    i = index["close"].groupby(index.index.floor("1h")).last()
    j = m.index.intersection(i.index)
    out = ((m[j] - i[j]) / i[j]).where(i[j] > 0)
    out.name = "premium_proxy"
    return out.dropna()


def _spearman(x: pd.Series, y: pd.Series) -> float:
    j = x.dropna().index.intersection(y.dropna().index)
    if len(j) < 3:
        return math.nan
    rx, ry = x[j].rank(), y[j].rank()
    if rx.std() == 0 or ry.std() == 0:
        return math.nan
    return float(np.corrcoef(rx, ry)[0, 1])


def _window_mean(s: pd.Series, lo: pd.Timestamp, hi: pd.Timestamp) -> float:
    w = s[(s.index >= lo) & (s.index < hi)]
    return float(w.mean()) if len(w) else math.nan


def proxy_diagnostics(proxies: dict[str, pd.Series], weeks: list[pd.Timestamp]) -> dict:
    rhos, positive = [], 0
    for t in weeks:
        past = pd.Series({a: _window_mean(p, t - WEEK, t) for a, p in proxies.items()})
        nxt = pd.Series({a: _window_mean(p, t, t + WEEK) for a, p in proxies.items()})
        rho = _spearman(past, nxt)
        if not math.isnan(rho):
            rhos.append(rho)
        positive += int((past > 0).sum())
    signs = {}
    for a, p in proxies.items():
        daily = np.sign(p.groupby(p.index.floor("D")).mean())
        signs[a] = float((daily.diff().dropna() == 0).mean()) if len(daily) > 1 else math.nan
    return {
        "series": "premium_proxy (not funding; rank/sign statistics only)",
        "rank_persistence": {"weeks": len(rhos), "mean_spearman": float(np.mean(rhos)) if rhos else math.nan},
        "sign_persistence": signs,
        "signal_counts": {"asset_weeks": len(weeks) * len(proxies), "positive_asset_weeks": positive},
    }


def run_proxy_diagnostics(
    mark: dict[str, pd.DataFrame], index: dict[str, pd.DataFrame], start: pd.Timestamp, end: pd.Timestamp
) -> dict:
    """Proxy-period diagnostics over [start, end) (Stage 1: 2023-01-01 -> 2025-10-06)."""
    proxies = {}
    for a in sorted(mark):
        p = premium_proxy(mark.get(a), index.get(a))
        proxies[a] = p[(p.index >= start) & (p.index < end)]
    first_monday = (start + pd.Timedelta(days=(7 - start.weekday()) % 7)).floor("D")
    weeks = list(pd.date_range(first_monday + WEEK, end - WEEK, freq="7D"))
    out = proxy_diagnostics(proxies, weeks)
    out["window"] = {"start": start.isoformat(), "end": end.isoformat()}
    return out
