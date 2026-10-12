"""H2a (spec S-H2-1): the only magnitude forecast that H1 and H3 decisions use, and its validity
gate (S-H2-3). H2b lives in ``forecast_diag`` and no decision path imports it.

    X_i(t)   = FL30: mean of the periods p in [t - 720h, t - 1h] visible at t (>= 648 periods)
    Y_i(s,h) = mean of the periods p in [s, s + h - 1h] as of t (>= 0.9 h known), needs s + h <= t
    fit(t,h) = pooled equal-weight OLS of clip(Y) on 1 + clip(X) over the daily samples
               S0 <= s, s + h <= t; clip = the fit sample's own 1 % / 99 % quantiles
    forecast = a + b * clip(X_i(t)) with the same fit's cut-offs; no X -> no forecast
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from luna.v6.data import FundingPanel
from luna.v6.spec import constants

DAY = pd.Timedelta("1D")
HOUR = pd.Timedelta("1h")
_C = constants()["h2a"]
FL_HOURS, FL_MIN = 720, 648
LABEL_MIN_SHARE = 0.9
MIN_DATES = _C["min_history_dates"]
MIN_MEAN_ASSETS = _C["min_mean_assets_per_date"]
S0_MIN_ASSETS = 10
Q_LO, Q_HI = _C["winsor_quantiles"]
HORIZONS = tuple(_C["horizons_hours"])


class ModelWarmup(RuntimeError):
    """MODEL WARMUP: fewer than the minimum history of sample dates; no new positions."""


def winsor_ols(x: np.ndarray, y: np.ndarray) -> tuple[float, float, float, float, float, float]:
    """(a, b, lo_x, hi_x, lo_y, hi_y): OLS of clip(y) on 1 + clip(x), cut-offs from the sample."""
    x, y = np.asarray(x, float), np.asarray(y, float)
    lo_x, hi_x = (float(v) for v in np.quantile(x, [Q_LO, Q_HI]))
    lo_y, hi_y = (float(v) for v in np.quantile(y, [Q_LO, Q_HI]))
    xc, yc = np.clip(x, lo_x, hi_x), np.clip(y, lo_y, hi_y)
    xm, ym = xc.mean(), yc.mean()
    vx = float(((xc - xm) ** 2).sum())
    b = float(((xc - xm) * (yc - ym)).sum() / vx) if vx > 0 else 0.0
    return float(ym - b * xm), b, lo_x, hi_x, lo_y, hi_y


@dataclass(frozen=True)
class H2aFit:
    h: int
    t: pd.Timestamp
    a: float
    b: float
    lo_x: float
    hi_x: float
    lo_y: float
    hi_y: float
    n_dates: int
    n_samples: int
    mean_assets: float
    max_sample_date: pd.Timestamp
    max_label_end: pd.Timestamp

    def predict(self, x: pd.Series) -> pd.Series:
        return self.a + self.b * x.clip(self.lo_x, self.hi_x)


@dataclass
class H2a:
    panel: FundingPanel
    h: int
    _x: dict = field(default_factory=dict, repr=False)
    _y: dict = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        if self.h not in HORIZONS:
            raise ValueError(f"horizon {self.h} is not a frozen H2a horizon {HORIZONS}")
        first = self.panel.rel.index.min()
        self._day0 = first.ceil("D") if first != first.floor("D") else first

    def x(self, s: pd.Timestamp) -> pd.Series:
        """FL30 as seen at s (cached: X(s) depends only on what was visible at s)."""
        s = pd.Timestamp(s)
        if s not in self._x:
            self._x[s] = self.panel.fl(s, FL_HOURS, FL_MIN)
        return self._x[s]

    def s0(self, until: pd.Timestamp | None = None) -> pd.Timestamp | None:
        end = pd.Timestamp(until) if until is not None else self.panel.rel.index.max() + HOUR
        s = self._day0
        while s <= end:
            if int(self.x(s).notna().sum()) >= S0_MIN_ASSETS:
                return s
            s += DAY
        return None

    def y(self, s: pd.Timestamp, t: pd.Timestamp) -> pd.Series:
        """Fit label (>= 90 % of the h periods known by t, else NaN). Cached once every period of
        every asset is known (it cannot change later)."""
        s = pd.Timestamp(s)
        hit = self._y.get(s)
        if hit is not None:
            return hit
        m, share = self.panel.label(s, self.h, t)
        out = m.where(share >= LABEL_MIN_SHARE)
        if bool((share >= 1.0).all()):
            self._y[s] = out
        return out

    def fit(self, t: pd.Timestamp) -> H2aFit | None:
        t = pd.Timestamp(t)
        s0 = self.s0(until=t)
        if s0 is None:
            return None
        xs, ys, dates, per_date = [], [], [], []
        s = s0
        while s + self.h * HOUR <= t:
            x, y = self.x(s), self.y(s, t)
            ok = x.notna() & y.notna()
            if ok.any():
                xs.append(x[ok].to_numpy())
                ys.append(y[ok].to_numpy())
                dates.append(s)
                per_date.append(int(ok.sum()))
            s += DAY
        if len(dates) < MIN_DATES or float(np.mean(per_date)) < MIN_MEAN_ASSETS:
            return None
        a, b, lo_x, hi_x, lo_y, hi_y = winsor_ols(np.concatenate(xs), np.concatenate(ys))
        return H2aFit(self.h, t, a, b, lo_x, hi_x, lo_y, hi_y, len(dates), int(sum(per_date)),
                      float(np.mean(per_date)), dates[-1], dates[-1] + self.h * HOUR)

    def forecast(self, t: pd.Timestamp) -> pd.Series:
        """F_i(t, h) per asset (NaN without FL30). Raises ModelWarmup before the minimum history."""
        f = self.fit(t)
        if f is None:
            raise ModelWarmup(f"H2a({self.h}) has no fit at {t}")
        return f.predict(self.x(t))


# --- validity gate (S-H2-3) -------------------------------------------------------------------------

_G = constants()["h2_gate"]
CAL_LO, CAL_HI = _G["calibration_range"]
FS_EPS = _G["fs_epsilon"]
GATE_B = _G["bootstrap_B"]
GATE_SEED = _G["seed"]
GATE_BLOCK = {168: _G["gate_block_days"]["168"], 720: _G["gate_block_days"]["720"]}
H1_TOP = _G["h1_top_n"]
PARTIAL_MIN = _G["missing_label_partial_min_share"]
MIN_DATES_168 = 60
MIN_WINDOWS_720 = 8
K_MIN = constants()["h3"]["min_k_prime"]


@dataclass(frozen=True)
class GateRecord:
    """One evaluation date: the eligible assets at s and the PIT forecast made at s (never a refit)."""

    s: pd.Timestamp
    eligible: list
    fhat: pd.Series


@dataclass(frozen=True)
class GateResult:
    cls: str
    R: float
    c: float
    ci_R: tuple
    ci_c: tuple
    n_dates: int
    min_evidence: bool
    n_imputed: int
    dates: list
    fs: list
    rs: list
    c_demeaned: float
    n_boot_dropped: int = 0  # replicates with an undefined ratio, dropped from the percentile (D-G3)


def min_evidence_720(dates: list) -> bool:
    if not dates:
        return False
    return int((max(dates) - min(dates)) / pd.Timedelta("30D")) + 1 >= MIN_WINDOWS_720


def classify(min_ev: bool, R: float, ci_R: tuple, c: float, ci_c: tuple) -> str:
    """INVALID needs the minimum evidence and wins over VALID; everything else is INCONCLUSIVE."""
    if min_ev and ((ci_R[1] < CAL_LO) or (ci_R[0] > CAL_HI) or (ci_c[1] <= 0)):
        return "INVALID"
    if min_ev and math.isfinite(R) and CAL_LO <= R <= CAL_HI and ci_c[0] > 0:
        return "VALID"
    return "INCONCLUSIVE"


def _realised(m: float, share: float, f: float, side: str) -> tuple[float, bool]:
    """Gate label: >= 90 % known -> mean; 50-90 % -> mean of the known; < 50 % -> min(0, F) for a
    top (T) asset, max(0, F) for a bottom (B) asset (no gain is ever assumed)."""
    if share >= PARTIAL_MIN and math.isfinite(m):
        return m, False
    return (min(0.0, f) if side == "T" else max(0.0, f)), True


def _block_index(n: int, block: int, B: int, seed: int) -> np.ndarray | None:
    if n == 0 or n < block:
        return None
    rng = np.random.default_rng(seed)
    nb = -(-n // block)
    starts = rng.integers(0, n - block + 1, size=(B, nb))
    idx = (starts[:, :, None] + np.arange(block)[None, None, :]).reshape(B, nb * block)[:, :n]
    return idx


def gate(panel: FundingPanel, records: list[GateRecord], *, kind: str, k: int | None, h: int,
         window_end: pd.Timestamp, B: int = GATE_B, seed: int = GATE_SEED) -> GateResult:
    if kind not in ("H3", "H1") or (kind == "H3") != (h == 168) or (kind == "H3" and k is None):
        raise ValueError(f"gate({kind}, k={k}, h={h}) is not a frozen config")
    end = pd.Timestamp(window_end)
    dates, fs, rs, n_imp = [], [], [], 0
    stats = []  # per date: n, sx, sy, sxx, sxy (slope sample)
    for r in sorted(records, key=lambda r: r.s):
        s = pd.Timestamp(r.s)
        if s + h * HOUR > end:
            continue
        f = r.fhat.reindex(list(r.eligible)).dropna()
        order = sorted(f.index, key=lambda a: (-f[a], a))
        if kind == "H3":
            kp = min(k, len(order) // 2)
            if kp < K_MIN:
                continue
            top, bot = order[:kp], order[-kp:]
        else:
            pos = [a for a in order if f[a] > 0]
            if not pos:
                continue
            top, bot = pos[: min(H1_TOP, len(pos))], []
        m, share = panel.label(s, h, end)
        yt = []
        for a in top:
            v, imp = _realised(float(m.get(a, np.nan)), float(share.get(a, 0.0)), float(f[a]), "T")
            yt.append(v)
            n_imp += imp
        yb = []
        for a in bot:
            v, imp = _realised(float(m.get(a, np.nan)), float(share.get(a, 0.0)), float(f[a]), "B")
            yb.append(v)
            n_imp += imp
        if kind == "H3":
            fs.append(float(f[top].mean() - f[bot].mean()))
            rs.append(float(np.mean(yt) - np.mean(yb)))
        else:
            fs.append(float(f[top].sum()))
            rs.append(float(np.sum(yt)))
        dates.append(s)
        ok = [a for a in f.index if float(share.get(a, 0.0)) >= PARTIAL_MIN and math.isfinite(float(m.get(a, np.nan)))]
        x = f[ok].to_numpy(float)
        y = m[ok].to_numpy(float)
        stats.append((len(ok), x.sum(), y.sum(), (x * x).sum(), (x * y).sum(), x, y))
    n = len(dates)
    FS, RS = np.array(fs), np.array(rs)
    R = float(RS.sum() / FS.sum()) if n and FS.sum() > FS_EPS else math.nan
    S = np.array([st[:5] for st in stats]) if n else np.zeros((0, 5))

    def slope(tot: np.ndarray) -> np.ndarray:
        nn, sx, sy, sxx, sxy = tot.T if tot.ndim == 2 else tot
        den = nn * sxx - sx * sx
        with np.errstate(invalid="ignore", divide="ignore"):
            return np.where(den > 0, (nn * sxy - sx * sy) / den, np.nan)

    c = float(slope(S.sum(axis=0))) if n else math.nan
    xs = [st[5] - st[5].mean() for st in stats if len(st[5])]
    ys = [st[6] - st[6].mean() for st in stats if len(st[6])]
    xd, yd = (np.concatenate(xs), np.concatenate(ys)) if xs else (np.array([]), np.array([]))
    c_dm = float((xd * yd).sum() / (xd * xd).sum()) if len(xd) and (xd * xd).sum() > 0 else math.nan
    min_ev = (n >= MIN_DATES_168) if h == 168 else min_evidence_720(dates)
    idx = _block_index(n, GATE_BLOCK[h], B, seed)
    n_drop = 0
    if idx is None or not math.isfinite(R):
        ci_R = ci_c = (math.nan, math.nan)
    else:
        fsb, rsb = FS[idx].sum(axis=1), RS[idx].sum(axis=1)
        with np.errstate(invalid="ignore", divide="ignore"):
            Rb = np.where(fsb > FS_EPS, rsb / fsb, np.nan)
        cb = slope(S[idx].sum(axis=1))
        n_drop = int(np.isnan(Rb).sum())
        ci_R = tuple(float(v) for v in np.nanquantile(Rb, [0.025, 0.975]))
        ci_c = tuple(float(v) for v in np.nanquantile(cb, [0.025, 0.975]))
    cls = classify(min_ev, R, ci_R, c, ci_c) if math.isfinite(R) else "INCONCLUSIVE"
    return GateResult(cls, R, c, ci_R, ci_c, n, min_ev, n_imp, dates, fs, rs, c_dm, n_drop)
