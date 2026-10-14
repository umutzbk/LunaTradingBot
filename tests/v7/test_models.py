import numpy as np
import pandas as pd
import pytest

from luna.v7 import models
from luna.v7.constants import FROZEN
from luna.v7.models import ALLOWED_FAMILIES, Config, fit, refit, search_space


def _xy(n=400, seed=0, binary=False):
    rng = np.random.default_rng(seed)
    X = pd.DataFrame(rng.normal(size=(n, 4)), columns=list("abcd"))
    z = 0.8 * X["a"] - 0.5 * X["b"]
    y = (z + rng.normal(scale=0.5, size=n) > 0).astype(float) if binary else z + rng.normal(scale=0.3, size=n)
    return X, y.to_numpy()


SMALL_LGB = {"num_leaves": 4, "min_data_in_leaf": 5}


def test_search_space_42():
    tot = {"en": 0, "lgb_reg": 0, "logit_en": 0, "lgb_clf": 0}
    for h in FROZEN.horizons_h:
        sp = search_space(h)
        assert len(sp) == 14
        for c in sp:
            tot[c.family] += 1
    assert tot == {"en": 18, "lgb_reg": 18, "logit_en": 3, "lgb_clf": 3}
    assert sum(t for k, t in tot.items() if k in ("en", "lgb_reg")) == 36
    assert tot["logit_en"] + tot["lgb_clf"] == 6
    sp = search_space(24)
    en = [(c.params["alpha"], c.params["l1_ratio"]) for c in sp if c.family == "en"]
    assert en == [(a, r) for a in (1e-3, 1e-2, 1e-1) for r in (0.2, 0.8)]
    lg = [(c.params["num_leaves"], c.params["min_data_in_leaf"]) for c in sp if c.family == "lgb_reg"]
    assert lg == [(n, m) for n in (7, 15, 31) for m in (500, 2000)]
    assert [c.params for c in sp if c.family == "logit_en"] == [{"alpha": 1e-2, "l1_ratio": 0.5}]
    assert [c.params for c in sp if c.family == "lgb_clf"] == [{"num_leaves": 15, "min_data_in_leaf": 2000}]
    assert {c.seed for c in sp} == {FROZEN.search.seed}
    with pytest.raises(ValueError):
        search_space(48)


def test_lgb_fixed_params():
    for fam, obj, metric in (("lgb_reg", "regression", "l2"), ("lgb_clf", "binary", "binary_logloss")):
        p = models._lgb_params(Config(fam, SMALL_LGB))
        assert p["objective"] == obj and p["metric"] == metric
        assert (p["learning_rate"], p["lambda_l2"], p["feature_fraction"]) == (0.03, 5, 0.8)
        assert (p["bagging_fraction"], p["bagging_freq"]) == (0.8, 1)
        assert p["deterministic"] is True and p["num_threads"] == 1
        assert p["seed"] == p["bagging_seed"] == p["feature_fraction_seed"] == FROZEN.search.seed
    assert FROZEN.search.lgb_fixed["max_trees"] == 1500 and FROZEN.search.lgb_fixed["early_stopping"] == 100
    X, y = _xy()
    f = fit(Config("lgb_reg", SMALL_LGB), X[:300], y[:300], X_sel=X[300:], y_sel=y[300:])
    assert 1 <= f.best_iteration <= 1500


@pytest.mark.parametrize("fam", ["xgboost", "catboost", "mlp", ""])
def test_disallowed_family_raises(fam):
    assert fam not in ALLOWED_FAMILIES
    with pytest.raises(ValueError, match="not allowed"):
        Config(fam, {"alpha": 1.0, "l1_ratio": 0.5})


def test_missing_params_raise():
    with pytest.raises(ValueError, match="missing"):
        Config("en", {"alpha": 0.1})


@pytest.mark.parametrize(
    "cfg,binary",
    [
        (Config("en", {"alpha": 0.01, "l1_ratio": 0.5}), False),
        (Config("logit_en", {"alpha": 0.01, "l1_ratio": 0.5}), True),
        (Config("lgb_reg", SMALL_LGB), False),
        (Config("lgb_clf", SMALL_LGB), True),
    ],
)
def test_deterministic_fit_on_synthetic(cfg, binary):
    X, y = _xy(binary=binary)
    kw = {"X_sel": X[300:], "y_sel": y[300:]}
    p1 = fit(cfg, X[:300], y[:300], **kw).predict(X)
    p2 = fit(cfg, X[:300], y[:300], **kw).predict(X)
    assert np.array_equal(p1, p2)
    assert np.corrcoef(p1, y)[0, 1] > 0.3  # learns the signal (also guards the alpha / C mapping)
    if binary:
        assert ((p1 > 0) & (p1 < 1)).all()
    r = refit(cfg, getattr(fit(cfg, X[:300], y[:300], **kw), "best_iteration", None), X, y)
    assert np.array_equal(r.predict(X), refit(cfg, r.best_iteration, X, y).predict(X))


def test_logit_C_mapping_matches_elasticnet_scaling():
    X, y = _xy(binary=True)
    n = len(y)
    f = refit(Config("logit_en", {"alpha": 0.05, "l1_ratio": 0.5}), None, X, y)
    assert f.model.C == pytest.approx(1.0 / (0.05 * n))
    assert f.model.l1_ratio == 0.5 and f.model.solver == "saga"
    e = refit(Config("en", {"alpha": 0.05, "l1_ratio": 0.3}), None, X, y)
    assert (e.model.alpha, e.model.l1_ratio) == (0.05, 0.3)


def test_early_stopping_uses_sel_only():
    X, y = _xy(n=600)
    cfg = Config("lgb_reg", SMALL_LGB)
    good = fit(cfg, X[:400], y[:400], X_sel=X[400:], y_sel=y[400:])
    rng = np.random.default_rng(5)
    noise = rng.permutation(y[400:])  # sel target unrelated to X: loss stops improving almost at once
    bad = fit(cfg, X[:400], y[:400], X_sel=X[400:], y_sel=noise)
    assert bad.best_iteration < good.best_iteration
    # the training target is never the stopping criterion: same train data, different sel -> different counts,
    # and a sel-less LightGBM fit is refused
    with pytest.raises(ValueError, match="X_sel"):
        fit(cfg, X[:400], y[:400])
    # refit uses exactly best_iter trees, no early stopping
    r = refit(cfg, good.best_iteration, X, y)
    assert r.model.num_trees() == good.best_iteration and r.best_iteration == good.best_iteration
    with pytest.raises(ValueError):
        refit(cfg, None, X, y)


def test_nonfinite_target_refused():
    X, y = _xy()
    y = y.copy()
    y[3] = np.nan
    with pytest.raises(ValueError):
        fit(Config("en", {"alpha": 0.1, "l1_ratio": 0.5}), X, y)
