import math

import numpy as np
import pytest

from luna.research.stats import benjamini_hochberg, block_bootstrap_ci, cvar, profit_factor


def test_bh_known_example():
    mask = benjamini_hochberg(np.array([0.01, 0.02, 0.03, 0.04, 0.5]), 0.05)
    assert mask.tolist() == [True, True, True, True, False]


def test_bh_step_up_and_order_preserved():
    # sorted: .001 (thr .01), .02 (.02), .035 (.03) fails, .04 (.04) passes -> step-up keeps 1..4
    p = np.array([0.04, 0.001, 0.035, 0.02, 0.9])
    assert benjamini_hochberg(p, 0.05).tolist() == [True, True, True, True, False]
    assert benjamini_hochberg(np.array([0.2, 0.3]), 0.05).tolist() == [False, False]
    assert benjamini_hochberg(np.array([]), 0.05).tolist() == []


def test_bootstrap_ci_contains_mean():
    rng = np.random.default_rng(1)
    values = rng.normal(0.5, 2.0, 300)
    days = np.repeat(np.arange(100), 3)
    mean, lo, hi = block_bootstrap_ci(values, days, n=2000, ci=0.90, seed=0)
    assert mean == pytest.approx(values.mean())
    assert lo < mean < hi
    assert hi - lo < 1.5
    again = block_bootstrap_ci(values, days, n=2000, ci=0.90, seed=0)
    assert again == (mean, lo, hi)  # deterministic per seed


def test_bootstrap_resamples_whole_days():
    # one day holds all the variance: every resample mean is a mix of whole-day blocks
    values = np.array([10.0, 10.0, 0.0, 0.0])
    days = np.array([1, 1, 2, 2])
    mean, lo, hi = block_bootstrap_ci(values, days, n=500, ci=0.90, seed=3)
    assert mean == 5.0
    assert lo == 0.0 and hi == 10.0


def test_profit_factor():
    assert profit_factor(np.array([1.0, 2.0, -1.0, -0.5])) == pytest.approx(2.0)
    assert profit_factor(np.array([1.0, 2.0])) == math.inf
    assert profit_factor(np.array([-1.0])) == 0.0
    assert math.isnan(profit_factor(np.array([])))


def test_cvar():
    x = np.arange(1.0, 101.0)
    assert cvar(x, 0.05) == pytest.approx(3.0)  # mean of the worst 5 values
    assert cvar(np.array([-0.03, 0.01, 0.02]), 0.05) == pytest.approx(-0.03)
    assert math.isnan(cvar(np.array([]), 0.05))
