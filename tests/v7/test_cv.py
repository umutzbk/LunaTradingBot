import functools

import numpy as np
import pandas as pd
import pytest

from luna.v7 import cv, models
from luna.v7.constants import FROZEN
from luna.v7.cv import (
    IsotonicCalibrator,
    LinearCalibrator,
    admissible,
    date_ic,
    date_ic_se,
    decision_dates,
    final_blocks,
    run_fold,
    select_hparams,
    split,
)
from luna.v7.features import FEATURES
from luna.v7.models import Config
from luna.v7.timeutil import MIN, D, H, to_ns
from luna.v7.windows import D0, W_HOLD, outer_folds

FOLD = outer_folds(D0)[0]
SMALL = {"num_leaves": 4, "min_data_in_leaf": 20}
CONFIGS = [
    Config("en", {"alpha": 1e-2, "l1_ratio": 0.2}),
    Config("en", {"alpha": 1e-1, "l1_ratio": 0.8}),
    Config("lgb_reg", SMALL),
    Config("logit_en", {"alpha": 1e-2, "l1_ratio": 0.5}),
    Config("lgb_clf", SMALL),
]


@functools.lru_cache(maxsize=4)
def _samples(lo: int, hi: int, step_days: int = 5, h: int = 24, seed: int = 0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    days = np.arange(lo // D, hi // D, step_days) * D
    ts = np.repeat(days + 5 * MIN, 4)  # 4 rows per decision (2 symbols x 2 sides)
    n = len(ts)
    df = pd.DataFrame(rng.normal(size=(n, len(FEATURES))), columns=list(FEATURES))
    df["side"] = np.tile([1.0, -1.0], n // 2)
    df["decision_ts"] = ts
    df["symbol"] = np.tile(["AAA", "AAA", "BBB", "BBB"], n // 4)
    df["h"] = h
    df["label_known_at"] = ts + h * H + MIN
    z = df["f1_fl1"] + 0.5 * df["f9_rvol_24h"]
    df["y_reg"] = 10 * z + rng.normal(scale=10, size=n) + 3
    df["y_pos"] = (df["y_reg"] > 0).astype(float)
    return df


def _full() -> pd.DataFrame:
    return _samples(D0, FOLD.test_hi).copy()


def test_purge_24h():
    T = FOLD.sel[0]
    assert admissible(T - 24 * H, T) is True
    assert admissible(T - 24 * H + 1, T) is False
    assert not admissible(np.array([T - 24 * H, T - 24 * H + 1]), T)[1]
    df = _full()
    ts = df["decision_ts"].to_numpy()
    # two tr-block rows: one whose label is known exactly 24 h before sel start, one 1 ns later
    i, j = np.flatnonzero(ts < FOLD.tr[1])[-2:]
    df.loc[i, "label_known_at"] = FOLD.sel[0] - 24 * H
    df.loc[j, "label_known_at"] = FOLD.sel[0] - 24 * H + 1
    idx = split(df, FOLD)
    assert i in idx["tr"] and j not in idx["tr"]
    # sel -> cal and cal -> test boundaries
    s = np.flatnonzero((ts >= FOLD.sel[0]) & (ts < FOLD.sel[1]))[-1]
    c = np.flatnonzero((ts >= FOLD.cal[0]) & (ts < FOLD.cal[1]))[-1]
    df.loc[s, "label_known_at"] = FOLD.cal[0] - 24 * H + 1
    df.loc[c, "label_known_at"] = FOLD.test_lo - 24 * H
    idx = split(df, FOLD)
    assert s not in idx["sel"] and c in idx["cal"]
    df.loc[c, "label_known_at"] = FOLD.test_lo - 24 * H + 1
    assert c not in split(df, FOLD)["cal"]


@pytest.mark.parametrize("fold", [FOLD, outer_folds(D0)[3], final_blocks()])
def test_blocks_never_overlap(fold):
    df = _samples(D0, W_HOLD[0] + 100 * D, step_days=7)
    idx = split(df, fold)
    ts = df["decision_ts"].to_numpy()
    seen = set()
    for k in ("tr", "sel", "cal", "test"):
        assert not (seen & set(idx[k]))
        seen |= set(idx[k])
    tr_, sel_, cal_, te_ = (ts[idx[k]] for k in ("tr", "sel", "cal", "test"))
    assert tr_.max() < fold.sel[0] <= sel_.min() and sel_.max() < fold.cal[0] <= cal_.min()
    assert cal_.max() < fold.test_lo <= te_.min() and te_.max() < fold.test_hi
    # every kept row is purged against the next block
    lka = df["label_known_at"].to_numpy()
    assert (lka[idx["tr"]] <= fold.sel[0] - 24 * H).all() and (lka[idx["cal"]] <= fold.test_lo - 24 * H).all()
    assert fold.sel[1] == fold.cal[0] and fold.tr[1] == fold.sel[0]  # contiguous blocks from windows


def test_cal_never_used_for_fit_or_selection(monkeypatch):
    df = _full()
    idx = split(df, FOLD)
    cal_rows = set(df.index[idx["cal"]])
    test_rows = set(df.index[idx["test"]])
    calls = []
    real_fit, real_refit = models.fit, models.refit

    def spy_fit(cfg, X, y, *, X_sel=None, y_sel=None):  # noqa: N803
        calls.append(("fit", cfg.family, set(X.index), set(X_sel.index) if X_sel is not None else None))
        return real_fit(cfg, X, y, X_sel=X_sel, y_sel=y_sel)

    def spy_refit(cfg, best_iter, X_fit, y_fit):  # noqa: N803
        calls.append(("refit", cfg.family, set(X_fit.index), None))
        return real_refit(cfg, best_iter, X_fit, y_fit)

    monkeypatch.setattr(models, "fit", spy_fit)
    monkeypatch.setattr(models, "refit", spy_refit)
    m = run_fold(FOLD, df, 24, configs=CONFIGS)
    tr, sel = set(df.index[idx["tr"]]), set(df.index[idx["sel"]])
    assert calls
    for kind, _fam, xs, xsel in calls:
        assert not (xs & (cal_rows | test_rows))
        if kind == "fit":
            assert xs == tr and xsel == sel  # candidates fit on T_tr, stop / score on T_sel only
        else:
            assert xs == tr | sel  # refit on T_fit
    assert {k for k, *_ in calls} == {"fit", "refit"}
    assert m.test_pred is not None and set(m.test_pred.index) == test_rows

    # corrupting the T_cal and test labels cannot change selection or the base model, only the calibrators
    d2 = df.copy()
    d2.loc[d2.index[idx["cal"]], ["y_reg", "y_pos"]] = [500.0, 1.0]
    d2.loc[d2.index[idx["test"]], "y_reg"] = -999.0
    monkeypatch.setattr(models, "fit", real_fit)
    monkeypatch.setattr(models, "refit", real_refit)
    m2 = run_fold(FOLD, d2, 24, configs=CONFIGS)
    assert m2.selection.cfg == m.selection.cfg and m2.selection.scores == m.selection.scores
    assert np.array_equal(m2.test_pred["yhat"], m.test_pred["yhat"])
    assert np.array_equal(m2.test_pred["p_lgb_clf"], m.test_pred["p_lgb_clf"])
    assert m2.lin_cal.a != m.lin_cal.a


def test_calibrators_fit_on_cal_from_refit_model():
    df = _full()
    idx = split(df, FOLD)
    m = run_fold(FOLD, df, 24, configs=CONFIGS)
    cal = df.iloc[idx["cal"]]
    yhat = m.predict(cal)["yhat"].to_numpy()
    ref = LinearCalibrator().fit(yhat, cal["y_reg"], decision_dates(cal["decision_ts"]))
    assert (m.lin_cal.a, m.lin_cal.b) == (ref.a, ref.b)
    assert set(m.test_pred.columns) >= {"yhat", "yhat_cal", "p_logit_en_cal", "p_lgb_clf_cal"}
    for c in ("p_logit_en_cal", "p_lgb_clf_cal"):
        assert m.test_pred[c].between(0.001, 0.999).all()
    assert m.selection.cfg.is_regressor and m.selection.ic > 0  # signal exists in the fixture


def test_selection_uses_date_ic_not_pnl():
    df = _full()
    idx = split(df, FOLD)
    sel = select_hparams(CONFIGS[:3], df, idx, FOLD, 24)
    assert max(ic for _, ic in sel.scores) == sel.ic
    assert dict((id(c), ic) for c, ic in sel.scores)[id(sel.cfg)] == sel.ic
    with pytest.raises(ValueError, match="classifiers"):
        select_hparams(CONFIGS, df, idx, FOLD, 24)
    if sel.cfg.family == "lgb_reg":
        assert sel.best_iter and sel.best_iter >= 1


def test_final_model_blocks_and_no_holdout():
    fb = final_blocks()
    df = _samples(D0, fb.test_hi, step_days=9)
    m = cv.final_model(df, 24, Config("en", {"alpha": 1e-2, "l1_ratio": 0.5}), classifiers=CONFIGS[3:])
    assert m.fold == fb and m.test_pred is None and m.test_idx is None
    # the holdout rows can be corrupted without changing the frozen model
    ts = df["decision_ts"].to_numpy()
    d2 = df.copy()
    d2.loc[ts >= W_HOLD[0], ["y_reg", "y_pos"]] = [1e6, 1.0]
    m2 = cv.final_model(d2, 24, Config("en", {"alpha": 1e-2, "l1_ratio": 0.5}), classifiers=CONFIGS[3:])
    probe = df.iloc[:50]
    assert np.array_equal(m.predict(probe)["yhat_cal"], m2.predict(probe)["yhat_cal"])
    with pytest.raises(ValueError):
        cv.final_model(df, 24, CONFIGS[3])


def test_linear_calibrator_b_clip_and_date_weights():
    # date A: 2 rows, date B: 6 rows; equal date weights != pooled OLS
    x = np.array([0, 1, 0, 1, 2, 3, 4, 5.0])
    y = np.array([0, 2, 10, 10, 10, 10, 10, 10.0])
    d = np.array([1, 1, 2, 2, 2, 2, 2, 2])
    c = LinearCalibrator().fit(x, y, d)
    w = np.where(d == 1, 1 / 4, 1 / 12)
    mx, my = w @ x, w @ y
    b = (w @ ((x - mx) * (y - my))) / (w @ (x - mx) ** 2)
    assert c.b == pytest.approx(b) and c.a == pytest.approx(my - b * mx)
    assert abs(np.polyfit(x, y, 1)[0] - b) > 0.01  # discriminates the weighting
    # negative slope -> clipped to 0, a = weighted mean y, the b = 0 rule
    neg = LinearCalibrator().fit(x, -x, d)
    assert neg.b == 0.0 and neg.b_is_zero and neg.a == pytest.approx(-(w @ x))
    assert not neg.zero_slope_entry_ok
    assert LinearCalibrator().fit(x, x + 1.0, d).b > 0
    pos = LinearCalibrator().fit(np.zeros(4), np.array([1.0, 2, 3, 4]), np.array([1, 1, 2, 2]))
    assert pos.b == 0.0 and pos.a == 2.5 and pos.zero_slope_entry_ok
    assert np.array_equal(neg.predict(x), np.full(8, neg.a))
    with pytest.raises(ValueError):
        LinearCalibrator().fit([np.nan, 1.0], [1.0, 2.0], [1, 1])


def test_isotonic_clip():
    p = np.linspace(0.05, 0.95, 40)
    y = (p > 0.5).astype(float)  # perfectly separable -> raw isotonic hits 0 and 1
    d = np.repeat(np.arange(4), 10)
    iso = IsotonicCalibrator().fit(p, y, d)
    out = iso.predict(np.array([0.0, 0.2, 0.5, 0.8, 1.0, 5.0, -3.0]))
    assert out.min() == 0.001 and out.max() == 0.999
    assert np.all(np.diff(iso.predict(np.linspace(0, 1, 50))) >= 0)
    # date-equal weights: a date with many rows must not dominate
    p2 = np.array([0.4] * 9 + [0.4])
    y2 = np.array([0.0] * 9 + [1.0])
    d2 = np.array([1] * 9 + [2])
    assert IsotonicCalibrator().fit(p2, y2, d2).predict([0.4])[0] == pytest.approx(0.5)
    with pytest.raises(RuntimeError):
        IsotonicCalibrator().predict([0.5])


def test_date_ic_equal_weights():
    pred = np.array([1, 2, 3, 1, 2, 3, 4, 5.0])
    y = np.array([1, 2, 3, 5, 4, 3, 2, 1.0])
    dates = np.array([0, 0, 0, 1, 1, 1, 1, 1])
    assert date_ic(pred, y, dates) == pytest.approx((1.0 + -1.0) / 2)
    # ties use average ranks; a date with a constant prediction is skipped (not counted as 0)
    pred2 = np.array([1, 1, 1, 1, 2, 3.0])
    y2 = np.array([1, 2, 3, 1, 2, 3.0])
    assert date_ic(pred2, y2, np.array([0, 0, 0, 1, 1, 1])) == pytest.approx(1.0)
    assert np.isnan(date_ic([1.0, 1.0], [1.0, 2.0], [0, 0]))
    with pytest.raises(ValueError):
        date_ic([np.nan, 1.0], [1.0, 2.0], [0, 0])


def test_permutation_canary_ic_near_zero():
    rng = np.random.default_rng(11)
    n_dates, per = 150, 12
    dates = np.repeat(np.arange(n_dates), per)
    signal = rng.normal(size=n_dates * per)
    y = signal + rng.normal(size=n_dates * per)
    true_ic = date_ic(signal, y, dates)
    assert true_ic > 0.4  # real signal is visible
    y_perm = y.copy()
    for k in range(n_dates):  # permute labels within decision dates (L9a)
        sl = slice(k * per, (k + 1) * per)
        y_perm[sl] = rng.permutation(y[sl])
    ic, se = date_ic(signal, y_perm, dates), date_ic_se(signal, y_perm, dates)
    assert se > 0 and abs(ic) <= 2 * se


def test_validate_rejects_bad_samples():
    df = _full()
    with pytest.raises(ValueError, match="lack"):
        split(df.drop(columns=["y_pos"]), FOLD)
    bad = df.copy()
    bad.loc[0, "y_reg"] = np.nan
    with pytest.raises(ValueError, match="non-finite"):
        split(bad, FOLD)
    with pytest.raises(ValueError, match="horizon"):
        run_fold(FOLD, df, 72, configs=CONFIGS)
    assert FROZEN.cv.purge_ns == 24 * H and to_ns("2021-01-01") == D0
