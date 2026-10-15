"""Arms and scores (S-7.2): exact values on synthetic inputs."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from luna.carry.config import load_carry_config
from luna.v7 import arms
from luna.v7.arms import (
    V5Candidate,
    a0_score,
    a0_v5_reference,
    a1_keep,
    arm_scores,
    cost_side,
    frozen_v5_config,
    knobs_sha256,
    ml_score,
    ts_ar1_r8,
    ts_ewma_r8,
)
from luna.v7.data.instances import Instance
from luna.v7.timeutil import MIN, D, H


def test_a0_formula_units():
    cost = cost_side(0.0003)
    assert cost == pytest.approx(0.0005 + 0.0003 + 0.0002)
    # FL30 = 0.0008 per 8 h -> 1e-4 per hour; short earns it: c = 1e-4
    s = a0_score(0.0008, -1, 72, cost)
    assert s == pytest.approx(1e-4 * 72 - 1.5 * 2 * cost - 5e-4)
    # long on the same FL30 pays it
    assert a0_score(0.0008, 1, 72, cost) == pytest.approx(-1e-4 * 72 - 3 * cost - 5e-4)
    # forgetting the /8 would change the result by a factor 8 in c
    assert s != pytest.approx(0.0008 * 72 - 3 * cost - 5e-4)
    assert math.isnan(a0_score(float("nan"), 1, 72, cost))
    # hurdle is strict
    assert not arms.a0_passes(0.0) and arms.a0_passes(1e-12) and not arms.a0_passes(float("nan"))
    with pytest.raises(ValueError):
        a0_score(0.0, 0, 72, cost)


def test_a1_veto_threshold():
    assert a1_keep(0.50) and a1_keep(0.9)
    assert not a1_keep(0.4999) and not a1_keep(float("nan"))


def _cands():
    return pd.DataFrame(
        {
            "symbol": ["B", "A", "C", "D"],
            "side": [-1, -1, 1, -1],
            "gate_ok": True,
            "fl30_r8": [0.0016, 0.0016, -0.0024, 0.0002],
            "cost_side": 0.001,
            "yhat_en": [5.0, 5.0, 9.0, -1.0],
            "yhat_cal_en": [1.0, 1.0, -2.0, 3.0],
            "yhat_lgb_reg": [1.0, 2.0, 3.0, 4.0],
            "yhat_cal_lgb_reg": [1.0, 1.0, 1.0, 1.0],
            "p_logit_en_cal": [0.9, 0.9, 0.3, 0.9],
            "p_lgb_clf_cal": [0.9, 0.2, 0.9, 0.9],
        }
    )


def test_a0_a1_order_and_veto():
    r0 = arm_scores("A0", _cands(), 72)
    # A and B tie on score -> by symbol; C (long on negative FL) scores highest
    assert list(r0.symbol) == ["C", "A", "B", "D"]
    assert list(r0.passes_hurdle) == [True, True, True, False]  # D: 2.5e-5*72-.003-.0005 < 0
    assert not r0.vetoed.any()
    r1 = arm_scores("A1", _cands(), 72, ml_star="A2")  # logit_en: C vetoed
    assert list(r1.symbol) == ["C", "A", "B", "D"]  # A0 order kept
    # DK-42 / red-team M8: the veto is a separate, entry-only flag; A1's hurdle is A0's s_rule > 0
    assert list(r1.passes_hurdle) == [True, True, True, False]
    assert list(r1.vetoed) == [True, False, False, False]
    r1b = arm_scores("A1", _cands(), 72, ml_star="A3")  # lgb_clf: A vetoed
    assert dict(zip(r1b.symbol, r1b.vetoed, strict=True)) == {
        "C": False,
        "A": True,
        "B": False,
        "D": False,
    }
    assert list(r1b.passes_hurdle) == list(r0.passes_hurdle)
    nan = _cands().assign(p_logit_en_cal=float("nan"))
    assert arm_scores("A1", nan, 72, ml_star="A2").vetoed.all()  # a missing P-hat vetoes (fail closed)
    with pytest.raises(ValueError):
        arm_scores("A1", _cands(), 72)


def test_ml_hurdle():
    assert ml_score(5.0, 0.1) == (5.0, True)
    assert ml_score(5.0, 0.0) == (5.0, False)
    assert ml_score(5.0, float("nan"))[1] is False
    r2 = arm_scores("A2", _cands(), 72)
    # ranked by raw yhat (C 9, then A/B tie by symbol, then D), hurdle on the calibrated value
    assert list(r2.symbol) == ["C", "A", "B", "D"]
    assert list(r2.passes_hurdle) == [False, True, True, True]
    assert not r2.vetoed.any()
    r3 = arm_scores("A3", _cands(), 72)
    assert list(r3.symbol) == ["D", "C", "A", "B"] and r3.passes_hurdle.all()


def test_ml_cannot_add_gate_failing_candidates():
    c = _cands()
    c.loc[0, "gate_ok"] = False
    for arm in ("A0", "A2", "A3"):
        with pytest.raises(ValueError, match="already-gated"):
            arm_scores(arm, c, 72)
    d = pd.concat([_cands(), _cands().iloc[:1]])
    with pytest.raises(ValueError, match="duplicate"):
        arm_scores("A2", d, 72)


T = 1_800_000_000_000_000_000 - 1_800_000_000_000_000_000 % (8 * H) + 8 * H * 100  # 8h-aligned
INST = Instance(0, T - 100 * D, None, None)


def _f(ages_h, rates, known_lag=MIN, interval=8):
    ts = np.array([T - int(a * H) for a in ages_h], dtype=np.int64)
    return pd.DataFrame(
        {
            "funding_ts": ts,
            "rate": np.asarray(rates, float),
            "interval_h_explicit": pd.array([interval] * len(ts), dtype="Int64"),
            "known_at": ts + known_lag,
        }
    )


def test_ewma_half_life():
    # two valid 8 h records 72 h apart (ages 80 h and 8 h, spaced by 72 h is not valid) -> use 8 h grid
    ages = [80.0, 72.0, 64.0, 56.0, 48.0, 40.0, 32.0, 24.0, 16.0, 8.0]
    r = [0.0] * 8 + [1.0, 1.0]
    f = _f(ages, r)
    w = np.array([0.5 ** (a / 72) for a in ages])
    assert ts_ewma_r8(f, INST, T) == pytest.approx(float(w @ np.array(r) / w.sum()))
    # half-life: weight ratio of records 72 h apart is exactly 2
    assert w[-1] / w[0] == pytest.approx(2.0)


def test_ewma_pit_excludes_late_known_record():
    ages = [40.0, 32.0, 24.0, 16.0, 8.0]
    f = _f(ages, [0.0, 0.0, 0.0, 0.0, 1.0])
    base = ts_ewma_r8(f, INST, T)
    late = f.copy()
    late.loc[4, "known_at"] = T - 30 * 10**9  # funding_ts <= t - 60 s but known_at > t - 1 min
    assert ts_ewma_r8(late, INST, T) == 0.0 != base
    assert ts_ewma_r8(f.iloc[:4], INST, T) == 0.0
    # records of an earlier instance are not used
    inst2 = Instance(1, T - 20 * H, None, None)
    w16, w8 = 0.5 ** (16 / 72), 0.5 ** (8 / 72)
    assert ts_ewma_r8(f, inst2, T) == pytest.approx(w8 / (w16 + w8))  # only the 16 h / 8 h records


def _ar1_frame(n=14):
    x = [0.001]
    for _ in range(n - 1):
        x.append(0.0002 + 0.5 * x[-1])
    ages = [8.0 * (n - k) for k in range(n)]
    return _f(ages, x), np.array(x)


def test_ar1_projection():
    f, x = _ar1_frame()
    cur, path = x[-1], []
    for _ in range(3):
        cur = 0.0002 + 0.5 * cur
        path.append(cur)
    got = ts_ar1_r8(f, INST, T, 3)
    assert got == pytest.approx(np.mean(path), rel=1e-6)
    assert got != pytest.approx(path[-1], rel=1e-9)  # horizon mean, not the terminal value
    # a record known too late is excluded from the fit and from the last state
    late = f.copy()
    late.loc[len(f) - 1, "known_at"] = T - 30 * 10**9
    assert ts_ar1_r8(late, INST, T, 3) != pytest.approx(got, rel=1e-9)
    # too few pairs / constant series (including all zeros) -> NaN
    assert math.isnan(ts_ar1_r8(f.iloc[:8], INST, T, 3))
    assert math.isnan(ts_ar1_r8(_f([8.0 * (14 - k) for k in range(14)], [0.0] * 14), INST, T, 3))
    assert math.isnan(ts_ar1_r8(_f([8.0 * (14 - k) for k in range(14)], [0.001] * 14), INST, T, 3))


def test_ar1_pairs_do_not_span_an_invalid_record():
    f, _ = _ar1_frame(24)
    base = ts_ar1_r8(f, INST, T, 3)
    # shifting one record off-schedule makes it (and its successor) invalid: pairs touching them vanish
    g = f.copy()
    g.loc[10, "funding_ts"] -= 2 * H
    g["known_at"] = g["funding_ts"] + MIN
    ts, x = arms._valid_r8(g, INST, T)
    assert np.isnan(x).sum() == 2
    got = ts_ar1_r8(g, INST, T, 3)
    assert math.isfinite(got)
    # reference: pairs formed only from adjacent valid records
    ok = np.isfinite(x[:-1]) & np.isfinite(x[1:])
    u, v = x[:-1][ok], x[1:][ok]
    phi = np.mean((u - u.mean()) * (v - v.mean())) / np.var(u)
    a = v.mean() - phi * u.mean()
    cur, path = x[-1], []
    for _ in range(3):
        cur = a + phi * cur
        path.append(cur)
    assert got == pytest.approx(np.mean(path), rel=1e-9)
    assert base == pytest.approx(ts_ar1_r8(f, INST, T, 3))


def test_a0_v5_reference_uses_frozen_config():
    assert knobs_sha256(arms.V5_FROZEN_KNOBS) == arms.V5_FROZEN_KNOBS_SHA256
    cfg = frozen_v5_config(["A", "B", "C", "D", "E"])
    assert (cfg.k, cfg.switch_horizon_hours, cfg.switch_cost_multiplier) == (2, 168.0, 1.5)
    assert (cfg.safety_buffer_bps, cfg.min_hold_hours, cfg.decision_mode) == (5.0, 24.0, "cost_aware")
    assert cfg.universe == ["A", "B", "C", "D", "E"]
    # strict fail-closed on a changed knob
    base = load_carry_config()
    mutated = dict(arms.V5_FROZEN_KNOBS, switch_horizon_hours=72.0)
    assert knobs_sha256(mutated) != arms.V5_FROZEN_KNOBS_SHA256
    assert base.switch_horizon_hours == 168.0
    cands = [
        V5Candidate("A", 0.0024, 0.001, True),  # high positive FL -> short
        V5Candidate("B", 0.0016, 0.001, True),
        V5Candidate("C", -0.0016, 0.001, True),
        V5Candidate("D", -0.0024, 0.001, True),
        V5Candidate("E", 0.0, 0.001, True),
    ]
    d = a0_v5_reference(pd.Timestamp("2026-10-14T00:05:00Z"), 1000.0, cands)
    assert d.ideal_short == ["A", "B"] and d.ideal_long == ["D", "C"]
    opened = {(o.asset, o.side) for a in d.actions for o in a.legs}
    assert opened == {("A", -1), ("B", -1), ("C", 1), ("D", 1)}


def test_a0_v5_reference_fails_closed_on_config_drift(monkeypatch):
    drift = dict(arms.V5_FROZEN_KNOBS, k=3)
    monkeypatch.setattr(arms, "V5_FROZEN_KNOBS", drift)
    with pytest.raises(RuntimeError, match="frozen"):
        frozen_v5_config(["A", "B", "C", "D", "E", "F"])
