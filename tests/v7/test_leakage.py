"""Leakage machinery (spec §11 L1 / L9, §16 K1 / K7, DEC-19, S-13.4; DK-26, DK-27, SC-02; AMB-33).

Synthetic, seeded, small: real ``cv`` / ``models`` fits under a tiny test-only search space (leak_fixtures).
"""

from __future__ import annotations

import dataclasses
import json
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest
from sklearn.linear_model import ElasticNet, LogisticRegression

from luna.v7 import cv, leakage, models
from luna.v7.constants import FROZEN
from luna.v7.models import Config, Fitted
from luna.v7.timeutil import MIN, D, H
from luna.v7.universe import LeakageError
from tests.v7 import leak_fixtures as lf
from tests.v7.run_fixtures import funding_run

LEAK = "f3_accel"


@pytest.fixture(scope="module")
def report():
    return lf.passed_report("A2", 72)


@pytest.fixture(scope="module")
def leaky():
    """Checks on a panel whose label is copied into ``f3_accel`` (ML* = A3 at 24 h; 24 h only: runtime)."""
    return lf.run_checks("A3", 24, leak=LEAK, samples={24: lf.panel(24, leak=LEAK, hi=lf.FOLD.test_hi)},
                         fold_models=lf.fold_models(leak=LEAK, horizons=(24,)), final_model=None)


# ------------------------------------------------------------------------------------------------ L9a


def test_permutation_stays_within_dates_and_is_seeded():
    s = cv.canonical(lf.panel(24, hi=lf.FOLD.test_hi), 24)
    p = leakage.permute_within_dates(s, 24, seed=11)
    assert p[cv.KEY].equals(s[cv.KEY])  # rows (keys, features) stay; only the label record moves
    assert p.drop(columns=list(leakage.LABEL_COLS)).equals(s.drop(columns=list(leakage.LABEL_COLS)))
    d = cv.decision_dates(s["decision_ts"])
    for c in leakage.LABEL_COLS:
        for day in np.unique(d)[:50]:
            m = d == day
            assert sorted(p.loc[m, c]) == sorted(s.loc[m, c])  # a permutation inside the date
    assert not np.array_equal(p["y_reg"], s["y_reg"])
    # the label record moves as a unit: (y_reg, y_pos, label_known_at) of one source row
    src = s.set_index(["y_reg"])
    for _, r in p.head(40).iterrows():
        assert src.loc[r["y_reg"], "label_known_at"] == r["label_known_at"]
        assert src.loc[r["y_reg"], "y_pos"] == r["y_pos"]
    assert leakage.permute_within_dates(s, 24, seed=11).equals(p)
    assert not leakage.permute_within_dates(s, 24, seed=12).equals(p)


def test_l9a_threshold_is_two_date_level_se():
    rng = np.random.default_rng(0)
    dates = np.repeat(np.arange(60) * D + lf.FOLD.test_lo, 8)
    y = rng.normal(size=len(dates))
    noise = rng.normal(size=len(dates))
    ok = leakage.l9a_result("en", 24, noise, y, dates, n_folds=1)
    assert ok.ic == pytest.approx(cv.date_ic(noise, y, dates))
    assert ok.se == pytest.approx(cv.date_ic_se(noise, y, dates))
    assert ok.passed is (abs(ok.ic) <= FROZEN.l9_se_mult * ok.se) and ok.passed and ok.n_dates == 60
    bad = leakage.l9a_result("en", 24, y + 0.3 * noise, y, dates, n_folds=1)  # a leak survives the shuffle
    assert not bad.passed and abs(bad.ic) > 2 * bad.se
    nan = leakage.l9a_result("en", 24, noise[:8], y[:8], dates[:8], n_folds=1)  # one date: SE undefined
    assert not nan.passed  # fail closed


def test_report_passes_on_synthetic_panel(report):
    r = report
    assert leakage.verify_report(r) and r.passed and r.complete and not r.failures and not r.k1
    assert {(x.family, x.horizon) for x in r.l9a} == {(f, h) for f in models.REGRESSORS for h in lf.HORIZONS}
    for x in r.l9a:
        assert x.passed and np.isfinite(x.ic) and np.isfinite(x.se) and abs(x.ic) <= 2 * x.se
        assert x.n_folds == 1 and x.n_dates > 15
    assert r.seed == models.SEED and r.ml_star == ("A2", 72)
    assert r.commit == lf.COMMIT and dict(r.data_manifests) == lf.MANIFESTS and r.dirty is False
    assert len(r.sha256) == 64 and dict(r.panel_sha256s).keys() == set(lf.HORIZONS)
    assert dict(r.fold_model_hashes)[72] == tuple(
        leakage.model_hash(m) for m in lf.fold_models()[72])
    assert r.final_model_hash == leakage.model_hash(lf.final(72, "en"))


def test_l9a_mirrors_the_real_run(report, monkeypatch):
    """The canary runs cv.run_fold on the permuted labels over exactly the real run's outer folds."""
    seen = []
    real = cv.run_fold

    def spy(fold, samples, h):
        seen.append((fold.fold_id, h))
        return real(fold, samples, h)

    monkeypatch.setattr(cv, "run_fold", spy)
    s = lf.panel(24, hi=lf.FOLD.test_hi)
    with lf.tiny_space():
        out = leakage.l9a(s, 24, [lf.FOLD], seed=models.SEED)
    assert seen == [(lf.FOLD.fold_id, 24)]
    assert {x.family for x in out} == set(models.REGRESSORS)
    assert [(x.ic, x.se) for x in out] == [(y.ic, y.se) for y in report.l9a if y.horizon == 24]


# ------------------------------------------------------------------------------------------------ L9b


def test_shifted_label_feature_and_l1_refusal():
    s = cv.canonical(lf.panel(24, hi=lf.FOLD.test_hi), 24)
    f = leakage.shifted_label_feature(s, 24)
    assert f[cv.KEY].equals(s[cv.KEY])
    t0 = s["decision_ts"].min()
    assert f.loc[s["decision_ts"] == t0, "value"].isna().all()  # no earlier decision
    for i in (200, 205, 209, 401):
        row = s.iloc[i]
        first_of_day = row["decision_ts"] % D == 5 * MIN
        gap = lf.STEP_DAYS * D - 16 * H if first_of_day else 8 * H  # the panel's previous decision
        prev = s[(s["decision_ts"] == row["decision_ts"] - gap) & (s["symbol"] == row["symbol"])
                 & (s["side"] == row["side"])]
        assert len(prev) == 1 and f.iloc[i]["value"] == prev["y_reg"].iloc[0]
        assert f.iloc[i]["known_at"] == prev["label_known_at"].iloc[0]
    res = leakage.l9b_refusal(f)
    have = f[f["value"].notna()]
    n_dec = s["decision_ts"].nunique() - 1
    late = (have["known_at"] > have["decision_ts"] - MIN).groupby(have["decision_ts"]).any()
    assert res[0] == n_dec and res[1] == int(late.sum()) and 0 < res[1] < n_dec  # 08:05 / 16:05: refused
    assert "known_at" in res[2]  # the real L1 assertion's message
    # discriminating: the same feature with its label already known by t - 1 min is accepted by L1
    known = f.assign(known_at=f["decision_ts"] - MIN)
    assert leakage.l9b_refusal(known)[1] == 0


def test_l9b_uses_the_real_l1_path(monkeypatch):
    calls = []
    real = leakage.features.assert_pit

    def spy(inputs, t):
        calls.append((tuple(inputs), int(t)))
        return real(inputs, t)

    monkeypatch.setattr(leakage.features, "assert_pit", spy)
    s = cv.canonical(lf.panel(24, hi=lf.FOLD.test_hi), 24)
    leakage.l9b_refusal(leakage.shifted_label_feature(s, 24))
    assert calls and all(names == (leakage.SHIFT_NAME,) for names, _ in calls)


def test_l9b_reported_with_sandbox_ic(report):
    assert {x.horizon for x in report.l9b} == set(lf.HORIZONS)
    for x in report.l9b:
        assert x.refused and 0 < x.n_refused <= x.n_decisions and x.sandbox_column == leakage.SANDBOX_COLUMN
        assert (x.n_refused == x.n_decisions) is (x.horizon == 168)  # 4-day gaps: only 168 h still unknown
        assert {s.family for s in x.sandbox} == set(models.REGRESSORS)
    # the demonstration: without L1 the out-of-fold IC jumps above the real run's (reported, no threshold)
    for x in report.l9b:
        [m] = lf.fold_models()[x.horizon]
        test = cv.canonical(lf.panel(x.horizon, hi=lf.FOLD.test_hi), x.horizon).iloc[m.test_idx]
        for s in x.sandbox:
            dates = cv.decision_dates(test["decision_ts"])
            real = cv.date_ic(m.test_pred[f"yhat_{s.family}"], test["y_reg"], dates)
            assert s.ic > real + 0.1 and s.se > 0 and s.n_dates > 15


# ------------------------------------------------------------------------------------------------ K7


def _en(coef, names, family="en"):
    m = ElasticNet() if family == "en" else LogisticRegression()
    m.coef_ = np.asarray(coef, np.float64) if family == "en" else np.asarray([coef], np.float64)
    m.feature_names_in_ = np.asarray(names, dtype=object)
    params = {"alpha": 0.01, "l1_ratio": 0.5}
    return Fitted(Config(family, params), m)


def test_k7_missing_indicator_is_merged():
    f = _en([0.30, -0.25, 0.45], ["f1_fl1", "f1_fl1__missing", "f9_rvol_24h"])
    shares, total = leakage.feature_shares(f)
    assert total == pytest.approx(1.0)
    assert shares == pytest.approx({"f1_fl1": 0.55, "f9_rvol_24h": 0.45})
    assert leakage.concentration(f) == (pytest.approx(0.55), "f1_fl1", pytest.approx(1.0))
    # without the merge no column would exceed 50 %
    g = _en([0.30, 0.25, 0.45], ["f1_fl1", "f2_autocorr1_30d", "f9_rvol_24h"], family="logit_en")
    assert leakage.concentration(g)[0] == pytest.approx(0.45)
    assert leakage.concentration(_en([0.0, 0.0], ["a", "b"])) == (0.0, None, 0.0)  # an all-zero model


def test_k7_lightgbm_gain_share():
    rng = np.random.default_rng(3)
    X = pd.DataFrame(rng.normal(size=(600, 4)), columns=["a", "b", "c", "d"])  # noqa: N806
    y = 5 * X["a"] + 0.2 * X["b"] + rng.normal(scale=0.1, size=600)
    cfg = Config("lgb_reg", lf.SMALL)
    f = models.fit(cfg, X.iloc[:400], y[:400], X_sel=X.iloc[400:], y_sel=y[400:])
    share, feat, total = leakage.concentration(f)
    g = f.model.feature_importance(importance_type="gain", iteration=f.best_iteration)
    assert feat == "a" and share == pytest.approx(g[0] / g.sum()) and share > FROZEN.k7_max_share
    assert total == pytest.approx(g.sum())


def test_k7_leaky_panel_triggers_on_the_evaluated_models(leaky):
    trig = [k for k in leaky.k7 if k.trigger]
    ev = [k for k in leaky.k7 if k.evaluated]
    assert {k.key for k in ev} == {(24, "fold0", "lgb_reg"), (24, "fold0", "lgb_clf")}
    assert {k.feature for k in trig} == {LEAK}
    assert all(k.max_share > 0.5 for k in trig)
    assert any(k.evaluated for k in trig) and any(not k.evaluated for k in trig)  # others reported
    assert not leaky.passed and leaky.open_triggers and not leaky.k1
    assert any("audit" in s for s in leaky.blocking)


def test_k7_audit_resolution(leaky):
    open_ = leaky.open_triggers
    r = leaky
    for k in open_:
        r = leakage.resolve_k7(r, k.key, "explained", "feature built from the label by a test fixture")
    assert leakage.verify_report(r) and r.sha256 != leaky.sha256 and not r.open_triggers
    assert not any("audit" in b for b in r.blocking)
    assert set(r.blocking) == set(r.missing) | set(r.failures)  # only the skipped 72 h / 168 h canaries left
    assert not leaky.passed  # the original report is unchanged (immutable)
    with pytest.raises(ValueError, match="already"):
        leakage.resolve_k7(r, open_[0].key, "unexplained", "changed my mind")
    u = leakage.resolve_k7(leaky, open_[0].key, "unexplained", "no explanation found")
    assert u.k1 and not u.passed and any("K1" in s for s in u.failures)
    for bad in (("explained", ""), ("explained", "   "), ("maybe", "text")):
        with pytest.raises(ValueError):
            leakage.resolve_k7(leaky, open_[0].key, *bad)

def test_k7_resolution_only_for_triggers(report):
    assert not any(k.trigger for k in report.k7)
    with pytest.raises(ValueError, match="not a K7 trigger"):
        leakage.resolve_k7(report, report.k7[0].key, "explained", "x")
    with pytest.raises(ValueError, match="no K7 result"):
        leakage.resolve_k7(report, (24, "fold9", "en"), "explained", "x")
    with pytest.raises(ValueError, match="run_leakage_checks"):
        leakage.resolve_k7(dataclasses.replace(report, commit="c" * 40), report.k7[0].key, "explained", "x")


# ------------------------------------------------------------------------------------- sealing / forging


def test_forged_report_never_verifies(report):
    content = {f.name: getattr(report, f.name) for f in dataclasses.fields(report)
               if f.name not in ("sha256", "_issuer")}
    with pytest.raises(TypeError):
        leakage.LeakageReport(**content, sha256=report.sha256)
    with pytest.raises(TypeError):
        leakage.LeakageReport(**content, sha256=report.sha256, _issuer=object())
    # a copy with altered content keeps a stale hash
    bad_l9a = tuple(dataclasses.replace(x, ic=0.0, passed=True) for x in report.l9a)
    forged = dataclasses.replace(report, l9a=bad_l9a, commit="c" * 40)
    assert not leakage.verify_report(forged) and not forged.passed
    # even with the recomputed content hash it was never issued by run_leakage_checks
    rehashed = dataclasses.replace(forged, sha256=leakage._content_sha(forged))
    assert not leakage.verify_report(rehashed) and not rehashed.passed
    tampered = dataclasses.replace(report)
    object.__setattr__(tampered, "k7", ())
    assert not leakage.verify_report(tampered) and not tampered.passed
    assert leakage.verify_report(dataclasses.replace(report))  # an identical copy is the same report
    for obj in (None, object(), "x", SimpleNamespace(passed=True, sha256=report.sha256)):
        assert not leakage.verify_report(obj)


def test_skipped_check_is_not_passed():
    """Horizons 72 / 168 and the final model left out: executed checks all pass, the report is not passed."""
    r = lf.run_checks("A2", 24, samples={24: lf.panel(24, hi=lf.FOLD.test_hi)},
                      fold_models=lf.fold_models(horizons=(24,)), final_model=None)
    assert leakage.verify_report(r) and not r.failures and not r.open_triggers
    assert not r.complete and not r.passed and set(r.blocking) == set(r.missing)
    for h in (72, 168):
        assert f"L9b @{h}h not executed" in r.missing
        assert all(f"L9a {f}@{h}h not executed" in r.missing for f in models.REGRESSORS)
    assert any("final model missing" in b for b in r.missing)
    assert all(x.horizon == 24 for x in (*r.l9a, *r.l9b))


def test_inputs_refused():
    with pytest.raises(ValueError, match="clean"):
        lf.run_checks("A2", 72, dirty=True)
    with pytest.raises(ValueError, match="commit"):
        lf.run_checks("A2", 72, commit="abc")
    with pytest.raises(ValueError, match="sha256"):
        lf.run_checks("A2", 72, data_manifests={"x": "nothex"})
    with pytest.raises(ValueError, match="final model"):
        lf.run_checks("A2", 72, final_model=SimpleNamespace(horizon=24))
    with pytest.raises(ValueError, match="ML"):
        lf.run_checks("A1", 72, final_model=None)


# ------------------------------------------------------------------------------------ holdout phase


def _runs():
    return [funding_run([0.001] * 5, arm="A2", h=72), funding_run([0.0] * 5, arm="A0", h=72)]


def test_holdout_checks(report):
    runs = _runs()
    ok = lf.holdout_report(report, runs)
    assert leakage.verify_report(ok) and ok.complete and ok.findings == () and ok.pre_sha256 == report.sha256
    assert leakage.covers(ok, runs) and not leakage.covers(ok, [funding_run([0.002] * 5, arm="A2", h=72)])
    t = int(runs[0].decisions["t"].iloc[0])

    def late(tt):
        return SimpleNamespace(max_known_at=int(tt))  # known at t > t - 1 min

    def raising(tt):
        raise LeakageError(f"decision {tt}: an input known later")

    for rec, text in ((late, "max_known_at"), (raising, "known later")):
        h = lf.holdout_report(report, runs, rec)
        assert leakage.verify_report(h) and h.complete
        assert h.findings and h.findings[0][0] == "L1"
        assert str(t) in h.findings[0][1] and text in h.findings[0][1]
    with pytest.raises(ValueError, match="max_known_at"):
        lf.holdout_report(report, runs, lambda tt: object())


def test_holdout_report_complete_needs_checked_decisions(report):
    """M-3: a holdout check over zero decisions was not executed: the report is not complete."""
    empty = pd.DataFrame({"t": pd.Series([], dtype="int64"), "killed": pd.Series([], dtype=bool)})
    runs = [funding_run([0.001] * 5, arm="A2", h=72, decisions=empty)]
    h = lf.holdout_report(report, runs)
    assert leakage.verify_report(h) and dict(h.checks) == {"L1": 0} and not h.complete
    assert lf.holdout_report(report, _runs()).complete


def test_covers_exactly(report):
    """M-1: ``covers(..., exactly=True)``: the report checked exactly the given runs."""
    runs = _runs()
    h = lf.holdout_report(report, runs)
    assert leakage.covers(h, runs, exactly=True) and leakage.covers(h, runs[:1])
    assert not leakage.covers(h, runs[:1], exactly=True)
    assert not leakage.covers(h, [*runs, funding_run([0.002] * 5, arm="A2", h=72)], exactly=True)


def test_persisted_report_round_trip(report, monkeypatch):
    """Task 24 I-1: the persisted content rebuilds the identical report in a fresh process, only for the
    pinned sha256; malformed content is refused."""
    content = json.loads(json.dumps(leakage.report_content(report)))
    monkeypatch.setattr(leakage, "_ISSUED", set())
    assert not leakage.verify_report(report)  # a fresh process: nothing issued
    r = leakage._load_persisted_report(content, report.sha256)
    assert r == report and r.sha256 == report.sha256 and leakage.verify_report(r) and r.passed
    with pytest.raises(ValueError, match="pinned"):
        leakage._load_persisted_report(content, "0" * 64)
    for mutate in (lambda c: c.pop("seed"), lambda c: c.update(seed="0"), lambda c: c.update(kind="X"),
                   lambda c: c["l9a"][0].update(passed=1), lambda c: c["k7"][0].update(resolution="maybe")):
        bad = json.loads(json.dumps(content))
        mutate(bad)
        with pytest.raises(ValueError):
            leakage._load_persisted_report(bad, report.sha256)
    with pytest.raises(ValueError, match="run_leakage_checks"):
        leakage.report_content(dataclasses.replace(report, commit="c" * 40))


def test_holdout_checks_need_a_passed_pre_report(report, leaky):
    with pytest.raises(ValueError, match="did not pass"):
        lf.holdout_report(leaky, _runs())
    forged = dataclasses.replace(report, commit="c" * 40)
    with pytest.raises(ValueError, match="run_leakage_checks"):
        lf.holdout_report(forged, _runs())
    h = lf.holdout_report(report, _runs())
    with pytest.raises(TypeError):
        leakage.HoldoutLeakageReport(**{f.name: getattr(h, f.name) for f in dataclasses.fields(h)
                                        if f.name != "_issuer"})
    assert not leakage.verify_report(dataclasses.replace(h, findings=(("L1", "x"),)))


# ------------------------------------------------------------------------------------------ DT4 record


def test_report_sha256_recorded_in_dt4(report):
    from luna.v7 import runrecord

    runs = _runs()
    hold = lf.holdout_report(report, runs)
    base = dict(commit=lf.COMMIT, dirty=False, config={"h": 72}, data_manifests=lf.MANIFESTS,
                seeds=runrecord.default_seeds(), uv_lock_sha256="c" * 64, libraries={"python": "3.12"},
                convergence=(), runs=runs)
    rec = runrecord.run_record(**base, leakage_reports=[report, hold])
    assert rec["leakage_reports"] == [
        {"kind": "pre_opening", "sha256": report.sha256, "passed": True, "ml_star": ["A2", 72]},
        {"kind": "holdout", "sha256": hold.sha256, "pre_sha256": report.sha256, "findings": 0}]
    assert runrecord.run_record(**base)["leakage_reports"] == []
    assert runrecord.run_record(**base)["run_id"] != rec["run_id"]  # content-bound
    with pytest.raises(ValueError, match="leakage_reports"):
        runrecord.run_record(**base, leakage_reports=[dataclasses.replace(report, commit="c" * 40)])
