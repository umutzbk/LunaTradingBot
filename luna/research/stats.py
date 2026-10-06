"""Small statistics toolkit: day-block bootstrap, Benjamini-Hochberg FDR, profit factor, CVaR."""

from __future__ import annotations

import math

import numpy as np


def block_bootstrap_ci(
    values: np.ndarray, days: np.ndarray, n: int = 2000, ci: float = 0.90, seed: int = 0
) -> tuple[float, float, float]:
    """(mean, lo, hi) of the mean of `values` with a day-block bootstrap.

    Whole days are resampled with replacement (as many days as observed), so trades of the same
    day stay together; each resample's statistic is the mean over all trades it contains.
    """
    v = np.asarray(values, dtype=float)
    d = np.asarray(days)
    if v.shape != d.shape:
        raise ValueError("values and days must have the same shape")
    if v.size == 0:
        return (math.nan, math.nan, math.nan)
    _, inv = np.unique(d, return_inverse=True)
    sums = np.bincount(inv, weights=v)
    counts = np.bincount(inv).astype(float)
    k = len(sums)
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, k, size=(n, k))
    means = sums[idx].sum(axis=1) / counts[idx].sum(axis=1)
    tail = (1.0 - ci) / 2.0
    lo, hi = np.quantile(means, [tail, 1.0 - tail])
    return (float(v.mean()), float(lo), float(hi))


def benjamini_hochberg(pvals: np.ndarray, q: float) -> np.ndarray:
    """Step-up BH: reject the k smallest p-values, k = max{i : p_(i) <= i*q/m}. Bool mask."""
    p = np.asarray(pvals, dtype=float)
    m = p.size
    mask = np.zeros(m, dtype=bool)
    if m == 0:
        return mask
    order = np.argsort(p, kind="stable")
    passed = p[order] <= q * np.arange(1, m + 1) / m
    if passed.any():
        k = int(np.flatnonzero(passed).max())
        mask[order[: k + 1]] = True
    return mask


def profit_factor(pnl: np.ndarray) -> float:
    """Sum of gains / |sum of losses|; inf if there are gains and no losses, NaN if no trades."""
    x = np.asarray(pnl, dtype=float)
    if x.size == 0:
        return math.nan
    gains = float(x[x > 0].sum())
    losses = float(-x[x < 0].sum())
    if losses == 0.0:
        return math.inf if gains > 0 else math.nan
    return gains / losses


def cvar(x: np.ndarray, alpha: float = 0.05) -> float:
    """Lower-tail expected shortfall: mean of the worst ceil(alpha * n) observations (>= 1)."""
    v = np.sort(np.asarray(x, dtype=float))
    v = v[np.isfinite(v)]
    if v.size == 0:
        return math.nan
    k = max(1, math.ceil(alpha * v.size - 1e-9))
    return float(v[:k].mean())
