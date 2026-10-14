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
from luna.v7.timeutil import H


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
    r1 = arm_scores("A1", _cands(), 72, ml_star="A2")  # logit_en: C vetoed
    assert list(r1.symbol) == ["C", "A", "B", "D"]  # A0 order kept
    assert list(r1.passes_hurdle) == [False, True, True, False]
    r1b = arm_scores("A1", _cands(), 72, ml_star="A3")  # lgb_clf: A vetoed
    assert dict(zip(r1b.symbol, r1b.passes_hurdle, strict=True)) == {
        "C": True,
        "A": False,
        "B": True,
        "D": False,
    }
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


def test_ewma_half_life():
    t = 1_800_000_000 * 10**9
    ts = np.array([t - 72 * H, t - 2 * H])
    # weights 0.5 and 0.5**(2/72)
    w0, w1 = 0.5, 0.5 ** (2 / 72)
    assert ts_ewma_r8(ts, [1.0, 0.0], t) == pytest.approx(w0 / (w0 + w1))
    # an observation inside the 60 s PIT lag is ignored
    ts2 = np.append(ts, t - 30 * 10**9)
    assert ts_ewma_r8(ts2, [1.0, 0.0, 99.0], t) == pytest.approx(w0 / (w0 + w1))
    assert math.isnan(ts_ewma_r8(ts, [float("nan")] * 2, t))


def test_ar1_projection():
    # exact AR(1) with intercept: r[k+1] = 0.0002 + 0.5 r[k] (deterministic, no noise)
    x = [0.001]
    for _ in range(40):
        x.append(0.0002 + 0.5 * x[-1])
    x = np.array(x[:14])  # not yet converged: the fit recovers phi=.5, a=.0002 exactly
    cur, path = x[-1], []
    for _ in range(3):
        cur = 0.0002 + 0.5 * cur
        path.append(cur)
    assert ts_ar1_r8(x, 3) == pytest.approx(np.mean(path), rel=1e-6)
    # horizon mean differs from the terminal value
    assert ts_ar1_r8(x, 3) != pytest.approx(path[-1], rel=1e-9)
    assert math.isnan(ts_ar1_r8(x[:5], 3)) and math.isnan(ts_ar1_r8(np.full(20, 0.001), 3))
    # a mean-reverting series with a spike: projection decays towards the mean
    y = np.array([0.0, 0.002] * 10 + [0.01])
    assert ts_ar1_r8(y, 9) < 0.01


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
