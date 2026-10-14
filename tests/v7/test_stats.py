"""S-13.1 bootstrap, Holm, Sharpe (luna.v7.stats) on small hand-checkable series."""

from __future__ import annotations

import math

import numpy as np
import pytest

from luna.v7 import stats


def test_daily_returns_hand():
    r = stats.daily_returns([1.0, 1.1, 0.99, 0.99])
    assert np.allclose(r, [0.1, -0.1, 0.0])
    with pytest.raises(ValueError):
        stats.daily_returns([1.0, 0.0, 1.0])
    with pytest.raises(ValueError):
        stats.daily_returns([1.0, float("nan")])
    with pytest.raises(ValueError):
        stats.daily_returns([1.0])


def test_cbb_shapes_and_wrap():
    T, ell, B, seed = 5, 2, 7, 11
    idx = stats.cbb_indices(T, ell, B, seed)
    assert idx.shape == (B, T) and idx.dtype == np.int64
    # k = ceil(5 / 2) = 3 starts drawn uniformly from [0, 5) by default_rng(seed)
    starts = np.random.default_rng(seed).integers(0, T, size=(B, 3), dtype=np.int64)
    for b in range(B):
        want = [(s + i) % T for s in starts[b] for i in range(ell)][:T]
        assert idx[b].tolist() == want
    # every row is made of consecutive (mod T) runs: the wrap 4 -> 0 occurs somewhere
    big = stats.cbb_indices(10, 4, 2000, 3)
    assert big.min() == 0 and big.max() == 9
    pairs = np.stack([big[:, :-1].ravel(), big[:, 1:].ravel()], 1)
    within = np.concatenate([np.arange(i, i + 3) for i in range(0, 8, 4)])  # positions inside a block
    blk = big[:, within]
    nxt = big[:, within + 1]
    assert np.all(nxt == (blk + 1) % 10)
    assert np.any((pairs[:, 0] == 9) & (pairs[:, 1] == 0))
    # deterministic in the seed, different for another seed
    assert np.array_equal(stats.cbb_indices(T, ell, B, seed), idx)
    assert not np.array_equal(stats.cbb_indices(T, ell, B, seed + 1), idx)
    # ell >= T: one block, wrapped
    one = stats.cbb_indices(3, 5, 4, 0)
    s0 = np.random.default_rng(0).integers(0, 3, size=(4, 1), dtype=np.int64)
    assert one.tolist() == [[(s + i) % 3 for i in range(3)] for s in s0[:, 0]]


def test_paired_draws():
    rng = np.random.default_rng(5)
    r = rng.normal(0.001, 0.01, 40)
    a0 = rng.normal(0.0, 0.01, 40)
    x = r - a0
    res = stats.cbb_test(r, x, ells=(14, 28), B=500, seed=9)
    for ell in (14, 28):
        idx = stats.cbb_indices(40, ell, 500, 9)
        assert res.p_abs_by_ell[ell] == stats.p_centered(r, idx)
        assert res.p_diff_by_ell[ell] == stats.p_centered(x, idx)
        # pairing: per replicate, the bootstrap mean of x = that of r minus that of a0
        assert np.allclose(x[idx].mean(1), r[idx].mean(1) - a0[idx].mean(1))
    assert res.p_abs == max(res.p_abs_by_ell.values()) and res.p_diff == max(res.p_diff_by_ell.values())
    assert res.seed == 9 and res.B == 500
    with pytest.raises(ValueError):
        stats.cbb_test(r, x[:-1])


def test_p_value_formula():
    x = np.array([1.0, -1.0, 2.0])  # mean 2/3
    idx = np.array([[0, 0, 0], [1, 1, 1], [2, 2, 2], [0, 1, 2]])  # boot means 1, -1, 2, 2/3
    # count of mean* - mean >= mean <=> mean* >= 4/3: only the replicate with mean 2
    assert stats.p_centered(x, idx) == pytest.approx((1 + 1) / (4 + 1))
    # equality counts (>=): x = [0, 2], mean 1, replicate mean 2 -> 2 - 1 >= 1
    assert stats.p_centered([0.0, 2.0], np.array([[1, 1]])) == 1.0
    assert stats.p_centered([0.0, 2.0], np.array([[0, 0]])) == 0.5
    with pytest.raises(ValueError):
        stats.p_centered(x, np.array([[0, 1]]))
    # max over blocks
    y = np.random.default_rng(1).normal(0.0, 1.0, 30)
    want = max(stats.p_centered(y, stats.cbb_indices(30, ell, 300, 4)) for ell in (14, 28))
    assert stats.p_max_over_blocks(y, (14, 28), 300, 4) == want


def test_iut_max():
    assert stats.iut(0.01, 0.04) == 0.04
    assert stats.iut(0.06, 0.001) == 0.06
    for bad in (0.0, 1.5, float("nan")):
        with pytest.raises(ValueError):
            stats.iut(bad, 0.5)
    r = stats.CBBResult(0.1, 0.1, {14: 0.01, 28: 0.03}, {14: 0.049, 28: 0.02}, 10, 1)
    assert r.p_P == 0.049 and r.holds
    r2 = stats.CBBResult(0.1, 0.1, {14: 0.01, 28: 0.05}, {14: 0.01, 28: 0.02}, 10, 1)
    assert r2.p_P == 0.05 and not r2.holds  # strict < 0.05


def test_lb_min_over_blocks():
    x = np.array([1.0, 2.0, 3.0, 4.0])
    idx = np.array([[0, 0, 0, 0], [3, 3, 3, 3], [0, 1, 2, 3], [1, 2, 1, 2]] * 5)  # B = 20
    # boot means - 2.5: -1.5, 1.5, 0, 0 (x5 each); inverted_cdf q95 = order stat ceil(19) -> 1.5
    assert stats.lower_bound_idx(x, idx) == pytest.approx(2.5 - 1.5)
    y = np.random.default_rng(2).normal(0.001, 0.01, 60)
    per = [stats.lower_bound_idx(y, stats.cbb_indices(60, ell, 400, 7)) for ell in (14, 28)]
    assert per[0] != per[1]
    assert stats.lower_bound(y, (14, 28), 400, 7) == min(per)
    assert stats.lower_bound(y, (14,), 400, 7) == per[0]
    # the quantile is the ceil(0.95 B)-th order statistic
    d = np.sort(y[stats.cbb_indices(60, 14, 400, 7)].mean(1) - y.mean())
    assert per[0] == pytest.approx(y.mean() - d[380 - 1])


def test_holm_six():
    p = [0.01, 0.04, 0.03, 0.005, 0.5, 0.02]
    # sorted: .005*6=.03, .01*5=.05, .02*4=.08, .03*3=.09, .04*2=.08->.09, .5*1=.5
    assert stats.holm(p) == pytest.approx([0.05, 0.09, 0.09, 0.03, 0.5, 0.08])
    d = stats.holm(dict(a=0.01, b=None, c=0.02, d=0.2, e=0.3, f=0.001))
    # voided member b stays in the family (m = 6), ranked last as p = 1
    assert d["b"] is None
    assert d["f"] == pytest.approx(0.006) and d["a"] == pytest.approx(0.05) and d["c"] == pytest.approx(0.08)
    assert d["d"] == pytest.approx(0.6) and d["e"] == pytest.approx(0.6)
    assert stats.holm([0.4, 0.9]) == pytest.approx([0.8, 0.9])
    with pytest.raises(ValueError):
        stats.holm([1.2])


def test_sharpe_and_newey_west():
    r = np.array([0.01, -0.005, 0.02, 0.0, 0.003])
    s = stats.sharpe(r, lag=0)
    assert s.value == pytest.approx(r.mean() / r.std(ddof=1) * math.sqrt(365))
    assert s.adjusted == pytest.approx(s.value)
    s1 = stats.sharpe(r, lag=1)
    d = r - r.mean()
    lrv = (d @ d + 2 * 0.5 * (d[1:] @ d[:-1])) / 4
    assert s1.adjusted == pytest.approx(r.mean() / math.sqrt(lrv) * math.sqrt(365))
    assert s1.value == s.value
    assert stats.nw_lag(365) == 5 and stats.nw_lag(100) == 4
    assert stats.sharpe(np.full(365, 0.001)).lag == 5
    assert math.isnan(stats.sharpe([0.01, 0.01, 0.01]).value)
