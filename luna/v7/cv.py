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
* EN and LightGBM regressors are selected separately (S-7.2 A2 / A3); the search space is the frozen one
  (``models.search_space``); samples are put in canonical order and duplicate keys are rejected.
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
from luna.v7.models import Config, Convergence, Fitted
from luna.v7.timeutil import D, ns_array
from luna.v7.windows import W_HOLD, Fold, final_blocks

PURGE_NS = FROZEN.cv.purge_ns
ISO_LO, ISO_HI = FROZEN.cv.iso_clip
KEY = ["decision_ts", "symbol", "side"]
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
    if samples.duplicated(KEY).any():
        raise ValueError("duplicate (decision_ts, symbol, side) keys in samples")
    for c in ("y_reg", "y_pos"):
        if not np.isfinite(samples[c].to_numpy(np.float64)).all():
            raise ValueError(f"{c}: non-finite label (invalid labels are excluded upstream)")


def canonical(samples: pd.DataFrame, horizon: int | None = None) -> pd.DataFrame:
    """Validated samples in the canonical order (decision_ts, symbol, side), index reset (S-17 determinism):
    the result never depends on the input row order. Duplicate (decision_ts, symbol, side) keys raise."""
    _validate(samples, horizon)
    return samples.sort_values(KEY, kind="stable").reset_index(drop=True)


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


def date_weights(dates) -> np.ndarray:
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
        w = date_weights(dates)
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
        self._iso.fit(x, t, sample_weight=date_weights(dates))
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
    fits: tuple[tuple[Config, Convergence | None], ...] = ()  # solver convergence of every candidate fit


@dataclass
class _Clf:
    fitted: Fitted
    iso: IsotonicCalibrator


@dataclass
class FoldModel:
    """One fold's models. ``reg`` / ``lin_cal`` / ``selection`` are keyed by regressor family (``en`` = the
    A2 ranker, ``lgb_reg`` = the A3 ranker; each selected only among its own 6 configs); ``clf`` by classifier
    family (``logit_en`` for A2 / A1, ``lgb_clf`` for A3 / A1). ``test_idx`` are positional indices into the
    canonically sorted samples (see ``canonical``)."""

    fold: Fold
    horizon: int
    selection: dict[str, Selection]
    reg: dict[str, Fitted]
    lin_cal: dict[str, LinearCalibrator]
    clf: dict[str, _Clf]
    pre: Preprocessor
    cols: tuple[str, ...]
    test_idx: np.ndarray | None = None
    test_pred: pd.DataFrame | None = None

    def predict(self, df: pd.DataFrame) -> pd.DataFrame:
        """``yhat_<fam>`` / ``yhat_cal_<fam>`` per regressor family, ``p_<fam>`` / ``p_<fam>_cal`` per
        classifier family."""
        out = pd.DataFrame(index=df.index)
        for fam, f in self.reg.items():
            out[f"yhat_{fam}"] = f.predict(_design(f.cfg, df, self.cols, self.pre))
            out[f"yhat_cal_{fam}"] = self.lin_cal[fam].predict(out[f"yhat_{fam}"].to_numpy())
        for fam, c in self.clf.items():
            p = c.fitted.predict(_design(c.fitted.cfg, df, self.cols, self.pre))
            out[f"p_{fam}"] = p
            out[f"p_{fam}_cal"] = c.iso.predict(p)
        return out

    def convergence(self) -> tuple[dict, ...]:
        """Solver convergence of every linear fit of this fold (AMB-26): the T_tr candidate fits of the
        selection and the T_fit refits. LightGBM fits have no convergence status (``converged`` None)."""
        rows: list[dict] = []

        def add(stage: str, cfg: Config, c: Convergence | None) -> None:
            rows.append(dict(fold_id=self.fold.fold_id, horizon=self.horizon, stage=stage, family=cfg.family,
                             params=tuple(sorted(cfg.params.items())),
                             converged=None if c is None else c.converged,
                             n_iter=None if c is None else c.n_iter,
                             max_iter=None if c is None else c.max_iter,
                             messages=() if c is None else c.messages))

        for fam in sorted(self.selection):
            for cfg, c in self.selection[fam].fits:
                add("select", cfg, c)
        for fam in sorted(self.reg):
            add("refit", self.reg[fam].cfg, self.reg[fam].convergence)
        for fam in sorted(self.clf):
            add("refit", self.clf[fam].fitted.cfg, self.clf[fam].fitted.convergence)
        return tuple(rows)


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

    ``configs`` must be members of one regressor family of the frozen ``search_space(horizon)`` (S-10.5): EN
    and LightGBM are selected separately (A2 / A3 each have their own model). Candidates are fit on T_tr only
    (linear preprocessing is fitted on T_tr rows); a candidate whose IC is undefined ranks last.
    ``samples`` must be canonical (``canonical``) and ``idx`` its ``split``.
    """
    cands = list(configs)
    if not cands or any(not c.is_regressor for c in cands):
        raise ValueError("select_hparams takes regressor configs only (classifiers are not selected)")
    if len({c.family for c in cands}) != 1:
        raise ValueError("select_hparams selects within one family (EN and LightGBM are separate rankers)")
    space = models.search_space(horizon)
    if any(c not in space for c in cands):
        raise ValueError("config outside the frozen search space (S-10.5)")
    cols = features_for_horizon(horizon)
    tr, sel = samples.iloc[idx["tr"]], samples.iloc[idx["sel"]]
    if len(tr) == 0 or len(sel) == 0:
        raise ValueError("empty T_tr or T_sel after the purge")
    pre_tr = Preprocessor(cols).fit(tr, _ts(tr), fold)
    scores: list[tuple[Config, float]] = []
    fits: list[tuple[Config, Convergence | None]] = []
    best: tuple[float, Fitted] | None = None
    for c in cands:
        f = _fit_tr(c, "y_reg", tr, sel, cols, pre_tr)
        ic = _sel_ic(f, sel, cols, pre_tr)
        scores.append((c, ic))
        fits.append((c, f.convergence))
        key = ic if np.isfinite(ic) else -np.inf
        if best is None or key > best[0]:
            best = (key, f)
    assert best is not None
    if best[0] == -np.inf:
        raise ValueError("no candidate has a defined T_sel date-level IC")
    return Selection(best[1].cfg, best[1].best_iteration, float(best[0]), tuple(scores), tuple(fits))


def _build(fold: Fold, samples: pd.DataFrame, horizon: int, families: Sequence[str]) -> FoldModel:
    samples = canonical(samples, horizon)
    idx = split(samples, fold)
    if min(len(idx[k]) for k in ("tr", "sel", "cal")) == 0:
        raise ValueError("empty T_tr / T_sel / T_cal after the purge")
    space = models.search_space(horizon)
    cols = features_for_horizon(horizon)
    tr, sel, cal = (samples.iloc[idx[k]] for k in ("tr", "sel", "cal"))
    fit_df = samples.iloc[np.concatenate([idx["tr"], idx["sel"]])]
    pre_tr = Preprocessor(cols).fit(tr, _ts(tr), fold)
    pre = Preprocessor(cols).fit(fit_df, _ts(fit_df), fold)  # T_fit only (L5)
    cal_dates = decision_dates(cal["decision_ts"])

    selection: dict[str, Selection] = {}
    reg: dict[str, Fitted] = {}
    lin: dict[str, LinearCalibrator] = {}
    for fam in families:
        s = select_hparams([c for c in space if c.family == fam], samples, idx, fold, horizon)
        rc = s.cfg
        r = models.refit(rc, s.best_iter, _design(rc, fit_df, cols, pre), fit_df["y_reg"].to_numpy())
        selection[fam], reg[fam] = s, r
        lin[fam] = LinearCalibrator().fit(r.predict(_design(rc, cal, cols, pre)), cal["y_reg"], cal_dates)

    clf: dict[str, _Clf] = {}
    for c in (c for c in space if not c.is_regressor):
        n_trees = None
        if c.family == "lgb_clf":  # tree count early-stopped on T_sel; logit_en has no stop phase
            n_trees = _fit_tr(c, "y_pos", tr, sel, cols, pre_tr).best_iteration
        r = models.refit(c, n_trees, _design(c, fit_df, cols, pre), fit_df["y_pos"].to_numpy())
        iso = IsotonicCalibrator().fit(r.predict(_design(c, cal, cols, pre)), cal["y_pos"], cal_dates)
        clf[c.family] = _Clf(r, iso)
    return FoldModel(fold, horizon, selection, reg, lin, clf, pre, cols)


def run_fold(fold: Fold, samples: pd.DataFrame, horizon: int) -> FoldModel:
    """Per regressor family select (T_sel IC over its own 6 configs), refit on T_fit, calibrate on T_cal only;
    fixed classifiers likewise; predict the test quarter (S-10.4 1-5, S-7.2 A2 = EN, A3 = LightGBM)."""
    m = _build(fold, samples, horizon, models.REGRESSORS)
    canon = canonical(samples, horizon)
    m.test_idx = split(canon, fold)["test"]
    m.test_pred = m.predict(canon.iloc[m.test_idx])
    return m


def final_model(samples: pd.DataFrame, horizon: int, family: str) -> FoldModel:
    """Final model before W_HOLD (S-10.4 step 6) for one regressor family: hyperparameters selected on the
    FINAL T_sel (``windows.final_blocks()``), refit on the final T_fit, calibrators on the final T_cal. No
    test prediction is made. Any row with ``decision_ts`` or ``label_known_at`` at or after the holdout start
    is refused (sealing, fail closed): there is no refit inside the holdout."""
    if family not in models.REGRESSORS:
        raise ValueError(f"final_model family must be one of {models.REGRESSORS}")
    _validate(samples, horizon)
    seal = W_HOLD[0]
    if (ns_array(samples["decision_ts"], "decision_ts") >= seal).any() or (
        ns_array(samples["label_known_at"], "label_known_at") >= seal
    ).any():
        raise ValueError("final_model refuses rows at or after the W_HOLD start (sealed)")
    return _build(final_blocks(), samples, horizon, (family,))
