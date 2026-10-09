"""Research V3 scores: premium (crowding), low volatility, shuffled null (spec §3, §4, §5)."""

import math

import numpy as np
import pandas as pd
import pytest

from luna.v2.xsec import XSecParams, daily_closes
from luna.v3.signals import premium_daily, score


def _with_premium(panel, value=0.01):
    return {a: df.assign(premium=value) for a, df in panel.items()}


def _stamps(df):
    return df.index[(df.index.hour == 23) & (df.index.minute == 45)]


def test_premium_daily_reads_the_decision_bar():
    idx = pd.date_range("2024-01-01", periods=96 * 3, freq="15min", tz="UTC")
    df = pd.DataFrame({"close": 1.0, "filled": False, "premium": np.arange(len(idx), dtype=float)}, index=idx)
    d = premium_daily({"BTC": df})
    assert list(d.index) == list(_stamps(df)) and d["BTC"].tolist() == [95.0, 191.0, 287.0]


def test_premium_daily_drops_filled_decision_bars(v2_panel):
    p = _with_premium(v2_panel)
    df = p["BTC"].copy()
    stamp = _stamps(df)[3]
    df.loc[stamp, "filled"] = True
    p["BTC"] = df
    d = premium_daily(p)
    assert math.isnan(d.loc[stamp, "BTC"]) and d["BTC"].dropna().eq(0.01).all()


def test_premium_daily_empty_without_premium_column(v2_panel):
    assert premium_daily(v2_panel).empty


def test_premium_score_is_the_negative_trailing_mean(v2_panel):
    p = _with_premium(v2_panel)
    dc, aux = daily_closes(p), premium_daily(p)
    s = score(dc, dc.index[60], XSecParams(14, 1, signal="premium"), aux)
    assert s["BTC"] == pytest.approx(-0.01)


def test_premium_score_ignores_data_after_t(v2_panel):
    p = _with_premium(v2_panel)
    dc, aux = daily_closes(p), premium_daily(p)
    t = dc.index[60]
    later = aux.copy()
    later.loc[later.index > t] = 99.0
    params = XSecParams(14, 1, signal="premium")
    pd.testing.assert_series_equal(score(dc, t, params, aux), score(dc, t, params, later))


def test_premium_score_nan_when_less_than_half_the_window_is_known(v2_panel):
    p = _with_premium(v2_panel)
    dc, aux = daily_closes(p), premium_daily(p)
    t = dc.index[60]
    aux.loc[(aux.index > t - pd.Timedelta(days=14)) & (aux.index <= t - pd.Timedelta(days=6)), "BTC"] = np.nan
    assert math.isnan(score(dc, t, XSecParams(14, 1, signal="premium"), aux)["BTC"])


def test_lowvol_score_is_negative_std_of_window_log_returns(v2_panel):
    dc = daily_closes(v2_panel)
    t = dc.index[60]
    win = dc.loc[(dc.index > t - pd.Timedelta(days=31)) & (dc.index <= t)]
    rets = np.log(win).diff()
    rets = rets.loc[rets.index > t - pd.Timedelta(days=30)]
    s = score(dc, t, XSecParams(30, 1, signal="lowvol"), None)
    assert s["ETH"] == pytest.approx(-rets["ETH"].std(ddof=1))


def test_lowvol_score_ignores_data_after_t(v2_panel):
    dc = daily_closes(v2_panel)
    t = dc.index[60]
    bumped = dc.copy()
    bumped.loc[bumped.index > t] *= 3
    params = XSecParams(30, 1, signal="lowvol")
    pd.testing.assert_series_equal(score(dc, t, params, None), score(bumped, t, params, None))


def test_shuffle_is_deterministic():
    idx = pd.date_range("2024-01-01 23:45", periods=60, freq="1D", tz="UTC")
    dc = pd.DataFrame({a: np.linspace(1, 2, 60) for a in ("BTC", "ETH", "AAA")}, index=idx)
    t = idx[50]
    a = score(dc, t, XSecParams(30, 1, signal="shuffle1"), None)
    assert a.equals(score(dc, t, XSecParams(30, 1, signal="shuffle1"), None))


def test_shuffle_depends_on_seed_and_stamp():
    idx = pd.date_range("2024-01-01 23:45", periods=60, freq="1D", tz="UTC")
    dc = pd.DataFrame({a: np.linspace(1, 2, 60) for a in ("BTC", "ETH", "AAA", "BBB")}, index=idx)
    a = score(dc, idx[50], XSecParams(30, 1, signal="shuffle1"), None)
    assert not a.equals(score(dc, idx[50], XSecParams(30, 1, signal="shuffle2"), None))
    assert not a.equals(score(dc, idx[51], XSecParams(30, 1, signal="shuffle1"), None))


def test_shuffle_is_nan_where_momentum_is_undefined():
    idx = pd.date_range("2024-01-01 23:45", periods=60, freq="1D", tz="UTC")
    dc = pd.DataFrame({"BTC": np.linspace(1, 2, 60), "ETH": np.nan}, index=idx)
    s = score(dc, idx[50], XSecParams(30, 1, signal="shuffle1"), None)
    assert math.isfinite(s["BTC"]) and math.isnan(s["ETH"])


def test_unknown_signal_raises(v2_panel):
    dc = daily_closes(v2_panel)
    with pytest.raises(ValueError):
        score(dc, dc.index[60], XSecParams(30, 1, signal="nope"), None)


def test_shuffle_keeps_the_base_signal_eligibility():
    idx = pd.date_range("2024-01-01 23:45", periods=60, freq="1D", tz="UTC")
    dc = pd.DataFrame({a: np.linspace(1, 2, 60) for a in ("BTC", "ETH", "AAA")}, index=idx)
    aux = pd.DataFrame({"BTC": 0.01, "ETH": 0.02}, index=idx)  # AAA has no premium (no other venue)
    s = score(dc, idx[50], XSecParams(14, 1, signal="shuffle1:premium"), aux)
    assert math.isfinite(s["BTC"]) and math.isfinite(s["ETH"]) and math.isnan(s["AAA"])


def test_shuffle_draws_do_not_depend_on_panel_composition():
    idx = pd.date_range("2024-01-01 23:45", periods=60, freq="1D", tz="UTC")
    full = pd.DataFrame({a: np.linspace(1, 2, 60) for a in ("BTC", "ETH", "AAA")}, index=idx)
    p = XSecParams(30, 1, signal="shuffle2:lowvol")
    assert score(full, idx[50], p, None)["ETH"] == score(full[["BTC", "ETH"]], idx[50], p, None)["ETH"]
