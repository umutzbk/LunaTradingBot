"""§15 forward refit (DK-105, ruling R39; AMB-43): no re-selection, the pinned final model's configuration,
T_fit / T_cal with the S-10.3 purge at the forward block boundaries, gated by the verified opening (K9).
Synthetic samples only; the opening is the synthetic one of ``auth_fixtures`` (``guard._roots`` seam)."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from luna.v7 import cv, forward, guard, k9, models
from luna.v7.features import FEATURES
from luna.v7.forward_model import (
    FORWARD_BLOCKS,
    FORWARD_FOLD,
    T_CAL,
    T_FIT,
    ForwardScoreModel,
    forward_model,
    forward_model_hash,
    forward_rows,
)
from luna.v7.timeutil import MIN, D, H, to_ns
from luna.v7.windows import D0, W_HOLD
from tests.v7 import auth_fixtures as af
from tests.v7 import leak_fixtures as lf

H_STAR = 72  # the synthetic opening's ML* = (A2, 72)
PURGE = 24 * H


def _samples(step_days: int = 10, seed: int = 3) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    days = np.arange(D0 // D, T_CAL[1] // D, step_days) * D
    ts = np.repeat(days + 5 * MIN, 4)
    n = len(ts)
    df = pd.DataFrame(rng.normal(size=(n, len(FEATURES))), columns=list(FEATURES))
    df["side"] = np.tile([1.0, -1.0], n // 2)
    df["decision_ts"] = ts
    df["symbol"] = np.tile(["AAA", "AAA", "BBB", "BBB"], n // 4)
    df["h"] = H_STAR
    df["label_known_at"] = ts + H_STAR * H + MIN
    df["y_reg"] = 3 * df["f1_fl1"] + rng.normal(scale=3, size=n) + 1
    df["y_pos"] = (df["y_reg"] > 0).astype(float)
    df = df[df["label_known_at"] < W_HOLD[1]]  # a label crossing the W_HOLD end does not exist (M6)
    return df.reset_index(drop=True)


@pytest.fixture(scope="module")
def opened(tmp_path_factory):
    return af.opening_once(tmp_path_factory.mktemp("fwd_refit"))


@pytest.fixture
def o(opened, monkeypatch):
    return af.use_opening(monkeypatch, opened)


def final():
    return lf.final(H_STAR, "en")


def test_blocks_are_the_spec_blocks():
    assert FORWARD_BLOCKS == forward.FORWARD_BLOCKS
    assert T_FIT == (D0, to_ns("2026-01-01")) and T_CAL == (to_ns("2026-01-01"), to_ns("2026-07-01"))
    assert FORWARD_FOLD.tr[0] == D0 and FORWARD_FOLD.sel[1] == T_FIT[1] and FORWARD_FOLD.cal == T_CAL


def test_refused_without_authorization_k9_logged(tmp_path, o):
    with k9.using(k9.K9Log(tmp_path / "k9")) as log:
        with pytest.raises(guard.HoldoutBlocked):
            forward_model(_samples(), final(), auth=None)
        with pytest.raises(guard.HoldoutBlocked):
            forward_model(_samples(), final(), auth=object())  # a forged / foreign object
        recs = log.records()
    assert len(recs) == 2 and all(r["request"]["kind"] == "forward_refit" for r in recs)
    assert all(r["rule"] == "S-1.7 W_HOLD seal (DEC-19 opening required)" for r in recs)


def test_refused_after_the_verdict_marker(o):
    """After the W_HOLD verdict marker nothing opens: the forward refit is made inside the opening
    (AMB-43 (b))."""
    forward_model(_samples(), final(), auth=o.auth)  # verified and cached while the opening is live
    paths = [d / guard.VERDICT for d in guard._dirs()]
    try:
        for p in paths:
            p.write_text('{"verdict_sha256": "%s"}' % ("0" * 64), encoding="utf-8")
        with pytest.raises(guard.HoldoutBlocked, match="already produced"):
            forward_model(_samples(), final(), auth=o.auth)
    finally:
        for p in paths:
            p.unlink(missing_ok=True)


def test_m6_rows_refused_before_r_v6(tmp_path, o):
    df = _samples()
    late = df.iloc[[-1]].copy()
    late["decision_ts"] = W_HOLD[1] - 2 * H  # its label resolves inside M6 = [2026-07-01, R_V6)
    late["label_known_at"] = W_HOLD[1] + H
    with k9.using(k9.K9Log(tmp_path / "k9")) as log:
        with pytest.raises(guard.EmbargoBlocked):
            forward_model(pd.concat([df, late], ignore_index=True), final(), auth=o.auth)
        assert log.records()[-1]["request"]["kind"] == "data_span"


def test_no_reselection_config_identical(o, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("re-selection / early stopping is forbidden in the forward refit")

    monkeypatch.setattr(cv, "select_hparams", boom)
    monkeypatch.setattr(models, "fit", boom)
    fin = final()
    fm = forward_model(_samples(), fin, auth=o.auth)
    assert fm.fold == FORWARD_FOLD and fm.horizon == H_STAR
    assert fm.selection == {"en": fin.selection["en"]}  # the frozen selection record, carried
    assert fm.reg["en"].cfg == fin.reg["en"].cfg == fin.selection["en"].cfg
    assert set(fm.clf) == set(fin.clf)
    for fam in fin.clf:
        assert fm.clf[fam].fitted.cfg == fin.clf[fam].fitted.cfg
        assert fm.clf[fam].fitted.best_iteration == fin.clf[fam].fitted.best_iteration  # frozen tree count
    assert forward_model_hash(fm) != forward_model_hash(fin)  # refitted, not the same model
    p = ForwardScoreModel(fm, "A2")
    with pytest.raises(ValueError):
        p.fold_index(T_CAL[1] - 1)
    assert p.fold_index(to_ns("2026-10-16")) == 0


def test_other_final_model_refused(o):
    with pytest.raises(ValueError, match="pinned"):
        forward_model(_samples(), lf.final(H_STAR, "en", seed=1), auth=o.auth)


def test_blocks_and_purge_exact(o, monkeypatch):
    df = _samples()
    fit_edge, cal_edge = T_FIT[1] - PURGE, T_CAL[1] - PURGE
    extra = pd.DataFrame([df.iloc[0]] * 6).reset_index(drop=True)
    extra["decision_ts"] = [T_FIT[1] - 4 * D, T_FIT[1] - 3 * D, T_CAL[1] - 4 * D, T_CAL[1] - 3 * D,
                            to_ns("2025-07-01") - 2 * D, T_FIT[1] - MIN]
    extra["label_known_at"] = [fit_edge, fit_edge + 1, cal_edge, cal_edge + 1,
                               to_ns("2025-07-01") + D, fit_edge]
    extra["symbol"] = ["E0", "E1", "E2", "E3", "E4", "E5"]
    s = cv.canonical(pd.concat([df, extra], ignore_index=True), H_STAR)
    idx = forward_rows(s)
    sym = s["symbol"].to_numpy()
    fit_syms, cal_syms = set(sym[idx["fit"]]), set(sym[idx["cal"]])
    # E4: its label crosses 2025-07-01 (a final-model block edge, not a forward one): kept; E5: last minute
    assert {"E0", "E4", "E5"} <= fit_syms and "E1" not in fit_syms
    assert "E2" in cal_syms and "E3" not in cal_syms
    ts, lka = s["decision_ts"].to_numpy(), s["label_known_at"].to_numpy()
    exp_fit = np.flatnonzero((ts >= D0) & (ts < T_FIT[1]) & (lka <= fit_edge))
    exp_cal = np.flatnonzero((ts >= T_CAL[0]) & (ts < T_CAL[1]) & (lka <= cal_edge))
    assert np.array_equal(idx["fit"], exp_fit) and np.array_equal(idx["cal"], exp_cal)
    seen: list[set] = []
    real = models.refit

    def spy(cfg, best_iter, X_fit, y_fit):  # noqa: N803
        seen.append(set(X_fit.index))
        return real(cfg, best_iter, X_fit, y_fit)

    monkeypatch.setattr(models, "refit", spy)
    cal_seen: list[int] = []
    real_lin = cv.LinearCalibrator.fit

    def spy_lin(self, yhat, y, dates):
        cal_seen.append(len(y))
        return real_lin(self, yhat, y, dates)

    monkeypatch.setattr(cv.LinearCalibrator, "fit", spy_lin)
    forward_model(pd.concat([df, extra], ignore_index=True), final(), auth=o.auth)
    assert seen and all(x == set(exp_fit) for x in seen)  # every refit (regressor + classifiers) on T_fit
    assert cal_seen == [len(exp_cal)]
