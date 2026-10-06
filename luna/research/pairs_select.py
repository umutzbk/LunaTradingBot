"""Pair selection (spec 5.3): correlation prefilter, Engle-Granger + Benjamini-Hochberg, stability.

Only data with index in [train_start, train_end) is used. Pairs are canonical "A/B" where A has the
lower ADV over the train window (B is the more liquid hedge asset), one test per unordered pair.

``always_watch`` pairs are always tested and reported in the table, even below ``corr_min`` (they then
carry the "corr" reason and are not part of the BH family), but get no exemption from the prefilter,
BH or stability: if they fail anything they are not selected (spec 5.3: "istisna yok").

Stability is evaluated for FDR passers only; ``stable`` is therefore a subset of ``fdr_pass`` and the
half columns of other rows are NaN.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations

import numpy as np
import pandas as pd
from statsmodels.tsa.stattools import coint

from luna.config import PairsConfig
from luna.features.pairs import half_life
from luna.research.stats import benjamini_hochberg
from luna.universe import adv_usd

_COLUMNS = ["pair", "corr", "p_raw", "p_bh", "hl_1", "hl_2", "beta_1", "beta_2", "reasons"]
_MIN_HOURLY = 30
_MAXLAG = 8  # ADF lag cap (AIC picks within it); the default ~25 lags is 10x slower


@dataclass
class PairSelection:
    tested: int
    prefiltered: int
    fdr_pass: list[str]
    stable: list[str]
    selected: list[str]
    table: pd.DataFrame


def _eg_pvalue(log_a: pd.Series, log_b: pd.Series) -> float:
    """Engle-Granger p-value on 1h log prices (last of each hour)."""
    a = log_a.resample("1h").last()
    b = log_b.resample("1h").last()
    return _eg_hourly(pd.concat([a, b], axis=1).dropna())


def _eg_hourly(both: pd.DataFrame) -> float:
    if len(both) < _MIN_HOURLY:
        return 1.0
    try:
        return float(coint(both.iloc[:, 0], both.iloc[:, 1], trend="c", maxlag=_MAXLAG)[1])
    except Exception:  # degenerate (e.g. constant) series
        return 1.0


def _beta_hl(log_a: pd.Series, log_b: pd.Series) -> tuple[float, float]:
    """OLS beta of log A on log B and half-life (15m bars) of the resulting spread."""
    if len(log_a) < _MIN_HOURLY:
        return float("nan"), float("inf")
    b = log_b.to_numpy()
    bc = b - b.mean()
    denom = float(bc @ bc)
    if denom == 0.0:
        return float("nan"), float("inf")
    a = log_a.to_numpy()
    beta = float(bc @ (a - a.mean())) / denom
    return beta, half_life(log_a - beta * log_b)


def _bh_adjust(p: np.ndarray) -> np.ndarray:
    """Benjamini-Hochberg adjusted p-values (step-up, monotone, capped at 1)."""
    m = len(p)
    order = np.argsort(p)
    scaled = p[order] * m / np.arange(1, m + 1)
    adj = np.empty(m)
    adj[order] = np.minimum.accumulate(scaled[::-1])[::-1]
    return np.minimum(adj, 1.0)


def select_pairs(
    panel: dict[str, pd.DataFrame],
    universe: list[str],
    train_start: pd.Timestamp,
    train_end: pd.Timestamp,
    cfg: PairsConfig,
) -> PairSelection:
    train_start, train_end = pd.Timestamp(train_start), pd.Timestamp(train_end)
    days = max(int(np.ceil((train_end - train_start) / pd.Timedelta(days=1))), 1)
    close: dict[str, pd.Series] = {}
    hourly_close: dict[str, pd.Series] = {}
    adv: dict[str, float] = {}
    for asset in universe:
        bars = panel.get(asset)
        if bars is None:
            continue
        w = bars[(bars.index >= train_start) & (bars.index < train_end)]
        if w.empty:
            continue
        close[asset] = np.log(w["close"])
        hourly_close[asset] = close[asset].resample("1h").last()
        adv[asset] = adv_usd(w, train_end, days=days)

    watch = set(cfg.always_watch)
    mid = train_start + (train_end - train_start) / 2
    rows: list[dict] = []
    legs: dict[str, tuple[str, str]] = {}
    for x, y in combinations(sorted(close), 2):
        a, b = (x, y) if (adv[x], x) <= (adv[y], y) else (y, x)  # a: lower ADV
        key = f"{a}/{b}"
        hourly = pd.concat([hourly_close[a], hourly_close[b]], axis=1).dropna()
        if len(hourly) < _MIN_HOURLY:
            continue
        corr = float(hourly.diff().dropna().corr().iloc[0, 1]) if len(hourly) > 2 else float("nan")
        pre = bool(corr > cfg.corr_min)
        if not pre and key not in watch:
            continue
        legs[key] = (a, b)
        rows.append({"pair": key, "corr": corr, "pre": pre, "p_raw": _eg_hourly(hourly)})

    df = pd.DataFrame(rows, columns=["pair", "corr", "pre", "p_raw"])
    for c in ("p_bh", "hl_1", "hl_2", "beta_1", "beta_2"):
        df[c] = np.nan
    df["reasons"] = ""
    df = df.set_index("pair", drop=False)

    fam = list(df.index[df["pre"].astype(bool)])
    prefiltered = len(fam)
    fdr_pass: list[str] = []
    if prefiltered:
        p = df.loc[fam, "p_raw"].to_numpy(dtype=float)
        mask = benjamini_hochberg(p, cfg.fdr_q)
        df.loc[fam, "p_bh"] = _bh_adjust(p)
        fdr_pass = [k for k, m in zip(fam, mask, strict=True) if m]

    reasons: dict[str, list[str]] = {k: [] for k in df.index}
    for k in df.index:
        if not df.at[k, "pre"]:
            reasons[k].append("corr")
        elif k not in fdr_pass:
            reasons[k].append("fdr")

    stable: list[str] = []
    for k in fdr_pass:
        both = pd.concat([close[legs[k][0]], close[legs[k][1]]], axis=1, join="inner").dropna()
        la, lb = both.iloc[:, 0], both.iloc[:, 1]
        h1 = (la[la.index < mid], lb[lb.index < mid])
        h2 = (la[la.index >= mid], lb[lb.index >= mid])
        (b1, hl1), (b2, hl2) = _beta_hl(*h1), _beta_hl(*h2)
        p1, p2 = _eg_pvalue(*h1), _eg_pvalue(*h2)
        df.loc[k, ["hl_1", "hl_2", "beta_1", "beta_2"]] = [hl1, hl2, b1, b2]
        bad = reasons[k]
        if not (p1 < cfg.stability_p and p2 < cfg.stability_p):
            bad.append("stab_p")
        if not all(cfg.half_life_min <= h <= cfg.half_life_max for h in (hl1, hl2)):
            bad.append("half_life")
        if not (b1 != 0 and abs(b1 - b2) / abs(b1) < cfg.beta_change_max):  # NaN -> False
            bad.append("beta_change")
        if not bad:
            stable.append(k)

    selected = sorted(stable, key=lambda k: df.at[k, "p_bh"])[: cfg.max_pairs]
    for k in stable:
        if k not in selected:
            reasons[k].append("max_pairs")
    df["reasons"] = [",".join(reasons[k]) for k in df.index]
    table = df[_COLUMNS].reset_index(drop=True)
    return PairSelection(
        tested=len(df),
        prefiltered=prefiltered,
        fdr_pass=fdr_pass,
        stable=stable,
        selected=selected,
        table=table,
    )
