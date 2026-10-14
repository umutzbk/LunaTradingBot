import numpy as np
import pandas as pd
import pytest

from luna.v7.beta import beta_hat, hourly_close_from_15m, hourly_known_at_from_15m
from luna.v7.timeutil import MIN, H, to_ns

T = to_ns("2024-06-01T00:00:30Z")
LAST = ((T - MIN) // H) * H


def kn(s):
    return pd.Series(s.index.to_numpy(), index=s.index)


def series(rets, last=LAST):
    n = len(rets)
    idx = last - np.arange(n, -1, -1) * H
    p = 100 * np.exp(np.concatenate([[0.0], np.cumsum(rets)]))
    return pd.Series(p, index=idx)


def test_beta_requires_500_obs():
    rng = np.random.default_rng(0)
    b = rng.normal(0, 0.01, 720)
    assert bh(series(b * 2), series(b), T) is not None
    assert bh(series(b[-499:] * 2), series(b[-499:]), T) is None
    assert bh(series(b[-500:] * 2), series(b[-500:]), T) is not None


def test_beta_shrink_and_clip():
    rng = np.random.default_rng(1)
    b = rng.normal(0, 0.01, 720)
    assert abs(bh(series(b * 2), series(b), T) - (0.7 * 2 + 0.3)) < 1e-9
    assert bh(series(b * 10), series(b), T) == 3.0
    assert bh(series(-b * 2), series(b), T) == 0.0


def test_beta_window_ends_before_t_minus_1min():
    rng = np.random.default_rng(2)
    b = rng.normal(0, 0.01, 720)
    a = series(b * 2)
    t_on = LAST + H  # t on the hour: hour t is not yet usable (needs <= t - 1 min)
    a2 = a.copy()
    a2.loc[t_on] = 1e9
    btc = series(b).copy()
    btc.loc[t_on] = 1.0
    assert abs(bh(a2, btc, t_on) - bh(a, series(b), t_on)) < 1e-9


def test_hourly_close_from_15m():
    t = to_ns("2024-06-01T00:00:00Z")
    df = pd.DataFrame({"open_ts": [t, t + 15 * MIN, t + 45 * MIN], "c": [1.0, 2.0, 3.0]})
    s = hourly_close_from_15m(df)
    assert list(s.index) == [t + H] and s.iloc[0] == 3.0


def bh(a, b, t):
    """beta_hat with every price known at its own hour (nothing late)."""
    return beta_hat(a, b, t, kn(a), kn(b))


def test_beta_requires_known_at():
    a = series(np.zeros(10))
    with pytest.raises(TypeError):
        beta_hat(a, a, T)
    with pytest.raises(TypeError):
        beta_hat(a, a, T, kn(a))
    with pytest.raises(ValueError):
        hourly_known_at_from_15m(pd.DataFrame({"open_ts": [0], "c": [1.0]}))


def _late_case():
    rng = np.random.default_rng(3)
    b = rng.normal(0, 0.01, 720)
    a, btc = series(b * 2 + rng.normal(0, 0.003, 720)), series(b)
    return a, btc


def test_beta_drops_late_known_at_bars():
    a, btc = _late_case()
    for late_pos in (-1, -300):
        for side in ("asset", "btc", "both"):
            ka, kb = kn(a), kn(btc)
            tgt = a.copy(), btc.copy()
            if side in ("asset", "both"):
                ka.iloc[late_pos] = T  # received after t - 1 min
                tgt[0].iloc[late_pos] *= 5
            if side in ("btc", "both"):
                kb.iloc[late_pos] = T
                tgt[1].iloc[late_pos] *= 5
            got = beta_hat(tgt[0], tgt[1], T, ka, kb)
            # reference: the late price is absent
            ra = a.drop(a.index[late_pos]) if side in ("asset", "both") else a
            rb = btc.drop(btc.index[late_pos]) if side in ("btc", "both") else btc
            assert abs(got - bh(ra, rb, T)) < 1e-12
            assert got != bh(*tgt, T)  # the leak would change the result


def test_nan_known_at_raises_and_none_args_raise():
    t = to_ns("2024-06-01T00:00:00Z")
    df = pd.DataFrame({"open_ts": [t + 45 * MIN], "c": [1.0], "known_at": [np.nan]})
    with pytest.raises(ValueError):
        hourly_known_at_from_15m(df)
    a = series(np.zeros(10))
    with pytest.raises(TypeError):
        beta_hat(a, a, T, None, None)
    with pytest.raises(TypeError):
        beta_hat(a, a, T, kn(a), None)


@pytest.mark.parametrize("bad", [1e30, -1e30])
def test_out_of_range_known_at_raises(bad):
    t = to_ns("2024-06-01T00:00:00Z")
    # the out-of-range value is on a non-hour-closing row: every row is validated, intentionally
    df = pd.DataFrame({"open_ts": [t, t + 45 * MIN], "c": [1.0, 1.0], "known_at": [bad, float(t + 60 * MIN)]})
    with pytest.raises(ValueError):
        hourly_known_at_from_15m(df)
    a = series(np.zeros(10))
    ka = kn(a).astype("float64")
    ka.iloc[-1] = bad
    with pytest.raises(ValueError):
        beta_hat(a, a, T, ka, kn(a))
