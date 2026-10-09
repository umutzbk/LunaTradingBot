"""V4 diagnostics (spec §3): D-PERSIST, D-DISP, D-H5CELLS and venue-basis statistics.

Diagnostics are reported only. They are not configs, not trials, never enter GO / NO-GO and never feed
config selection (nothing in ``strategies`` / ``stats`` reads them). D-PERSIST deliberately looks at
the 7 days AFTER t (it measures persistence); that is why it is quarantined here.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

from luna.v4.funding import HOURS_PER_YEAR, known_rates

WEEK = pd.Timedelta(days=7)
DISP_DAYS = 30
F7_HOURS = 168


def _spearman(x: pd.Series, y: pd.Series) -> float:
    j = x.dropna().index.intersection(y.dropna().index)
    if len(j) < 3:
        return math.nan
    rx, ry = x[j].rank(), y[j].rank()
    if rx.std() == 0 or ry.std() == 0:
        return math.nan
    return float(np.corrcoef(rx, ry)[0, 1])


def f7_cross_section(funding: dict[str, pd.DataFrame], t: pd.Timestamp) -> dict[str, float]:
    out = {}
    for a, f in funding.items():
        k = known_rates(f, t)
        w = k.loc[k.index >= t - pd.Timedelta(hours=F7_HOURS), "funding_rel"]
        out[a] = float(w.mean()) * HOURS_PER_YEAR if len(w) else math.nan
    return out


def _forward_mean(f: pd.DataFrame, t: pd.Timestamp, span: pd.Timedelta = WEEK) -> float:
    w = f.loc[(f.index >= t) & (f.index < t + span), "funding_rel"]
    return float(w.mean()) * HOURS_PER_YEAR if len(w) else math.nan


def _daily_mean(f: pd.DataFrame) -> pd.Series:
    return f["funding_rel"].groupby(f.index.floor("D")).mean()


def d_persist(funding: dict[str, pd.DataFrame], weeks: list[pd.Timestamp]) -> dict:
    """Weekly non-overlapping cross-sectional Spearman of F7(t) vs the next 7 days' mean funding, and
    the half-life of daily funding (pooled AR(1) of per-asset demeaned daily means)."""
    rhos = []
    for t in weeks:
        rho = _spearman(
            pd.Series(f7_cross_section(funding, t)),
            pd.Series({a: _forward_mean(f, t) for a, f in funding.items()}),
        )
        if not math.isnan(rho):
            rhos.append(rho)
    xs, ys = [], []
    for f in funding.values():
        d = _daily_mean(f)
        d = d - d.mean()
        xs.extend(d.iloc[:-1].to_numpy())
        ys.extend(d.iloc[1:].to_numpy())
    phi = float(np.dot(xs, ys) / np.dot(xs, xs)) if xs and np.dot(xs, xs) > 0 else math.nan
    half_life = math.log(0.5) / math.log(phi) if 0 < phi < 1 else math.nan
    mean = float(np.mean(rhos)) if rhos else math.nan
    return {
        "weeks": len(rhos),
        "mean_spearman": mean,
        "ar1_daily": phi,
        "half_life_days": half_life,
        "warning": bool(not (mean > 0)),
    }


def dispersion(funding: dict[str, pd.DataFrame], t: pd.Timestamp) -> float:
    """Cross-asset std of trailing 30-day mean relative funding, ended periods only."""
    vals = []
    for f in funding.values():
        k = known_rates(f, t)
        w = k.loc[k.index >= t - pd.Timedelta(days=DISP_DAYS), "funding_rel"]
        if len(w):
            vals.append(float(w.mean()))
    return float(np.std(vals, ddof=1)) if len(vals) > 1 else math.nan


def d_disp_from(ret: pd.Series, disp: pd.Series) -> dict:
    """Split daily returns by dispersion above / at-or-below its expanding median (days <= t)."""
    med = disp.expanding().median()
    above = disp > med
    valid = disp.notna() & med.notna()
    r = ret.reindex(disp.index)

    def stats(mask):
        x = r[mask & valid].dropna()
        return {"n": int(len(x)), "mean": float(x.mean()) if len(x) else math.nan}

    return {"above": stats(above), "below_or_equal": stats(~above)}


def d_disp(h2_ret: pd.Series, funding: dict[str, pd.DataFrame]) -> dict:
    disp = pd.Series({t: dispersion(funding, t) for t in h2_ret.index}, dtype="float64")
    return d_disp_from(h2_ret, disp)


def d_h5cells(table: pd.DataFrame, funding: dict[str, pd.DataFrame], y: float = 0.10) -> dict:
    """The 2x2 F7 (>= 10%) x basis (B24 >= B30med) table over eligible asset-days: counts and the mean
    funding of the following 7 days (annualised; a diagnostic, never a P&L)."""
    cells: dict[str, list[float]] = {"HF-WB": [], "HF-NB": [], "LF-WB": [], "LF-NB": []}
    for (t, a), r in table.iterrows():
        if not r["elig_h1"] or pd.isna(r["B24"]) or pd.isna(r["B30med"]) or pd.isna(r["F7"]):
            continue
        key = ("HF" if r["F7"] >= y else "LF") + "-" + ("WB" if r["B24"] >= r["B30med"] else "NB")
        cells[key].append(_forward_mean(funding[a], t) if a in funding else math.nan)
    return {
        k: {
            "n": len(v),
            "next7d_funding_ann": float(np.nanmean(v)) if v and not np.isnan(v).all() else math.nan,
        }
        for k, v in cells.items()
    }


def basis_stats(basis: dict[str, pd.Series]) -> dict:
    """Venue-basis distribution (15m quantiles) and lag-1 autocorrelation of the daily median."""
    out = {}
    for a, b in basis.items():
        b = b.dropna()
        if b.empty:
            out[a] = {"p05": math.nan, "p50": math.nan, "p95": math.nan, "acf1_daily": math.nan, "n_days": 0}
            continue
        daily = b.groupby(b.index.floor("D")).median()
        acf = float(daily.autocorr(1)) if len(daily) > 2 else math.nan
        out[a] = {
            "p05": float(b.quantile(0.05)),
            "p50": float(b.quantile(0.5)),
            "p95": float(b.quantile(0.95)),
            "acf1_daily": acf,
            "n_days": int(len(daily)),
        }
    return out
