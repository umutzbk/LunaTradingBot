"""§15 forward refit (DK-105, rulings R39 / R54; AMB-43): no re-selection, the pinned final model's
configuration, T_fit / T_cal with the S-10.3 purge at the forward block boundaries, gated by the verified
opening (K9), samples bound to the canonical panels, a write-once forward-model record next to the opening
markers, and the frozen model loaded only through ``load_forward_model``. Synthetic samples only; the opening
is the synthetic one of ``auth_fixtures`` (``guard._roots`` seam)."""

from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest

from luna.v7 import cv, forward, guard, k9, models, runner
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
    load_forward_model,
    loaded_hash,
)
from luna.v7.runner import LabelPanel
from luna.v7.timeutil import MIN, D, H, to_ns
from luna.v7.windows import D0, W_HOLD
from tests.v7 import auth_fixtures as af
from tests.v7 import leak_fixtures as lf

H_STAR = 72  # the synthetic opening's ML* = (A2, 72)
PURGE = 24 * H


def _hold_samples(step_days: int = 10, seed: int = 3, extra: pd.DataFrame | None = None) -> pd.DataFrame:
    """W_HOLD label-panel rows (synthetic): decisions in W_HOLD, labels resolving before the W_HOLD end."""
    rng = np.random.default_rng(seed)
    days = np.arange(W_HOLD[0] // D, W_HOLD[1] // D, step_days) * D
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
    df = df[df["label_known_at"] < W_HOLD[1]]
    if extra is not None:
        df = pd.concat([df, extra], ignore_index=True)
    return df.reset_index(drop=True)


def _panel(samples: pd.DataFrame) -> LabelPanel:
    return LabelPanel(H_STAR, samples, pd.DataFrame(), len(samples), 0, 0, 0.0, ())


def issued(o, samples=None) -> LabelPanel:
    """A W_HOLD panel issued under the opening (``runner._issue_holdout_panel``: TEST HOOK for
    ``label_panel``, whose real run needs a full research universe)."""
    p = _panel(_hold_samples() if samples is None else samples)
    runner._issue_holdout_panel(p, o.auth)
    return p


def dev():
    return lf.panel(H_STAR, 0, None, lf.FOLD.test_hi)  # the W_DEV panel the pinned LeakageReport hashed


def final():
    return lf.final(H_STAR, "en")


def _files():
    return [d / n for d in guard._dirs() for n in (guard.FORWARD_MODEL, guard.FORWARD_BLOB, guard.VERDICT)]


@pytest.fixture(scope="module")
def opened(tmp_path_factory):
    return af.opening_once(tmp_path_factory.mktemp("fwd_refit"))


@pytest.fixture
def o(opened, monkeypatch):
    af.use_opening(monkeypatch, opened)
    yield opened
    for p in _files():  # the record is write-once per opening: every test starts without one
        p.unlink(missing_ok=True)


def _rewrite(mutate):
    for d in guard._dirs():
        p = d / guard.FORWARD_MODEL
        rec = json.loads(p.read_text(encoding="utf-8"))
        mutate(rec)
        p.write_text(json.dumps(rec, indent=1), encoding="utf-8")


def _attempts():
    p = guard._dirs()[0] / guard.FORWARD_LOG
    return [json.loads(x)["result"] for x in p.read_text(encoding="utf-8").splitlines()]


def test_blocks_are_the_spec_blocks():
    assert FORWARD_BLOCKS == forward.FORWARD_BLOCKS
    assert T_FIT == (D0, to_ns("2026-01-01")) and T_CAL == (to_ns("2026-01-01"), to_ns("2026-07-01"))
    assert FORWARD_FOLD.tr[0] == D0 and FORWARD_FOLD.sel[1] == T_FIT[1] and FORWARD_FOLD.cal == T_CAL


def test_refused_without_authorization_k9_logged(tmp_path, o):
    p = issued(o)
    with k9.using(k9.K9Log(tmp_path / "k9")) as log:
        with pytest.raises(guard.HoldoutBlocked):
            forward_model(dev(), [p], final(), auth=None)
        with pytest.raises(guard.HoldoutBlocked):
            forward_model(dev(), [p], final(), auth=object())  # a forged / foreign object
        recs = log.records()
    assert len(recs) == 2 and all(r["request"]["kind"] == "forward_refit" for r in recs)
    assert all(r["rule"] == "S-1.7 W_HOLD seal (DEC-19 opening required)" for r in recs)


def test_identical_refit_is_idempotent_and_recorded(o):
    p = issued(o)
    fm = forward_model(dev(), [p], final(), auth=o.auth)
    rec = guard.forward_model_record()
    assert rec["forward_model_hash"] == forward_model_hash(fm)
    assert rec["opening_id"] == o.auth.opening_id and rec["code_commit"] == af.HEAD_COMMIT
    assert rec["final_model_hash"] == lf.passed_report("A2", H_STAR).final_model_hash
    assert rec["config"]["regressor"]["family"] == "en"
    fm2 = forward_model(dev(), [p], final(), auth=o.auth)
    assert forward_model_hash(fm2) == forward_model_hash(fm)
    assert guard.forward_model_record() == rec  # the existing record (same created_utc)
    assert _attempts()[-2:] == ["pinned", "identical"]


def test_second_different_refit_refused(tmp_path, o):
    forward_model(dev(), [issued(o)], final(), auth=o.auth)
    other = issued(o, _hold_samples(seed=4))
    with k9.using(k9.K9Log(tmp_path / "k9")) as log:
        with pytest.raises(guard.HoldoutBlocked, match="already pinned"):
            forward_model(dev(), [other], final(), auth=o.auth)
        assert log.records()[-1]["request"]["kind"] == "forward_refit"
    assert _attempts()[-1] == "refused"


def test_subset_or_altered_samples_refused(o):
    p = issued(o)
    d = dev()
    with pytest.raises(guard.HoldoutBlocked, match="W_DEV"):
        forward_model(d.iloc[:-4], [p], final(), auth=o.auth)  # a row subset
    alt = d.copy()
    alt.loc[0, "y_reg"] += 1.0
    with pytest.raises(guard.HoldoutBlocked, match="W_DEV"):
        forward_model(alt, [p], final(), auth=o.auth)  # an altered frame
    hs = p.samples.copy()
    hs.loc[0, "y_reg"] += 1.0
    with pytest.raises(guard.HoldoutBlocked, match="not issued"):
        forward_model(d, [_panel(hs)], final(), auth=o.auth)  # an altered W_HOLD panel
    with pytest.raises(guard.HoldoutBlocked, match="not issued"):
        forward_model(d, [_panel(p.samples.iloc[:-4])], final(), auth=o.auth)  # a W_HOLD subset
    assert guard.forward_model_record() is None


def test_holdout_panel_not_issued_under_this_auth_refused(o, monkeypatch):
    with pytest.raises(guard.HoldoutBlocked, match="not issued"):
        forward_model(dev(), [_panel(_hold_samples(seed=5))], final(), auth=o.auth)  # never issued
    p = issued(o, _hold_samples(seed=6))
    for k in [k for k, v in runner._HOLD_PANELS.items() if v == o.auth.opening_id]:
        monkeypatch.setitem(runner._HOLD_PANELS, k, "f" * 64)  # issued under another opening
    with pytest.raises(guard.HoldoutBlocked, match="not issued"):
        forward_model(dev(), [p], final(), auth=o.auth)


def test_record_from_another_opening_refused(o):
    forward_model(dev(), [issued(o)], final(), auth=o.auth)
    assert load_forward_model().fold == FORWARD_FOLD

    def foreign(rec):
        rec["opening_id"] = "e" * 64
        rec["record_sha256"] = guard._sha({k: v for k, v in rec.items() if k != "record_sha256"})

    _rewrite(foreign)
    with pytest.raises(guard.HoldoutBlocked, match="opening_id"):
        load_forward_model()


def test_tampered_record_or_blob_refused(o):
    forward_model(dev(), [issued(o)], final(), auth=o.auth)
    _rewrite(lambda rec: rec.update(samples_sha256="0" * 64))
    with pytest.raises(guard.HoldoutBlocked, match="tampered"):
        load_forward_model()
    for p in _files():
        p.unlink(missing_ok=True)
    forward_model(dev(), [issued(o)], final(), auth=o.auth)
    for d in guard._dirs():
        b = d / guard.FORWARD_BLOB
        b.write_bytes(b.read_bytes() + b"x")
    with pytest.raises(guard.HoldoutBlocked, match="blob"):
        load_forward_model()


def test_after_the_verdict_marker_refit_refused_load_works(o):
    fm = forward_model(dev(), [issued(o)], final(), auth=o.auth)
    for p in [d / guard.VERDICT for d in guard._dirs()]:
        p.write_text('{"verdict_sha256": "%s"}' % ("0" * 64), encoding="utf-8")
    with pytest.raises(guard.HoldoutBlocked, match="already produced"):
        forward_model(dev(), [], final(), auth=o.auth)
    frozen = load_forward_model()  # the forward stage reads the frozen model after the verdict
    assert loaded_hash(frozen) == guard.forward_model_record()["forward_model_hash"] == forward_model_hash(fm)
    with pytest.raises(ValueError, match="load_forward_model"):
        ForwardScoreModel(fm, "A2")  # not loaded through the record
    sm = ForwardScoreModel(frozen, "A2")
    with pytest.raises(ValueError):
        sm.fold_index(T_CAL[1] - 1)
    assert sm.fold_index(to_ns("2026-10-16")) == 0


def test_m6_rows_refused_before_r_v6(tmp_path, o):
    late = _hold_samples().iloc[[-1]].copy()
    late["decision_ts"] = W_HOLD[1] - 2 * H  # its label resolves inside M6 = [2026-07-01, R_V6)
    late["label_known_at"] = W_HOLD[1] + H
    late["symbol"] = "LATE"
    p = issued(o, _hold_samples(extra=late))
    with k9.using(k9.K9Log(tmp_path / "k9")) as log:
        with pytest.raises(guard.EmbargoBlocked):
            forward_model(dev(), [p], final(), auth=o.auth)
        assert log.records()[-1]["request"]["kind"] == "data_span"
    assert guard.forward_model_record() is None


def test_no_reselection_config_identical(o, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("re-selection / early stopping is forbidden in the forward refit")

    monkeypatch.setattr(cv, "select_hparams", boom)
    monkeypatch.setattr(models, "fit", boom)
    fin = final()
    fm = forward_model(dev(), [issued(o)], fin, auth=o.auth)
    assert fm.fold == FORWARD_FOLD and fm.horizon == H_STAR
    assert fm.selection == {"en": fin.selection["en"]}  # the frozen selection record, carried
    assert fm.reg["en"].cfg == fin.reg["en"].cfg == fin.selection["en"].cfg
    assert set(fm.clf) == set(fin.clf)
    for fam in fin.clf:
        assert fm.clf[fam].fitted.cfg == fin.clf[fam].fitted.cfg
        assert fm.clf[fam].fitted.best_iteration == fin.clf[fam].fitted.best_iteration  # frozen tree count
    assert forward_model_hash(fm) != forward_model_hash(fin)  # refitted, not the same model


def test_other_final_model_refused(o):
    with pytest.raises(ValueError, match="pinned"):
        forward_model(dev(), [issued(o)], lf.final(H_STAR, "en", seed=1), auth=o.auth)


def test_forward_rows_purge_exact():
    df = _hold_samples()
    fit_edge, cal_edge = T_FIT[1] - PURGE, T_CAL[1] - PURGE
    extra = pd.DataFrame([df.iloc[0]] * 6).reset_index(drop=True)
    extra["decision_ts"] = [T_FIT[1] - 4 * D, T_FIT[1] - 3 * D, T_CAL[1] - 4 * D, T_CAL[1] - 3 * D,
                            to_ns("2025-07-01") - 2 * D, T_FIT[1] - MIN]
    extra["label_known_at"] = [fit_edge, fit_edge + 1, cal_edge, cal_edge + 1, to_ns("2025-07-01") + D,
                               fit_edge]
    extra["symbol"] = ["E0", "E1", "E2", "E3", "E4", "E5"]
    s = cv.canonical(pd.concat([df, extra], ignore_index=True), H_STAR)
    idx = forward_rows(s)
    sym = s["symbol"].to_numpy()
    fit_syms, cal_syms = set(sym[idx["fit"]]), set(sym[idx["cal"]])
    # E4: its label crosses 2025-07-01 (a final-model block edge, not a forward one): kept; E5: last minute
    assert {"E0", "E4", "E5"} <= fit_syms and "E1" not in fit_syms
    assert "E2" in cal_syms and "E3" not in cal_syms
    ts, lka = s["decision_ts"].to_numpy(), s["label_known_at"].to_numpy()
    assert np.array_equal(idx["fit"], np.flatnonzero((ts >= D0) & (ts < T_FIT[1]) & (lka <= fit_edge)))
    assert np.array_equal(idx["cal"], np.flatnonzero((ts >= T_CAL[0]) & (ts < T_CAL[1]) & (lka <= cal_edge)))


def test_refit_and_calibrators_on_exact_blocks(o, monkeypatch):
    fit_edge, cal_edge = T_FIT[1] - PURGE, T_CAL[1] - PURGE
    base = _hold_samples()
    extra = pd.DataFrame([base.iloc[0]] * 4).reset_index(drop=True)
    extra["decision_ts"] = [T_FIT[1] - 4 * D, T_FIT[1] - 3 * D, T_CAL[1] - 4 * D, T_CAL[1] - 3 * D]
    extra["label_known_at"] = [fit_edge, fit_edge + 1, cal_edge, cal_edge + 1]
    extra["symbol"] = ["E0", "E1", "E2", "E3"]
    p = issued(o, _hold_samples(extra=extra))
    s = cv.canonical(pd.concat([dev(), p.samples], ignore_index=True), H_STAR)
    idx = forward_rows(s)
    sym = s["symbol"].to_numpy()
    assert "E0" in set(sym[idx["fit"]]) and "E1" not in set(sym[idx["fit"]])
    assert "E2" in set(sym[idx["cal"]]) and "E3" not in set(sym[idx["cal"]])
    seen: list[set] = []
    real = models.refit

    def spy(cfg, best_iter, X_fit, y_fit):  # noqa: N803
        seen.append(set(X_fit.index))
        return real(cfg, best_iter, X_fit, y_fit)

    lin_n: list[int] = []
    iso_n: list[int] = []
    real_lin, real_iso = cv.LinearCalibrator.fit, cv.IsotonicCalibrator.fit

    def spy_lin(self, yhat, y, dates):
        lin_n.append(len(y))
        return real_lin(self, yhat, y, dates)

    def spy_iso(self, ph, ypos, dates):
        iso_n.append(len(ypos))
        return real_iso(self, ph, ypos, dates)

    monkeypatch.setattr(models, "refit", spy)
    monkeypatch.setattr(cv.LinearCalibrator, "fit", spy_lin)
    monkeypatch.setattr(cv.IsotonicCalibrator, "fit", spy_iso)
    forward_model(dev(), [p], final(), auth=o.auth)
    assert seen and all(x == set(idx["fit"]) for x in seen)  # every refit (regressor + classifiers) on T_fit
    assert lin_n == [len(idx["cal"])]
    assert iso_n == [len(idx["cal"])] * len(final().clf)  # one isotonic calibrator per classifier, on T_cal


# --------------------------------------------------------------------------------- lgb_reg (ML* = A3)


@pytest.fixture(scope="module")
def opened_a3(tmp_path_factory):
    return af.opening_once(tmp_path_factory.mktemp("fwd_refit_a3"), dev=af.dev_selection("A3"))


def test_lgb_reg_reuses_best_iter(opened_a3, monkeypatch):
    o = af.use_opening(monkeypatch, opened_a3)
    try:
        fin = lf.final(H_STAR, "lgb_reg")
        assert fin.selection["lgb_reg"].best_iter is not None
        seen: list[tuple] = []
        real = models.refit

        def spy(cfg, best_iter, X_fit, y_fit):  # noqa: N803
            seen.append((cfg.family, best_iter))
            return real(cfg, best_iter, X_fit, y_fit)

        monkeypatch.setattr(models, "refit", spy)
        fm = forward_model(dev(), [issued(o)], fin, auth=o.auth)
        assert ("lgb_reg", fin.selection["lgb_reg"].best_iter) in seen
        assert fm.reg["lgb_reg"].best_iteration == fin.selection["lgb_reg"].best_iter
        assert fm.reg["lgb_reg"].cfg == fin.selection["lgb_reg"].cfg
        rec = guard.forward_model_record()
        assert rec["config"]["regressor"]["best_iteration"] == fin.selection["lgb_reg"].best_iter
    finally:
        for p in _files():
            p.unlink(missing_ok=True)
