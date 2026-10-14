"""Temporal CV, purge, three-block calibration and date-level IC (spec S-10.1 - S-10.5, L4 - L7, L9a, L13).

* Every split is temporal (S-10.1). Blocks follow ``windows`` (T_tr / T_sel / T_cal, S-10.4).
* Purge (S-10.3): a sample is admissible for a later block / fold starting at T only if
  ``label_known_at <= T - 24 h``; applied at every block boundary (tr -> sel, sel -> cal, cal -> test).
* Hyperparameters are chosen on T_sel by the date-level Spearman IC of the regressors only (never P&L,
  classifiers are not selected). The base models are refit on T_fit = T_tr u T_sel with the selected
  hyperparameters (LightGBM: the early-stopped tree count); calibrators are fitted on T_cal predictions of
  exactly that refit model. T_cal is never used to fit, select or early-stop.
* Linear models use ``features.Preprocessor`` (fitted on the fit rows only, L5; during candidate selection
  on T_tr rows only), LightGBM raw values.
* ``samples`` (ruling R2): a DataFrame with decision_ts, symbol, side, h, label_known_at, y_reg, y_pos and the
  FEATURES columns; one row per (decision, symbol, side) for one horizon; invalid labels excluded upstream.
* A "decision date" is the UTC calendar day of ``decision_ts`` (AMB-12).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

import numpy as np
import pandas as pd
from sklearn.isotonic import IsotonicRegression

from luna.v7 import models
from luna.v7.constants import FROZEN
from luna.v7.features import FEATURES, Preprocessor, features_for_horizon, raw_matrix
from luna.v7.models import Config, Fitted
from luna.v7.timeutil import D, ns_array
from luna.v7.windows import Fold, final_blocks

PURGE_NS = FROZEN.cv.purge_ns
ISO_LO, ISO_HI = FROZEN.cv.iso_clip
REQUIRED = ("decision_ts", "symbol", "side", "h", "label_known_at", "y_reg", "y_pos", *FEATURES)


# ------------------------------------------------------------------------------------ purge and splits


def admissible(label_known_at, block_start):
    """S-10.3: label_known_at <= T - 24 h (scalar -> bool, array -> bool array)."""
    lka = ns_array(np.atleast_1d(label_known_at), "label_known_at")
    ok = lka <= int(block_start) - PURGE_NS
    return bool(ok[0]) if np.ndim(label_known_at) == 0 else ok


def decision_dates(decision_ts) -> np.ndarray:
    """UTC calendar day (ns of its midnight) of each decision time (AMB-12)."""
    return ns_array(decision_ts, "decision_ts") // D * D


def _validate(samples: pd.DataFrame, horizon: int | None = None) -> None:
    missing = [c for c in REQUIRED if c not in samples.columns]
    if missing:
        raise ValueError(f"samples lack columns {missing[:5]}")
    if horizon is not None:
        if horizon not in FROZEN.horizons_h:
            raise ValueError(f"unknown horizon {horizon}")
        if len(samples) and not (samples["h"] == horizon).all():
            raise ValueError("samples hold rows of another horizon than the requested one")
    for c in ("y_reg", "y_pos"):
        if not np.isfinite(samples[c].to_numpy(np.float64)).all():
            raise ValueError(f"{c}: non-finite label (invalid labels are excluded upstream)")


def split(samples: pd.DataFrame, fold: Fold) -> dict[str, np.ndarray]:
    """Positional row indices of the purged blocks ``tr`` / ``sel`` / ``cal`` and of ``test``.

    A block row is in the block's decision-time span and admissible for the NEXT block start (S-10.3/S-10.4);
    the test quarter is the plain decision-time span (its labels are never used for fitting).
    """
    _validate(samples)
    ts = ns_array(samples["decision_ts"], "decision_ts")
    lka = ns_array(samples["label_known_at"], "label_known_at")
    nxt = {"tr": fold.sel[0], "sel": fold.cal[0], "cal": fold.test_lo}
    spans = {"tr": fold.tr, "sel": fold.sel, "cal": fold.cal}
    out: dict[str, np.ndarray] = {}
    for k, (lo, hi) in spans.items():
        out[k] = np.flatnonzero((ts >= lo) & (ts < hi) & (lka <= nxt[k] - PURGE_NS))
    out["test"] = np.flatnonzero((ts >= fold.test_lo) & (ts < fold.test_hi))
    return out


# ------------------------------------------------------------------------------------ date-level IC


def date_ics(pred, y, dates) -> pd.Series:
    """Per-date Spearman correlation (average ranks); dates with < 2 distinct pred or y values are skipped."""
    p, t, d = np.asarray(pred, np.float64), np.asarray(y, np.float64), np.asarray(dates)
    if not (len(p) == len(t) == len(d)):
        raise ValueError("pred, y and dates lengths differ")
    if not (np.isfinite(p).all() and np.isfinite(t).all()):
        raise ValueError("non-finite pred / y in date_ic")
    df = pd.DataFrame({"p": p, "y": t, "d": d})
    out: dict = {}
    for day, g in df.groupby("d", sort=True):
        if g["p"].nunique() < 2 or g["y"].nunique() < 2:
            continue
        out[day] = float(np.corrcoef(g["p"].rank().to_numpy(), g["y"].rank().to_numpy())[0, 1])
    return pd.Series(out, dtype=np.float64)


def date_ic(pred, y, dates) -> float:
    """Mean over decision dates of the within-date Spearman IC (S-10.5, date-equal weights); NaN if none."""
    s = date_ics(pred, y, dates)
    return float(s.mean()) if len(s) else float("nan")


def date_ic_se(pred, y, dates) -> float:
    """Standard error of the mean date-level IC over dates (std ddof 1 / sqrt(#dates))."""
    s = date_ics(pred, y, dates)
    return float(s.std(ddof=1) / np.sqrt(len(s))) if len(s) > 1 else float("nan")


# ------------------------------------------------------------------------------------ calibrators


def _date_weights(dates) -> np.ndarray:
    """Each decision date carries total weight 1 / #dates (rows within a date are equal)."""
    d = np.asarray(dates)
    if len(d) == 0:
        raise ValueError("empty calibration set")
    _, inv, cnt = np.unique(d, return_inverse=True, return_counts=True)
    return 1.0 / (cnt[inv] * len(cnt))


@dataclass
class LinearCalibrator:
    """y_cal = a + b * yhat; (a, b) by date-equal-weight OLS of y_reg on yhat over T_cal, b clipped >= 0.

    If b clips to 0 (or yhat is constant) the model is constant: a is the date-weighted mean of y (the OLS
    intercept with b fixed at 0, AMB-12) and ``b_is_zero`` is set: entries iff a > 0, ranking by yhat.
    """

    a: float = float("nan")
    b: float = float("nan")

    def fit(self, yhat, y, dates) -> LinearCalibrator:
        x, t = np.asarray(yhat, np.float64), np.asarray(y, np.float64)
        if not (len(x) == len(t) == len(dates)) or not (np.isfinite(x).all() and np.isfinite(t).all()):
            raise ValueError("LinearCalibrator: lengths differ or non-finite input")
        w = _date_weights(dates)
        mx, my = float(w @ x), float(w @ t)
        vx = float(w @ (x - mx) ** 2)
        b = float(w @ ((x - mx) * (t - my))) / vx if vx > 1e-300 else 0.0
        self.b = max(b, 0.0)
        self.a = my - self.b * mx if self.b > 0 else my
        return self

    @property
    def b_is_zero(self) -> bool:
        return self.b == 0.0

    @property
    def zero_slope_entry_ok(self) -> bool:
        """The b = 0 rule (S-10.4): entries iff a > 0 (meaningful when ``b_is_zero``)."""
        return self.a > 0

    def predict(self, yhat) -> np.ndarray:
        return self.a + self.b * np.asarray(yhat, np.float64)


@dataclass
class IsotonicCalibrator:
    """Isotonic regression of y_pos on P-hat, date-equal weights, outputs clipped to [0.001, 0.999]."""

    _iso: IsotonicRegression | None = field(default=None, repr=False)

    def fit(self, p, ypos, dates) -> IsotonicCalibrator:
        x, t = np.asarray(p, np.float64), np.asarray(ypos, np.float64)
        if not (len(x) == len(t) == len(dates)) or not (np.isfinite(x).all() and np.isfinite(t).all()):
            raise ValueError("IsotonicCalibrator: lengths differ or non-finite input")
        self._iso = IsotonicRegression(y_min=ISO_LO, y_max=ISO_HI, increasing=True, out_of_bounds="clip")
        self._iso.fit(x, t, sample_weight=_date_weights(dates))
        return self

    def predict(self, p) -> np.ndarray:
        if self._iso is None:
            raise RuntimeError("IsotonicCalibrator is not fitted")
        return np.clip(self._iso.predict(np.asarray(p, np.float64)), ISO_LO, ISO_HI)


# ------------------------------------------------------------------------------------ fold models


@dataclass(frozen=True)
class Selection:
    cfg: Config
    best_iter: int | None
    ic: float
    scores: tuple[tuple[Config, float], ...]


@dataclass
class _Clf:
    fitted: Fitted
    iso: IsotonicCalibrator


@dataclass
class FoldModel:
    fold: Fold
    horizon: int
    selection: Selection
    reg: Fitted
    lin_cal: LinearCalibrator
    clf: dict[str, _Clf]
    pre: Preprocessor
    cols: tuple[str, ...]
    test_idx: np.ndarray | None = None
    test_pred: pd.DataFrame | None = None

    def predict(self, df: pd.DataFrame) -> pd.DataFrame:
        """yhat / yhat_cal and, per classifier family, ``p_<family>`` and ``p_<family>_cal``."""
        out = pd.DataFrame(index=df.index)
        out["yhat"] = self.reg.predict(_design(self.reg.cfg, df, self.cols, self.pre))
        out["yhat_cal"] = self.lin_cal.predict(out["yhat"].to_numpy())
        for fam, c in self.clf.items():
            p = c.fitted.predict(_design(c.fitted.cfg, df, self.cols, self.pre))
            out[f"p_{fam}"] = p
            out[f"p_{fam}_cal"] = c.iso.predict(p)
        return out


def _design(cfg: Config, df: pd.DataFrame, cols: Sequence[str], pre: Preprocessor) -> pd.DataFrame:
    return pre.transform(df) if cfg.family in ("en", "logit_en") else raw_matrix(df, cols)


def _ts(df: pd.DataFrame) -> np.ndarray:
    return ns_array(df["decision_ts"], "decision_ts")


def _fit_tr(cfg: Config, target: str, tr, sel, cols, pre_tr) -> Fitted:
    """Fit on T_tr; LightGBM early-stops on the T_sel loss (never on T_cal)."""
    return models.fit(
        cfg, _design(cfg, tr, cols, pre_tr), tr[target].to_numpy(),
        X_sel=_design(cfg, sel, cols, pre_tr), y_sel=sel[target].to_numpy(),
    )


def _sel_ic(f: Fitted, sel, cols, pre_tr) -> float:
    pred = f.predict(_design(f.cfg, sel, cols, pre_tr))
    return date_ic(pred, sel["y_reg"], decision_dates(sel["decision_ts"]))


def select_hparams(
    configs: Sequence[Config], samples: pd.DataFrame, idx: dict[str, np.ndarray], fold: Fold, horizon: int
) -> Selection:
    """Pick the regressor with the highest T_sel date-level IC (S-10.5; never P&L). Ties -> first in order.

    Candidates are fit on T_tr only (linear preprocessing is fitted on T_tr rows); a candidate whose IC is
    undefined ranks last. Classifiers are not selected: passing one raises.
    """
    cands = list(configs)
    if not cands or any(not c.is_regressor for c in cands):
        raise ValueError("select_hparams takes regressor configs only (classifiers are not selected)")
    cols = features_for_horizon(horizon)
    tr, sel = samples.iloc[idx["tr"]], samples.iloc[idx["sel"]]
    if len(tr) == 0 or len(sel) == 0:
        raise ValueError("empty T_tr or T_sel after the purge")
    pre_tr = Preprocessor(cols).fit(tr, _ts(tr), fold)
    scores: list[tuple[Config, float]] = []
    best: tuple[float, Fitted] | None = None
    for c in cands:
        f = _fit_tr(c, "y_reg", tr, sel, cols, pre_tr)
        ic = _sel_ic(f, sel, cols, pre_tr)
        scores.append((c, ic))
        key = ic if np.isfinite(ic) else -np.inf
        if best is None or key > best[0]:
            best = (key, f)
    assert best is not None
    if best[0] == -np.inf:
        raise ValueError("no candidate has a defined T_sel date-level IC")
    return Selection(best[1].cfg, best[1].best_iteration, float(best[0]), tuple(scores))


def _build(
    fold: Fold,
    samples: pd.DataFrame,
    horizon: int,
    reg_cfgs: Sequence[Config],
    clf_cfgs: Sequence[Config],
    fixed_reg: Config | None,
) -> FoldModel:
    _validate(samples, horizon)
    idx = split(samples, fold)
    if min(len(idx[k]) for k in ("tr", "sel", "cal")) == 0:
        raise ValueError("empty T_tr / T_sel / T_cal after the purge")
    cols = features_for_horizon(horizon)
    tr, sel, cal = (samples.iloc[idx[k]] for k in ("tr", "sel", "cal"))
    fit_df = samples.iloc[np.concatenate([idx["tr"], idx["sel"]])]
    pre_tr = Preprocessor(cols).fit(tr, _ts(tr), fold)

    if fixed_reg is None:
        selection = select_hparams(reg_cfgs, samples, idx, fold, horizon)
    else:  # final model: the early-stopped count still comes from T_sel
        f = _fit_tr(fixed_reg, "y_reg", tr, sel, cols, pre_tr)
        selection = Selection(fixed_reg, f.best_iteration, _sel_ic(f, sel, cols, pre_tr), ())

    pre = Preprocessor(cols).fit(fit_df, _ts(fit_df), fold)  # T_fit only (L5)
    cal_dates = decision_dates(cal["decision_ts"])

    rc = selection.cfg
    reg = models.refit(rc, selection.best_iter, _design(rc, fit_df, cols, pre), fit_df["y_reg"].to_numpy())
    lin = LinearCalibrator().fit(reg.predict(_design(rc, cal, cols, pre)), cal["y_reg"], cal_dates)

    clf: dict[str, _Clf] = {}
    for c in clf_cfgs:
        if c.is_regressor:
            raise ValueError("classifier slot holds a regressor config")
        stop = _fit_tr(c, "y_pos", tr, sel, cols, pre_tr)
        r = models.refit(c, stop.best_iteration, _design(c, fit_df, cols, pre), fit_df["y_pos"].to_numpy())
        iso = IsotonicCalibrator().fit(r.predict(_design(c, cal, cols, pre)), cal["y_pos"], cal_dates)
        clf[c.family] = _Clf(r, iso)
    return FoldModel(fold, horizon, selection, reg, lin, clf, pre, cols)


def run_fold(
    fold: Fold, samples: pd.DataFrame, horizon: int, *, configs: Sequence[Config] | None = None
) -> FoldModel:
    """Select (T_sel IC), refit on T_fit, calibrate on T_cal only, predict the test quarter (S-10.4 1-5)."""
    cfgs = list(models.search_space(horizon) if configs is None else configs)
    regs = [c for c in cfgs if c.is_regressor]
    clfs = [c for c in cfgs if not c.is_regressor]
    m = _build(fold, samples, horizon, regs, clfs, None)
    m.test_idx = split(samples, fold)["test"]
    m.test_pred = m.predict(samples.iloc[m.test_idx])
    return m


def final_model(
    samples: pd.DataFrame, horizon: int, cfg: Config, *, classifiers: Sequence[Config] | None = None
) -> FoldModel:
    """Final model before W_HOLD (S-10.4 step 6): blocks from ``windows.final_blocks()``; ``cfg`` is the
    pre-selected regressor (its tree count is early-stopped on T_sel). No test prediction is made and no row
    at or after the holdout start is used: there is no refit inside the holdout."""
    if not cfg.is_regressor:
        raise ValueError("final_model takes a regressor config")
    pool = models.search_space(horizon) if classifiers is None else classifiers
    return _build(final_blocks(), samples, horizon, [cfg], [c for c in pool if not c.is_regressor], cfg)
