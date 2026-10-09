"""V4 evidence statistics and verdicts (spec §6, §7.3, §7.4, §9)."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from luna.v4 import stats as s

DAYS = pd.date_range("2026-01-01T00:00:00Z", periods=200, freq="D")


def _series(mean, sd=0.01, n=200, seed=0):
    r = np.random.default_rng(seed).normal(0.0, sd, n)
    r = r - r.mean() + mean
    return pd.Series(r, index=DAYS[:n])


# --- bootstrap ------------------------------------------------------------------------------------


def test_bootstrap_constants():
    assert (s.B, s.SEED, s.BLOCKS) == (10_000, 20261009, (28, 14))
    assert s.ALPHA_FORWARD == pytest.approx(0.05 / 3) and s.ALPHA_STAGE1 == 0.05


def test_positive_drift_is_rejected_and_zero_drift_is_not():
    pos = s.mbb_pvalue(_series(0.01).to_numpy(), 28)
    assert pos.p < 0.001
    zero = s.mbb_pvalue(_series(0.0).to_numpy(), 28)
    assert zero.p > 0.2
    for b in s.BLOCKS:
        assert s.mbb_pvalue(_series(0.01).to_numpy(), b).p < 0.05
        assert s.mbb_pvalue(_series(0.0).to_numpy(), b).p > 0.05


def test_pvalue_formula_and_determinism():
    x = _series(0.001, seed=3).to_numpy()
    r1, r2 = s.mbb_pvalue(x, 14, B=500), s.mbb_pvalue(x, 14, B=500)
    assert r1.p == r2.p
    m = x.mean()
    assert r1.m_hat == pytest.approx(m)
    assert r1.p == pytest.approx((1 + np.sum(r1.boot_means - m >= m)) / 501)


def test_bootstrap_means_are_moving_block_resamples():
    x = np.arange(10, dtype=float)
    r = s.mbb_pvalue(x, 5, B=50)
    # a resample of 2 blocks of 5 consecutive values: mean in the range of window means
    win = [x[i:i + 5].mean() for i in range(6)]
    assert r.boot_means.min() >= min(win) - 1e-12 and r.boot_means.max() <= max(win) + 1e-12


def test_lower_bound_positive_iff_reject():
    x = _series(0.004, seed=5).to_numpy()
    r = s.mbb_pvalue(x, 28)
    for alpha in (0.01, 0.05, 0.2):
        assert (r.lower_bound(alpha) > 0) == (r.p <= alpha) or abs(r.p - alpha) < 2e-3


# --- Holm ------------------------------------------------------------------------------------------


def test_holm_step_down_on_hand_made_pvalues():
    out = s.holm({"a": 0.01, "b": 0.04, "c": 0.03, "d": 0.2}, 0.05)
    assert {k: v["reject"] for k, v in out.items()} == {"a": True, "b": False, "c": False, "d": False}
    assert out["a"]["alpha"] == pytest.approx(0.0125) and out["c"]["alpha"] == pytest.approx(0.05 / 3)
    all_rej = s.holm({"x": 0.001, "y": 0.01, "z": 0.02}, 0.05)
    assert all(v["reject"] for v in all_rej.values())
    assert s.holm({"x": float("nan"), "y": 0.001}, 0.05)["x"]["reject"] is False


# --- risk metrics -----------------------------------------------------------------------------------


def test_sharpe_drawdown_worst_day_es():
    r = pd.Series([0.01, -0.02, 0.03, -0.01], index=DAYS[:4])
    assert s.sharpe(r) == pytest.approx(r.mean() / r.std(ddof=1) * np.sqrt(365))
    eq = np.cumprod(1 + r.to_numpy())
    assert s.max_drawdown(r) == pytest.approx(1 - eq[1] / eq[0])
    assert s.worst_day(r) == -0.02
    big = pd.Series(np.linspace(-0.05, 0.05, 201), index=pd.date_range("2025-01-01", periods=201, tz="UTC"))
    tail = np.sort(big.to_numpy())[: int(np.ceil(0.025 * 201))]
    assert s.es975(big) == pytest.approx(tail.mean())
    assert np.isnan(s.sharpe(pd.Series([0.0, 0.0], index=DAYS[:2])))


def test_c7_counts_only_full_calendar_months():
    idx = pd.date_range("2026-01-15T00:00Z", "2026-05-10T00:00Z", freq="D")
    r = pd.Series(0.001, index=idx)
    r[(idx >= "2026-03-01") & (idx < "2026-04-01")] = -0.001
    out = s.monthly_check(r)
    assert out["months"] == 3 and out["positive"] == 2  # Feb, Mar, Apr are full
    assert out["pass"] is True  # 2/3 >= 60%
    assert s.monthly_check(r[r.index < "2026-04-01"])["pass"] is False  # only 2 full months


# --- M1–M4 -------------------------------------------------------------------------------------------


def test_minimum_evidence_thresholds():
    ok = s.evidence_checks("carry", window_days=182, dq_valid_days=164, active_days=60,
                           entry_assets=["A", "B", "A", "C"], decisions=0)
    assert ok == {"M1": True, "M2": True, "M3": True, "M4": True}
    bad = s.evidence_checks("carry", window_days=181, dq_valid_days=162, active_days=59,
                            entry_assets=["A", "A", "A", "A"], decisions=0)
    assert bad == {"M1": False, "M2": False, "M3": False, "M4": False}
    h2 = s.evidence_checks("h2", window_days=182, dq_valid_days=182, active_days=150, entry_assets=[],
                           decisions=24)
    assert all(h2.values())
    assert not s.evidence_checks("h2", window_days=182, dq_valid_days=182, active_days=149, entry_assets=[],
                                 decisions=23)["M4"]


# --- verdicts ----------------------------------------------------------------------------------------


def _evidence(cid, kind, mean, **over):
    r = _series(mean, sd=0.002, n=200)
    base = dict(config_id=cid, kind=kind, primary=r, cost2x=r - 0.0001, carry_value=1.0,
                carry_net=r * 0.5 if kind == "h2" else None, null_sharpes=[0.1] * 5,
                always_sharpe=0.2 if kind == "carry" else None, had_entries=True,
                m={"M1": True, "M2": True, "M3": True, "M4": True})
    base.update(over)
    return s.ConfigEvidence(**base)


def _family(mean=0.003, **over):
    from luna.v4.strategies import CONFIGS

    return {c: _evidence(c, v.account, mean, **over.get(c, {})) for c, v in CONFIGS.items()}


def test_strong_synthetic_evidence_is_go_in_stage2():
    out = s.evaluate(_family(), stage=2, B=1000)
    assert all(v["verdict"] == "GO" for v in out.values())
    assert all(v["criteria"]["C2"] for v in out.values())


def test_missing_minimum_evidence_is_insufficient_data():
    fam = _family(**{"H1-Y10": {"m": {"M1": True, "M2": False, "M3": True, "M4": True}}})
    assert s.evaluate(fam, stage=2, B=500)["H1-Y10"]["verdict"] == "INSUFFICIENT DATA"


def test_failing_carry_component_is_no_go_even_with_positive_total():
    fam = _family(**{"H1-Y20": {"carry_value": -0.5}})
    out = s.evaluate(fam, stage=2, B=500)
    assert out["H1-Y20"]["verdict"] == "NO-GO" and not out["H1-Y20"]["criteria"]["C6"]


def test_c6_h2_holm_on_the_four_carry_net_series():
    neg = _series(-0.001, sd=0.002)
    fam = _family(**{"H2-L7-k1": {"carry_net": neg}})
    out = s.evaluate(fam, stage=2, B=500)
    assert not out["H2-L7-k1"]["criteria"]["C6"] and out["H2-L7-k2"]["criteria"]["C6"]
    assert set(out["H2-L7-k1"]["c6_holm"]) == {"b28", "b14"}


def test_c4_fails_without_entries_and_against_n_always():
    fam = _family(**{"H1-Y10": {"had_entries": False}, "H5-HFWB": {"always_sharpe": 1e9}})
    out = s.evaluate(fam, stage=2, B=500)
    assert not out["H1-Y10"]["criteria"]["C4"] and not out["H5-HFWB"]["criteria"]["C4"]


def test_c5_risk_limits():
    r = _series(0.003, sd=0.002)
    r.iloc[10] = -0.06
    out = s.evaluate(_family(**{"H1-Y10": {"primary": r}}), stage=2, B=500)
    assert not out["H1-Y10"]["criteria"]["C5"]


def test_stage1_never_gives_go_and_picks_a_candidate():
    out = s.evaluate(_family(), stage=1, B=500)
    assert all(v["verdict"] != "GO" for v in out.values())
    assert all(v["criteria_pass"] for v in out.values())
    assert s.stage1_candidate(out) in out


def test_prior_go_configs_leave_the_holm_family():
    out = s.evaluate(_family(), stage=2, B=500, prior_go=["H1-Y10"])
    assert "H1-Y10" not in out and len(out["H1-Y20"]["holm"]["b28"]) == 6


def test_nulls_can_never_be_go():
    with pytest.raises(ValueError):
        s.evaluate({"N-ALWAYS:H1-Y10": _evidence("N-ALWAYS:H1-Y10", "carry", 0.01)}, stage=2, B=100)
    with pytest.raises(ValueError):
        s.verdict("N-SHUFFLE:1:H2-L7-k1", {"C1": True}, stage=2)


def test_deployable_now_is_always_no_with_four_reasons():
    d = s.deployable_now()
    assert d["deployable_now"] == "NO" and len(d["reasons"]) == 4


# --- Stage 1 ADVANCE / KILL (§7.3) -------------------------------------------------------------------


def test_upper_bound_is_the_one_sided_percentile_bound():
    x = _series(-0.004, seed=5).to_numpy()
    r = s.mbb_pvalue(x, 28)
    assert r.upper_bound(0.05) == pytest.approx(r.m_hat - np.quantile(r.boot_means - r.m_hat, 0.05))
    assert r.upper_bound(0.05) < 0


def _s1(kind="carry", mean=0.001, carry_value=1.0, carry_net=None, active_days=100):
    return s.stage1_decision(kind, _series(mean, sd=0.002), carry_value, carry_net, active_days, B=500)


def test_stage1_kill_rules():
    assert _s1()["decision"] == "ADVANCE TO FORWARD"
    neg = _s1(mean=-0.003)
    assert neg["decision"] == "KILL" and "a" in neg["reasons"]
    # negative but not statistically negative: (a) needs mean < 0 AND upper bound < 0
    noisy = s.stage1_decision("carry", _series(-0.00005, sd=0.01), 1.0, None, 100, B=500)
    assert noisy["decision"] == "ADVANCE TO FORWARD"
    assert _s1(carry_value=0.0)["reasons"] == ["b"]
    assert _s1(kind="h2", carry_value=None, carry_net=_series(-1e-5))["reasons"] == ["b"]
    assert _s1(kind="h2", carry_value=None, carry_net=_series(1e-5))["decision"] == "ADVANCE TO FORWARD"
    assert _s1(active_days=0)["reasons"] == ["c"]


def test_stage1_evaluate_reports_advance_or_kill_and_hypothesis_summary():
    fam = _family(**{"H1-Y10": {"carry_value": -1.0}, "H1-Y20": {"carry_value": -1.0}})
    out = s.evaluate(fam, stage=1, B=300)
    assert out["H1-Y10"]["verdict"] == "KILL" and out["H2-L7-k1"]["verdict"] == "ADVANCE TO FORWARD"
    hyp = s.hypothesis_summary(out)
    assert hyp == {"H1": False, "H2": True, "H5": True}


def test_forward_holm_family_keeps_killed_configs_with_p_one():
    fam = _family()
    killed = ["H1-Y20", "H2-L30-k2"]
    for k in killed:
        fam.pop(k)
    out = s.evaluate(fam, stage=2, B=500, killed=killed)
    assert set(out) == set(fam)
    fam28 = out["H1-Y10"]["holm"]["b28"]
    assert len(fam28) == 7 and fam28["H1-Y20"]["p"] == 1.0 and not fam28["H1-Y20"]["reject"]
    assert len(out["H2-L7-k1"]["c6_holm"]["b28"]) == 4
    assert out["H2-L7-k1"]["c6_holm"]["b28"]["H2-L30-k2"]["p"] == 1.0
    # family = 7 - previous GO, killed stay with p = 1
    out2 = s.evaluate({k: v for k, v in fam.items() if k != "H1-Y10"}, stage=2, B=500, killed=killed,
                      prior_go=["H1-Y10"])
    assert len(out2["H5-HFWB"]["holm"]["b28"]) == 6


def test_stage1_decision_reports_its_inputs():
    d = s.stage1_decision("h2", _series(0.001, sd=0.002), None, _series(2e-4), 100, B=300)
    assert set(d) >= {"mean_daily", "ub95_b28", "lb95_b28", "lb95_b14", "carry_value", "carry_net_mean"}
    assert d["carry_net_mean"] == pytest.approx(2e-4) and d["carry_value"] is None
    c = s.stage1_decision("carry", _series(0.001, sd=0.002), 1.5, None, 100, B=300)
    assert c["carry_value"] == 1.5 and c["carry_net_mean"] is None
    assert c["lb95_b28"] < c["mean_daily"] < c["ub95_b28"]
