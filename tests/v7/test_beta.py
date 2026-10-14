import numpy as np
import pandas as pd

from luna.v7.beta import beta_hat, hourly_close_from_15m
from luna.v7.timeutil import MIN, H, to_ns

T = to_ns("2024-06-01T00:00:30Z")
LAST = ((T - MIN) // H) * H


def series(rets, last=LAST):
    n = len(rets)
    idx = last - np.arange(n, -1, -1) * H
    p = 100 * np.exp(np.concatenate([[0.0], np.cumsum(rets)]))
    return pd.Series(p, index=idx)


def test_beta_requires_500_obs():
    rng = np.random.default_rng(0)
    b = rng.normal(0, 0.01, 720)
    assert beta_hat(series(b * 2), series(b), T) is not None
    assert beta_hat(series(b[-499:] * 2), series(b[-499:]), T) is None
    assert beta_hat(series(b[-500:] * 2), series(b[-500:]), T) is not None


def test_beta_shrink_and_clip():
    rng = np.random.default_rng(1)
    b = rng.normal(0, 0.01, 720)
    assert abs(beta_hat(series(b * 2), series(b), T) - (0.7 * 2 + 0.3)) < 1e-9
    assert beta_hat(series(b * 10), series(b), T) == 3.0
    assert beta_hat(series(-b * 2), series(b), T) == 0.0


def test_beta_window_ends_before_t_minus_1min():
    rng = np.random.default_rng(2)
    b = rng.normal(0, 0.01, 720)
    a = series(b * 2)
    t_on = LAST + H  # t on the hour: hour t is not yet usable (needs <= t - 1 min)
    a2 = a.copy()
    a2.loc[t_on] = 1e9
    btc = series(b).copy()
    btc.loc[t_on] = 1.0
    assert abs(beta_hat(a2, btc, t_on) - beta_hat(a, series(b), t_on)) < 1e-9


def test_hourly_close_from_15m():
    t = to_ns("2024-06-01T00:00:00Z")
    df = pd.DataFrame({"open_ts": [t, t + 15 * MIN, t + 45 * MIN], "c": [1.0, 2.0, 3.0]})
    s = hourly_close_from_15m(df)
    assert list(s.index) == [t + H] and s.iloc[0] == 3.0


def test_beta_drops_late_known_at_bars():
    rng = np.random.default_rng(3)
    b = rng.normal(0, 0.01, 720)
    a, btc = series(b * 2), series(b)
    ka = pd.Series(a.index.to_numpy(), index=a.index)
    ok = beta_hat(a, btc, T, ka, ka)
    late = ka.copy()
    late.iloc[-1] = T  # last hourly close received after t - 1 min
    a_bad = a.copy()
    a_bad.iloc[-1] = a_bad.iloc[-1] * 5  # would change beta if used
    got = beta_hat(a_bad, btc, T, late, ka)
    assert ok is not None and got is not None
    assert beta_hat(a_bad, btc, T) != beta_hat(a, btc, T)  # without known_at it leaks
    assert abs(got - beta_hat(a.iloc[:-1], btc, T)) < 1e-12
