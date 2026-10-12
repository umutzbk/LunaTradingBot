"""H2b (spec S-H2-2): DIAGNOSTIC ONLY. No decision, gate or verdict path imports this module
(tests/v6/test_h2b.py checks the imports). Ridge on z-scored FL30, FL7, FL1d and PREM24:

    minimise (1/n) sum (y - a - sum_j b_j z_j)^2 + lambda * sum_j b_j^2, lambda = 0.01, a unpenalised

Each fit: features and y winsorised at the fit sample's 1 % / 99 %, then z-scored with that
sample's mean and std. Samples with a missing feature are dropped; an asset with a missing feature
gets no forecast. Labels, sample dates, horizons, window, S0 and the minimum history as in H2a.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from luna.v6.data import BarPanel, FundingPanel
from luna.v6.forecast import MIN_DATES, MIN_MEAN_ASSETS, Q_HI, Q_LO, H2a
from luna.v6.spec import constants

HOUR = pd.Timedelta("1h")
DAY = pd.Timedelta("1D")
_C = constants()["h2b"]
LAMBDA = _C["ridge_lambda"]
FEATS = _C["features"]  # name -> [window, minimum]


def prem24(mark: BarPanel, index: BarPanel, t: pd.Timestamp) -> pd.Series:
    n, need = FEATS["PREM24_bars"]
    m, i = mark.closed_window(t, n), index.closed_window(t, n)
    r = m / i.reindex_like(m) - 1
    return r.mean().where(r.notna().sum() >= need)


def ridge(Z: np.ndarray, y: np.ndarray, lam: float) -> tuple[float, np.ndarray]:
    n = len(y)
    zm, ym = Z.mean(axis=0), y.mean()
    Zc = Z - zm
    b = np.linalg.solve(Zc.T @ Zc / n + lam * np.eye(Z.shape[1]), Zc.T @ (y - ym) / n)
    return float(ym - zm @ b), b


@dataclass(frozen=True)
class H2bFit:
    h: int
    t: pd.Timestamp
    a: float
    b: np.ndarray
    lo: np.ndarray
    hi: np.ndarray
    mu: np.ndarray
    sd: np.ndarray
    lam: float
    n_dates: int
    max_sample_date: pd.Timestamp


@dataclass
class H2b:
    panel: FundingPanel
    mark: BarPanel
    index: BarPanel
    h: int
    _f: dict = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        self._h2a = H2a(self.panel, self.h)

    def features(self, s: pd.Timestamp) -> pd.DataFrame:
        s = pd.Timestamp(s)
        if s not in self._f:
            self._f[s] = pd.DataFrame({
                "FL30": self.panel.fl(s, *FEATS["FL30"]),
                "FL7": self.panel.fl(s, *FEATS["FL7"]),
                "FL1d": self.panel.fl(s, *FEATS["FL1d"]),
                "PREM24": prem24(self.mark, self.index, s).reindex(self.panel.assets),
            })
        return self._f[s]

    def fit(self, t: pd.Timestamp) -> H2bFit | None:
        t = pd.Timestamp(t)
        s0 = self._h2a.s0(until=t)
        if s0 is None:
            return None
        X, Y, per = [], [], []
        s, last = s0, None
        while s + self.h * HOUR <= t:
            f, y = self.features(s), self._h2a.y(s, t)
            ok = f.notna().all(axis=1) & y.notna()
            if ok.any():
                X.append(f[ok].to_numpy())
                Y.append(y[ok].to_numpy())
                per.append(int(ok.sum()))
                last = s
            s += DAY
        if len(per) < MIN_DATES or float(np.mean(per)) < MIN_MEAN_ASSETS:
            return None
        Xa, Ya = np.vstack(X), np.concatenate(Y)
        lo, hi = np.quantile(Xa, Q_LO, axis=0), np.quantile(Xa, Q_HI, axis=0)
        ylo, yhi = np.quantile(Ya, [Q_LO, Q_HI])
        Xw, Yw = np.clip(Xa, lo, hi), np.clip(Ya, ylo, yhi)
        mu, sd = Xw.mean(axis=0), Xw.std(axis=0)
        sd = np.where(sd > 0, sd, 1.0)
        a, b = ridge((Xw - mu) / sd, Yw, LAMBDA)
        return H2bFit(self.h, t, a, b, lo, hi, mu, sd, LAMBDA, len(per), last)

    def forecast(self, t: pd.Timestamp) -> pd.Series:
        f = self.fit(t)
        if f is None:
            return pd.Series(np.nan, index=self.panel.assets)
        x = self.features(t)
        ok = x.notna().all(axis=1).to_numpy()
        z = (np.clip(x.to_numpy(), f.lo, f.hi) - f.mu) / f.sd
        out = np.where(ok, f.a + np.nan_to_num(z) @ f.b, np.nan)
        return pd.Series(out, index=x.index)
