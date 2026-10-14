"""Model families and the frozen search space (spec S-0.4, S-10.5, section 9).

Families are CLOSED (S-0.4): Elastic Net, logistic Elastic Net and LightGBM (regressor / classifier).
Anything else raises. The wrappers are thin: preprocessing (linear: ``features.Preprocessor`` fitted on T_fit
only; LightGBM: ``features.raw_matrix``) is the caller's job (``luna.v7.cv``). No hyperparameter outside
S-10.5 is introduced; the remaining library settings are sklearn / LightGBM defaults or the technical
settings listed in AMB-11 (convergence limits, determinism, single thread).

* ``en``: ``sklearn.linear_model.ElasticNet(alpha, l1_ratio)`` on ``y_reg``.
* ``logit_en``: ``LogisticRegression(solver="saga", l1_ratio, C = 1 / (alpha * n))`` on ``y_pos`` (AMB-11:
  matches the mean-loss scaling of ElasticNet; sklearn >= 1.8 deprecates ``penalty=``, a float
  ``l1_ratio`` in (0, 1) selects the elastic-net penalty).
* ``lgb_reg`` / ``lgb_clf``: LightGBM with the S-10.5 fixed settings, early stopping (100 rounds, max 1500
  trees) on the ``T_sel`` loss (l2 / logloss) in ``fit``; ``refit`` uses exactly ``best_iter`` trees, no
  early stopping.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

import lightgbm as lgb
import numpy as np
import pandas as pd
from sklearn.linear_model import ElasticNet, LogisticRegression

from luna.v7.constants import FROZEN

S = FROZEN.search
Family = Literal["en", "lgb_reg", "logit_en", "lgb_clf"]
ALLOWED_FAMILIES: tuple[str, ...] = ("en", "lgb_reg", "logit_en", "lgb_clf")
REGRESSORS = ("en", "lgb_reg")
CLASSIFIERS = ("logit_en", "lgb_clf")
SEED = S.seed
LINEAR_MAX_ITER = 1000  # AMB-11
LINEAR_TOL = 1e-4  # AMB-11 (sklearn default)


@dataclass(frozen=True)
class Config:
    family: str
    params: dict = field(default_factory=dict)
    seed: int = SEED

    def __post_init__(self) -> None:
        if self.family not in ALLOWED_FAMILIES:
            raise ValueError(f"model family {self.family!r} is not allowed (S-0.4): {ALLOWED_FAMILIES}")
        linear = self.family in ("en", "logit_en")
        need = ("alpha", "l1_ratio") if linear else ("num_leaves", "min_data_in_leaf")
        missing = [k for k in need if k not in self.params]
        if missing:
            raise ValueError(f"{self.family}: missing params {missing}")

    @property
    def is_regressor(self) -> bool:
        return self.family in REGRESSORS


def search_space(h: int) -> list[Config]:
    """S-10.5: 6 EN + 6 LightGBM regressors + fixed logistic EN + fixed LightGBM classifier (14 per h)."""
    if h not in FROZEN.horizons_h:
        raise ValueError(f"unknown horizon {h}")
    cfgs = [Config("en", {"alpha": a, "l1_ratio": r}) for a in S.en_alpha for r in S.en_l1]
    for n in S.lgb_leaves:
        cfgs += [Config("lgb_reg", {"num_leaves": n, "min_data_in_leaf": m}) for m in S.lgb_min_data]
    cfgs.append(Config("logit_en", {"alpha": S.logit[0], "l1_ratio": S.logit[1]}))
    cfgs.append(Config("lgb_clf", {"num_leaves": S.lgb_clf[0], "min_data_in_leaf": S.lgb_clf[1]}))
    return cfgs


@dataclass(frozen=True)
class Fitted:
    cfg: Config
    model: Any
    best_iteration: int | None = None  # LightGBM only: the tree count used by predict / refit

    def predict(self, X: pd.DataFrame) -> np.ndarray:  # noqa: N803
        """Regressors: y_reg estimate. Classifiers: P(y_pos = 1)."""
        f = self.cfg.family
        if f == "en":
            return np.asarray(self.model.predict(X), dtype=np.float64)
        if f == "logit_en":
            return np.asarray(self.model.predict_proba(X)[:, 1], dtype=np.float64)
        return np.asarray(self.model.predict(X, num_iteration=self.best_iteration), dtype=np.float64)


def _lgb_params(cfg: Config) -> dict:
    fx = S.lgb_fixed
    reg = cfg.family == "lgb_reg"
    return {
        "objective": "regression" if reg else "binary",
        "metric": "l2" if reg else "binary_logloss",
        "learning_rate": fx["learning_rate"],
        "lambda_l2": fx["lambda_l2"],
        "feature_fraction": fx["feature_fraction"],
        "bagging_fraction": fx["bagging_fraction"],
        "bagging_freq": fx["bagging_freq"],
        "num_leaves": int(cfg.params["num_leaves"]),
        "min_data_in_leaf": int(cfg.params["min_data_in_leaf"]),
        "seed": cfg.seed,
        "bagging_seed": cfg.seed,
        "feature_fraction_seed": cfg.seed,
        "deterministic": True,
        "force_row_wise": True,
        "num_threads": 1,  # reproducibility (AMB-11)
        "verbosity": -1,
    }


def _check_xy(X: pd.DataFrame, y, name: str) -> np.ndarray:  # noqa: N803
    y = np.asarray(y, dtype=np.float64)
    if len(X) != len(y) or len(y) == 0:
        raise ValueError(f"{name}: X / y lengths differ or are empty")
    if not np.isfinite(y).all():
        raise ValueError(f"{name}: non-finite target")
    return y


def _fit_linear(cfg: Config, X: pd.DataFrame, y: np.ndarray) -> Fitted:  # noqa: N803
    a, r = float(cfg.params["alpha"]), float(cfg.params["l1_ratio"])
    if cfg.family == "en":
        m = ElasticNet(alpha=a, l1_ratio=r, max_iter=LINEAR_MAX_ITER, tol=LINEAR_TOL, random_state=cfg.seed)
        return Fitted(cfg, m.fit(X, y))
    if len(np.unique(y)) != 2:
        raise ValueError("logit_en needs both classes in y")
    m = LogisticRegression(
        solver="saga", l1_ratio=r, C=1.0 / (a * len(y)),
        max_iter=LINEAR_MAX_ITER, tol=LINEAR_TOL, random_state=cfg.seed,
    )
    return Fitted(cfg, m.fit(X, y))


def _lgb_data(X: pd.DataFrame, y: np.ndarray, params: dict, ref=None) -> lgb.Dataset:  # noqa: N803
    return lgb.Dataset(X, label=y, params={"min_data_in_leaf": params["min_data_in_leaf"], "verbosity": -1},
                       reference=ref, free_raw_data=False)


def fit(
    cfg: Config, X: pd.DataFrame, y, *, X_sel: pd.DataFrame | None = None, y_sel=None  # noqa: N803
) -> Fitted:
    """Fit on (X, y). LightGBM requires (X_sel, y_sel) and early-stops on that loss only (S-10.4/10.5)."""
    yv = _check_xy(X, y, "fit")
    if cfg.family in ("en", "logit_en"):
        return _fit_linear(cfg, X, yv)
    if X_sel is None or y_sel is None:
        raise ValueError("LightGBM fit needs (X_sel, y_sel) for early stopping on the T_sel loss")
    ys = _check_xy(X_sel, y_sel, "fit sel")
    p = _lgb_params(cfg)
    dtr = _lgb_data(X, yv, p)
    dsel = _lgb_data(X_sel, ys, p, ref=dtr)
    booster = lgb.train(
        p, dtr, num_boost_round=int(S.lgb_fixed["max_trees"]), valid_sets=[dsel], valid_names=["sel"],
        callbacks=[lgb.early_stopping(int(S.lgb_fixed["early_stopping"]), verbose=False)],
    )
    best = int(booster.best_iteration) if booster.best_iteration > 0 else int(booster.current_iteration())
    return Fitted(cfg, booster, max(best, 1))


def refit(cfg: Config, best_iter: int | None, X_fit: pd.DataFrame, y_fit) -> Fitted:  # noqa: N803
    """Refit on T_fit, selected hyperparameters; LightGBM: exactly ``best_iter`` trees, no early stop."""
    yv = _check_xy(X_fit, y_fit, "refit")
    if cfg.family in ("en", "logit_en"):
        return _fit_linear(cfg, X_fit, yv)
    if best_iter is None or int(best_iter) < 1:
        raise ValueError("LightGBM refit needs best_iter >= 1 (the early-stopped tree count)")
    p = _lgb_params(cfg)
    booster = lgb.train(p, _lgb_data(X_fit, yv, p), num_boost_round=int(best_iter))
    return Fitted(cfg, booster, int(best_iter))
