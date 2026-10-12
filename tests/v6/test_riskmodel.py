"""S-H3-6 risk model: EWMA vol (half-life 336 h, init = sample variance of the first 30 days),
60-day Ledoit-Wolf constant-correlation shrinkage on standardised returns, beta to BTC."""

import numpy as np
import pandas as pd
import pytest

from luna.v6 import riskmodel as rm

T0 = pd.Timestamp("2025-06-01T00:00Z")


def prices(n_hours=24 * 120, vols=None, seed=0, corr=0.5):
    rng = np.random.default_rng(seed)
    names = ["BTC", "A", "B", "C"]
    vols = vols or {"BTC": 0.5, "A": 1.0, "B": 0.8, "C": 1.5}
    C = np.full((4, 4), corr) + (1 - corr) * np.eye(4)
    L = np.linalg.cholesky(C)
    z = rng.normal(size=(n_hours, 4)) @ L.T
    sd = np.array([vols[n] for n in names]) / np.sqrt(8760)
    r = z * sd
    idx = pd.date_range(T0, periods=n_hours, freq="h")
    return pd.DataFrame(100 * np.exp(np.cumsum(r, axis=0)), index=idx, columns=names)


def test_ewma_vol_recursion_and_init():
    p = prices()
    r = np.log(p).diff()
    v = rm.ewma_vol(p)
    lam = 0.5 ** (1 / 336)
    x = r["A"].dropna().to_numpy()
    var = x[:720].var(ddof=1)
    for val in x[720:2000]:
        var = lam * var + (1 - lam) * val * val
    t = r["A"].dropna().index[1999]
    assert v["A"].loc[t] == pytest.approx(np.sqrt(var * 8760))
    assert np.isnan(v["A"].loc[r["A"].dropna().index[700]])  # before 30 days of returns: no vol


def test_ewma_vol_is_pit():
    p = prices()
    v = rm.ewma_vol(p)
    t = p.index[2500]
    p2 = p.copy()
    p2.loc[t + pd.Timedelta("1h"):] *= 3
    assert rm.ewma_vol(p2)["A"].loc[t] == pytest.approx(v["A"].loc[t])


def test_ledoit_wolf_constant_correlation_matches_the_reference_formula():
    rng = np.random.default_rng(1)
    X = rng.normal(size=(300, 5)) @ np.linalg.cholesky(np.full((5, 5), 0.4) + 0.6 * np.eye(5)).T
    corr, delta = rm.lw_constant_correlation(X)
    # reference (Ledoit & Wolf 2004) on standardised data
    Y = (X - X.mean(0)) / X.std(0)
    T, N = Y.shape
    S = Y.T @ Y / T
    var = np.diag(S)
    sq = np.sqrt(var)
    R = S / np.outer(sq, sq)
    rbar = (R.sum() - N) / (N * (N - 1))
    F = rbar * np.outer(sq, sq)
    np.fill_diagonal(F, var)
    Y2 = Y ** 2
    pi_mat = Y2.T @ Y2 / T - S ** 2
    pihat = pi_mat.sum()
    gamma = ((F - S) ** 2).sum()
    rho_diag = np.trace(pi_mat)
    term1 = (Y ** 3).T @ Y / T
    term2 = np.outer(var, np.ones(N)) * S
    theta = term1 - term2
    np.fill_diagonal(theta, 0)
    rho_off = rbar * (np.outer(1 / sq, sq) * theta).sum()
    kappa = (pihat - (rho_diag + rho_off)) / gamma
    d = max(0.0, min(1.0, kappa / T))
    shr = d * F + (1 - d) * S
    exp = shr / np.outer(np.sqrt(np.diag(shr)), np.sqrt(np.diag(shr)))
    assert delta == pytest.approx(d) and corr == pytest.approx(exp)


def test_covariance_uses_the_last_60_days_only_and_ewma_vols():
    p = prices(n_hours=24 * 150)
    t = p.index[-1] + pd.Timedelta("1h")
    vol = rm.ewma_vol(p)
    cov = rm.covariance(p, vol, ["BTC", "A", "B"], t)
    sig = vol.asof(t)
    assert cov.Sigma.loc["A", "A"] == pytest.approx(sig["A"] ** 2)
    p2 = p.copy()
    p2.loc[: t - pd.Timedelta("61D")] *= np.exp(np.random.default_rng(9).normal(0, 0.05, (len(p2.loc[: t - pd.Timedelta("61D")]), 4)))
    assert rm.covariance(p2, vol, ["BTC", "A", "B"], t).corr.equals(cov.corr)
    assert cov.beta["BTC"] == pytest.approx(1.0)


def test_missing_covariance_history_is_reported():
    p = prices(n_hours=24 * 150)
    p.loc[p.index[-24 * 40]:, "C"] = np.nan  # C stopped 40 days ago -> < 90 % of 1440 hours
    t = p.index[-1] + pd.Timedelta("1h")
    with pytest.raises(rm.CovarianceUnavailable):
        rm.covariance(p, rm.ewma_vol(p), ["BTC", "A", "C"], t)
    assert not rm.has_history(p, "C", t) and rm.has_history(p, "A", t)
