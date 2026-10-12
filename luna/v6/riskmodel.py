"""H3 risk model (spec S-H3-6). Inputs are hourly mark mids P_h (close of the bar that opened at
h - 15m, visible at h) and their log returns r_h = log(P_h / P_{h-1}), visible at h.

- vol: EWMA of r^2 with half-life 336 h; initialised at the 720th valid return with the sample
  variance (ddof 1) of the first 720 valid returns; annualised by sqrt(8760); a missing return
  carries the previous value;
- covariance: the last 60 days (1440 hourly returns ending at t) of the asset set; rows complete
  across the set; correlation from Ledoit & Wolf (2004) constant-correlation shrinkage with the
  analytic intensity on standardised returns (clipped to [0, 1]); Sigma_ij = rho_ij sigma_i sigma_j
  with the EWMA vols; beta_i = Sigma_i,BTC / Sigma_BTC,BTC.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from luna.v6.spec import constants

_C = constants()["h3"]
HL_HOURS = _C["ewma_half_life_days"] * 24
INIT_HOURS = _C["ewma_init_days"] * 24
COV_HOURS = _C["cov_window_days"] * 24
HIST_DAYS = _C["min_mark_history_days"]
LAM = 0.5 ** (1 / HL_HOURS)
ANNUAL = np.sqrt(8760)
MIN_PRESENT = 0.9  # declared: >= 90 % of the 1440 hourly returns present (luna/v6/declared.py D-R1)
MIN_COMPLETE_ROWS = 0.5  # declared: >= 50 % of the 1440 rows complete across the set (D-R2)


class CovarianceUnavailable(RuntimeError):
    pass


def ewma_vol(prices: pd.DataFrame) -> pd.DataFrame:
    """Annualised EWMA vol at every hour of ``prices`` (NaN before initialisation)."""
    r = np.log(prices).diff()
    out = pd.DataFrame(np.nan, index=prices.index, columns=prices.columns)
    for c in prices.columns:
        x = r[c].to_numpy()
        valid = np.flatnonzero(np.isfinite(x))
        if len(valid) < INIT_HOURS:
            continue
        v = np.full(len(x), np.nan)
        var = float(np.var(x[valid[:INIT_HOURS]], ddof=1))
        v[valid[INIT_HOURS - 1]] = var
        for i in range(valid[INIT_HOURS - 1] + 1, len(x)):
            if np.isfinite(x[i]):
                var = LAM * var + (1 - LAM) * x[i] * x[i]
            v[i] = var
        out[c] = np.sqrt(v * 8760)
    return out


def lw_constant_correlation(X: np.ndarray) -> tuple[np.ndarray, float]:
    """(shrunk correlation matrix, delta*) for the T x N return matrix X (standardised inside)."""
    X = np.asarray(X, float)
    Y = (X - X.mean(axis=0)) / X.std(axis=0)
    T, N = Y.shape
    S = Y.T @ Y / T
    var = np.diag(S)
    sq = np.sqrt(var)
    R = S / np.outer(sq, sq)
    rbar = (R.sum() - N) / (N * (N - 1))
    F = rbar * np.outer(sq, sq)
    np.fill_diagonal(F, var)
    Y2 = Y * Y
    pi_mat = Y2.T @ Y2 / T - S * S
    pihat = pi_mat.sum()
    theta = (Y ** 3).T @ Y / T - np.outer(var, np.ones(N)) * S
    np.fill_diagonal(theta, 0.0)
    rho = np.trace(pi_mat) + rbar * (np.outer(1 / sq, sq) * theta).sum()
    gamma = ((F - S) ** 2).sum()
    delta = float(np.clip((pihat - rho) / gamma / T, 0.0, 1.0)) if gamma > 0 else 1.0
    shr = delta * F + (1 - delta) * S
    d = np.sqrt(np.diag(shr))
    return shr / np.outer(d, d), delta


def _window(prices: pd.DataFrame, t: pd.Timestamp) -> pd.DataFrame:
    r = np.log(prices).diff()
    r = r[r.index <= pd.Timestamp(t)]
    return r.iloc[-COV_HOURS:]


def has_history(prices: pd.DataFrame, asset: str, t: pd.Timestamp) -> bool:
    """>= 60 days of mark history: first price at least 60 days before t and >= 90 % of the last
    1440 hourly returns present."""
    t = pd.Timestamp(t)
    s = prices[asset]
    first = s.first_valid_index()
    if first is None or first > t - pd.Timedelta(days=HIST_DAYS):
        return False
    w = _window(prices[[asset]], t)
    return len(w) == COV_HOURS and int(w[asset].notna().sum()) >= MIN_PRESENT * COV_HOURS


@dataclass(frozen=True)
class Cov:
    names: list
    corr: pd.DataFrame
    Sigma: pd.DataFrame
    sigma: pd.Series
    beta: pd.Series
    delta: float
    n_rows: int


def covariance(prices: pd.DataFrame, vol: pd.DataFrame, names: list, t: pd.Timestamp) -> Cov:
    t = pd.Timestamp(t)
    names = list(dict.fromkeys(names))
    if "BTC" not in names:
        raise CovarianceUnavailable("BTC is needed for beta")
    w = _window(prices[names], t)
    if len(w) < COV_HOURS or (w.notna().sum() < MIN_PRESENT * COV_HOURS).any():
        raise CovarianceUnavailable(f"fewer than 90 % of {COV_HOURS} returns for some of {names}")
    full = w.dropna()
    if len(full) < MIN_COMPLETE_ROWS * COV_HOURS:
        raise CovarianceUnavailable("too few complete rows")
    corr, delta = lw_constant_correlation(full.to_numpy())
    sig = vol[names].asof(t) if len(vol.index) and vol.index[0] <= t else pd.Series(np.nan, index=names)
    if not np.isfinite(sig.to_numpy(float)).all() or (sig <= 0).any():
        raise CovarianceUnavailable("EWMA vol not available")
    C = pd.DataFrame(corr, index=names, columns=names)
    S = C * np.outer(sig, sig)
    beta = S["BTC"] / S.loc["BTC", "BTC"]
    return Cov(names, C, S, sig, beta, delta, len(full))
