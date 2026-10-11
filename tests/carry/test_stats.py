"""Protocol §10 statistics, guardrails and the per-row verdict (synthetic series only)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from luna.carry import stats

V4_STATS = Path(__file__).resolve().parents[3] / "LunaTradingBot-v4" / "luna" / "v4" / "stats.py"


def _series(seed, n=233, mu=1e-4, sd=1e-3):
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2025-11-10", periods=n, freq="D", tz="UTC")
    return pd.Series(mu + sd * rng.standard_normal(n), index=idx)


@pytest.mark.skipif(not V4_STATS.exists(), reason="V4 worktree not present")
def test_bootstrap_equals_the_v4_method_on_synthetic_data():
    spec = importlib.util.spec_from_file_location("v4stats_readonly", V4_STATS)
    v4 = importlib.util.module_from_spec(spec)
    sys.modules["v4stats_readonly"] = v4
    try:
        spec.loader.exec_module(v4)
    finally:
        sys.modules.pop("v4stats_readonly", None)
    x = _series(1).to_numpy()
    for block in (28, 14):
        a = stats.mbb(x, block, B=2000, seed=stats.SEED)
        b = v4.mbb_pvalue(x, block, B=2000, seed=stats.SEED)
        assert a.p == b.p and np.array_equal(a.boot_means, b.boot_means)
        assert a.lower_bound(0.05) == b.lower_bound(0.05) and a.upper_bound(0.05) == b.upper_bound(0.05)
    r = _series(2)
    for f in ("sharpe", "max_drawdown", "worst_day", "es975"):
        assert getattr(stats, f)(r) == getattr(v4, f)(r)
    p = {"a": 0.01, "b": 0.04, "c": 0.02}
    assert stats.holm(p, 0.05) == v4.holm(p, 0.05)


def test_v5_seed_and_blocks_are_the_frozen_ones():
    assert stats.SEED == 20261010 and stats.BLOCKS == (28, 14) and stats.B == 10_000


def _row(carry, ret, kill=False):
    return stats.RowSeries(carry_net=carry, ret=ret, net_pnl=float(ret.sum() * 265), kill=kill)


def test_delta_carry_pass_needs_both_blocks():
    w = _series(3, mu=0.0)
    e = w + 5e-4  # a clear, constant advantage
    v = stats.row_verdict(_row(w, w), _row(e, e), cost2x_delta_mean=1e-4, e_only_kill=False, B=2000)
    assert v["delta_carry"]["pass"] and v["verdict"] == "ADVANTAGE" and not any(v["guardrails"].values())


def test_carry_only_names_the_broken_guardrail():
    w = _series(3, mu=0.0)
    e = w + 5e-4
    worse_ret = w - 2e-3  # E loses overall
    v = stats.row_verdict(_row(w, w), _row(e, worse_ret), cost2x_delta_mean=1e-4, e_only_kill=False, B=2000)
    assert v["verdict"] == "CARRY ONLY" and v["guardrails"]["G1"]


def test_g6_and_g5_break_advantage():
    w = _series(3, mu=0.0)
    e = w + 5e-4
    assert stats.row_verdict(_row(w, w), _row(e, w), cost2x_delta_mean=0.0, e_only_kill=False,
                             B=2000)["guardrails"]["G6"]
    v = stats.row_verdict(_row(w, w), _row(e, e), cost2x_delta_mean=1e-4, e_only_kill=True, B=2000)
    assert v["guardrails"]["G5"] and v["verdict"] == "CARRY ONLY"


def test_no_advantage_and_worse():
    w = _series(3, mu=0.0)
    noise = _series(9, mu=0.0, sd=1e-3)
    v = stats.row_verdict(_row(w, w), _row(w + noise * 0.1, w), cost2x_delta_mean=0.0, e_only_kill=False, B=2000)
    assert v["verdict"] == "NO ADVANTAGE SHOWN"
    v = stats.row_verdict(_row(w, w), _row(w - 5e-4, w), cost2x_delta_mean=-1e-4, e_only_kill=False, B=2000)
    assert v["verdict"] == "WORSE"


def test_guardrail_thresholds_are_the_frozen_ones():
    g = stats.guardrails(
        w={"net": 10.0, "mdd": 0.05, "es": -0.01, "worst": -0.02, "sharpe": 1.0},
        e={"net": 10.0, "mdd": 0.08, "es": -0.015, "worst": -0.03, "sharpe": 0.70},
        cost2x_delta_mean=1e-9, e_only_kill=False)
    assert g == {"G1": True, "G2": False, "G3": False, "G4": False, "G5": False, "G6": False}
    g = stats.guardrails(
        w={"net": 10.0, "mdd": 0.05, "es": -0.01, "worst": -0.02, "sharpe": 1.0},
        e={"net": 10.01, "mdd": 0.0801, "es": -0.0151, "worst": -0.0301, "sharpe": 0.69},
        cost2x_delta_mean=1e-9, e_only_kill=False)
    assert g == {"G1": False, "G2": True, "G3": True, "G4": True, "G5": False, "G6": False}
