import numpy as np
import pandas as pd
import pytest

from luna.features.core import (
    atr,
    ema,
    ewma_zscore,
    realized_vol,
    rolling_beta,
    rolling_corr,
    trend_strength,
)
from luna.features.pairs import half_life, spread
from tests.helpers import assert_causal, synthetic_bars, synthetic_pair


def test_synthetic_bars_valid_and_deterministic():
    for kind in ("random_walk", "trend", "ou"):
        a = synthetic_bars(500, kind, seed=3)
        b = synthetic_bars(500, kind, seed=3)
        pd.testing.assert_frame_equal(a, b)
        assert list(a.columns) == ["open", "high", "low", "close", "volume", "trades", "filled"]
        assert str(a.index.tz) == "UTC"
        assert (a["high"] >= a[["open", "close"]].max(axis=1)).all()
        assert (a["low"] <= a[["open", "close"]].min(axis=1)).all()
        assert (a[["open", "high", "low", "close"]] > 0).all().all()
        assert not a["filled"].any()
    assert not synthetic_bars(100, "ou", seed=1).equals(synthetic_bars(100, "ou", seed=2))


def test_ema_matches_pandas():
    x = pd.Series([1.0, 2.0, 4.0, 3.0, 5.0, 6.0])
    got = ema(x, 3)
    exp = x.ewm(span=3, adjust=False).mean()
    assert got.iloc[:2].isna().all()
    np.testing.assert_allclose(got.iloc[2:], exp.iloc[2:])


def test_atr_wilder_hand_example():
    bars = pd.DataFrame(
        {
            "high": [10.0, 11, 12, 11, 10],
            "low": [8.0, 9, 9, 8, 7],
            "close": [9.0, 10, 11, 9, 8],
        }
    )
    got = atr(bars, 2)
    assert np.isnan(got.iloc[0])
    np.testing.assert_allclose(got.iloc[1:], [2.0, 2.5, 2.75, 2.875])


def test_ewma_zscore_hand_and_zero_std():
    x = pd.Series([1.0, 2.0, 3.0, 5.0, 4.0])
    got = ewma_zscore(x, 3)
    m = x.ewm(span=3, min_periods=3, adjust=False).mean()
    s = x.ewm(span=3, min_periods=3, adjust=False).std()
    assert got.iloc[:2].isna().all()
    np.testing.assert_allclose(got.iloc[2:], ((x - m) / s).iloc[2:])
    const = ewma_zscore(pd.Series([2.0] * 8), 3)
    assert const.isna().all()


def test_realized_vol_and_corr():
    close = pd.Series([100.0, 101, 99, 102, 100, 103])
    rv = realized_vol(close, 3)
    lr = np.log(close).diff()
    assert rv.iloc[:3].isna().all()
    assert rv.iloc[3] == pytest.approx(lr.iloc[1:4].std())
    assert rolling_corr(close, close * 2, 4).dropna().round(12).eq(1.0).all()


def test_trend_strength_signed():
    up = synthetic_bars(600, "trend", seed=1, drift=0.003, sigma=0.0005)
    ts = trend_strength(up["close"], up, fast=10, slow=50, atr_n=14)
    assert ts.iloc[:49].isna().all()
    assert ts.iloc[-1] > 0
    down = up.copy()
    down["open"] = 1e4 / up["open"]
    down["close"] = 1e4 / up["close"]
    down["high"] = 1e4 / up["low"]
    down["low"] = 1e4 / up["high"]
    assert trend_strength(down["close"], down, fast=10, slow=50, atr_n=14).iloc[-1] < 0


def test_spread_scalar_and_series_beta():
    la = pd.Series([1.0, 2.0, 3.0])
    lb = pd.Series([1.0, 1.0, 2.0])
    np.testing.assert_allclose(spread(la, lb, 0.5), [0.5, 1.5, 2.0])
    np.testing.assert_allclose(spread(la, lb, pd.Series([1.0, 2.0, 1.0])), [0.0, 0.0, 1.0])


def test_half_life_ar1():
    rng = np.random.default_rng(0)
    n = 5000
    s = np.zeros(n)
    for i in range(1, n):
        s[i] = s[i - 1] * (1 - 0.1) + rng.normal()
    hl = half_life(pd.Series(s))
    assert hl == pytest.approx(np.log(2) / 0.10536, rel=0.10)


def test_half_life_random_walk_and_short():
    rw = pd.Series(np.cumsum(np.random.default_rng(1).normal(size=3000)))
    assert half_life(rw) > 200 or np.isinf(half_life(rw))
    assert half_life(pd.Series([1.0, 2.0, 3.0])) == np.inf
    assert half_life(pd.Series(np.arange(100.0))) == np.inf  # b >= 0


def test_all_features_causal():
    df = synthetic_bars(600, "random_walk", seed=4)
    other = synthetic_bars(600, "ou", seed=5)

    def with_other(fn):
        return lambda d: fn(d, other.loc[d.index])

    assert_causal(lambda d: ema(d["close"], 20), df)
    assert_causal(lambda d: atr(d, 14), df)
    assert_causal(lambda d: ewma_zscore(d["close"], 30), df)
    assert_causal(lambda d: realized_vol(d["close"], 20), df)
    assert_causal(lambda d: trend_strength(d["close"], d, 10, 50, 14), df)
    assert_causal(with_other(lambda d, o: rolling_corr(d["close"], o["close"], 40)), df)
    assert_causal(with_other(lambda d, o: rolling_beta(np.log(d["close"]), np.log(o["close"]), 40)), df)
    assert_causal(with_other(lambda d, o: spread(np.log(d["close"]), np.log(o["close"]), 0.8)), df)


def test_rolling_beta_recovers_true_beta():
    a, b = synthetic_pair(5000, "cointegrated", seed=7)
    beta = rolling_beta(np.log(a["close"]), np.log(b["close"]), 1000)
    assert beta.iloc[:999].isna().all()
    assert beta.dropna().mean() == pytest.approx(0.8, abs=0.05)


def test_synthetic_pair_kinds():
    a, b = synthetic_pair(500, "constant_spread", seed=1)
    sp = np.log(a["close"]) - np.log(b["close"])
    assert sp.std() < 1e-12
    a, b = synthetic_pair(500, "independent", seed=1)
    assert abs(np.log(a["close"]).diff().corr(np.log(b["close"]).diff())) < 0.5
    for f in synthetic_pair(100, "cointegrated", seed=2):
        assert (f["high"] >= f[["open", "close"]].max(axis=1)).all()
