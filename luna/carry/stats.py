"""V5 backtest statistics, guardrails and the per-row verdict (frozen protocol rev 3, §10).

The moving block bootstrap and the risk metrics re-implement the V4 method (centred moving block,
not circular; one-sided bounds) in V5 code; a test checks them equal to V4 ``stats`` on synthetic
data. V4 code is never imported here.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import pandas as pd

B = 10_000
SEED = 20261010
BLOCKS = (28, 14)
ALPHA = 0.05
DAYS_PER_YEAR = 365

# §10 guardrail thresholds (fixed before any outcome)
G2_MDD_PP = 0.03
G3_ES_PP, G3_WORST_PP = 0.005, 0.01
G4_SHARPE = 0.30


@dataclass(frozen=True)
class BootResult:
    p: float
    m_hat: float
    boot_means: np.ndarray

    def lower_bound(self, alpha: float) -> float:
        if not len(self.boot_means):
            return math.nan
        return float(self.m_hat - np.quantile(self.boot_means - self.m_hat, 1 - alpha))

    def upper_bound(self, alpha: float) -> float:
        if not len(self.boot_means):
            return math.nan
        return float(self.m_hat - np.quantile(self.boot_means - self.m_hat, alpha))


def mbb(x, block: int, B: int = B, seed: int = SEED) -> BootResult:  # noqa: N803
    """Moving block bootstrap of the mean: ceil(n / b) overlapping blocks drawn uniformly from the
    n - b + 1 starts, concatenated and truncated to n; one-sided p = (1 + #{m* - m >= m}) / (B + 1)."""
    x = np.asarray(x, dtype="float64")
    n = len(x)
    if n == 0 or n < block or not np.isfinite(x).all():
        return BootResult(math.nan, float(x.mean()) if n else math.nan, np.array([]))
    m_hat = float(x.mean())
    nb = -(-n // block)
    last = n - (nb - 1) * block
    cs = np.concatenate([[0.0], np.cumsum(x)])
    rng = np.random.default_rng(seed)
    starts = rng.integers(0, n - block + 1, size=(B, nb))
    full = (cs[starts[:, :-1] + block] - cs[starts[:, :-1]]).sum(axis=1) if nb > 1 else np.zeros(B)
    part = cs[starts[:, -1] + last] - cs[starts[:, -1]]
    means = (full + part) / n
    p = (1 + int(np.sum(means - m_hat >= m_hat))) / (B + 1)
    return BootResult(p, m_hat, means)


def holm(pvals: dict[str, float], alpha: float) -> dict[str, dict]:
    clean = {k: (v if v is not None and not math.isnan(v) else 1.0) for k, v in pvals.items()}
    order = sorted(clean, key=lambda k: (clean[k], k))
    m = len(order)
    out, going = {}, True
    for i, k in enumerate(order):
        a = alpha / (m - i)
        going = going and clean[k] <= a
        out[k] = {"p": pvals[k], "alpha": a, "reject": bool(going)}
    return out


def sharpe(r: pd.Series) -> float:
    r = pd.Series(r).dropna()
    sd = r.std(ddof=1) if len(r) > 1 else math.nan
    return float(r.mean() / sd * math.sqrt(DAYS_PER_YEAR)) if sd and sd > 0 else math.nan


def max_drawdown(r: pd.Series) -> float:
    eq = np.cumprod(1 + pd.Series(r).fillna(0.0).to_numpy())
    if not len(eq):
        return 0.0
    peak = np.maximum.accumulate(np.concatenate([[1.0], eq]))[1:]
    return float(np.max(1 - eq / peak))


def worst_day(r: pd.Series) -> float:
    return float(pd.Series(r).min()) if len(r) else math.nan


def es975(r: pd.Series) -> float:
    v = np.sort(pd.Series(r).dropna().to_numpy())
    if not len(v):
        return math.nan
    return float(v[: max(1, math.ceil(0.025 * len(v)))].mean())


def risk_metrics(ret: pd.Series, net_pnl: float) -> dict:
    return {"net": net_pnl, "mdd": max_drawdown(ret), "es": es975(ret), "worst": worst_day(ret), "sharpe": sharpe(ret)}


def guardrails(w: dict, e: dict, cost2x_delta_mean: float, e_only_kill: bool) -> dict[str, bool]:
    """§10 G1-G6; True = breached."""
    return {
        "G1": bool(e["net"] - w["net"] <= 0),
        "G2": bool(e["mdd"] > w["mdd"] + G2_MDD_PP),
        "G3": bool(e["es"] < w["es"] - G3_ES_PP or e["worst"] < w["worst"] - G3_WORST_PP),
        "G4": bool(e["sharpe"] < w["sharpe"] - G4_SHARPE),
        "G5": bool(e_only_kill),
        "G6": bool(cost2x_delta_mean <= 0),
    }


@dataclass(frozen=True)
class RowSeries:
    carry_net: pd.Series
    ret: pd.Series
    net_pnl: float
    kill: bool = False


def delta_carry(w: RowSeries, e: RowSeries, B: int = B, seed: int = SEED) -> dict:  # noqa: N803
    if not w.carry_net.index.equals(e.carry_net.index):
        raise ValueError("W and E daily series must be paired on the same days")
    d = (e.carry_net - w.carry_net).to_numpy()
    boots = {b: mbb(d, b, B, seed) for b in BLOCKS}
    lb = {b: boots[b].lower_bound(ALPHA) for b in BLOCKS}
    ub = {b: boots[b].upper_bound(ALPHA) for b in BLOCKS}
    return {"mean": float(d.mean()), "lb95": lb, "ub95": ub, "p": {b: boots[b].p for b in BLOCKS},
            "pass": bool(all(lb[b] > 0 for b in BLOCKS))}


def row_verdict(w: RowSeries, e: RowSeries, cost2x_delta_mean: float, e_only_kill: bool,
                B: int = B, seed: int = SEED) -> dict:  # noqa: N803
    """One row's verdict: ADVANTAGE / CARRY ONLY / NO ADVANTAGE SHOWN / WORSE (§10 table)."""
    dc = delta_carry(w, e, B, seed)
    g = guardrails(risk_metrics(w.ret, w.net_pnl), risk_metrics(e.ret, e.net_pnl), cost2x_delta_mean, e_only_kill)
    if dc["pass"]:
        v = "CARRY ONLY" if any(g.values()) else "ADVANTAGE"
    elif dc["ub95"][28] >= 0:
        v = "NO ADVANTAGE SHOWN"
    else:
        v = "WORSE"
    return {"verdict": v, "delta_carry": dc, "guardrails": g,
            "broken": sorted(k for k, x in g.items() if x)}
