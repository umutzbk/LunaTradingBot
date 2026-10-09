"""V4 signals and eligibility (spec §3, §8). Every value at decision time t uses only bars with
open_ts <= t - 15m and funding periods that have ended (start + 1h <= t). Look-ahead tests corrupt
everything after t and require identical output."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from luna.v4 import signals as s
from tests.v4.synth import bars, hourly_funding

T = pd.Timestamp("2026-03-10T00:00:00Z")


def _corrupt_after(df: pd.DataFrame, t: pd.Timestamp, cols, value=1e9) -> pd.DataFrame:
    out = df.copy()
    out.loc[out.index >= t, cols] = value
    return out


# --- funding signals ---------------------------------------------------------------------------


def test_f7_is_mean_relative_rate_of_last_168_ended_periods_annualized():
    rates = hourly_funding(T - pd.Timedelta(hours=400), 400 + 10, rel=2e-5)
    rates.loc[rates.index < T - pd.Timedelta(hours=168), "funding_rel"] = 1.0  # outside the window
    f7, n = s.f7(rates, T)
    assert n == 168
    assert f7 == pytest.approx(2e-5 * 8760)


def test_f7_ignores_the_running_period_and_everything_after_t():
    rates = hourly_funding(T - pd.Timedelta(hours=200), 300, rel=1e-5)
    base = s.f7(rates, T)
    # the period starting at t - 1h ends exactly at t and is known; the one starting at t is not
    bad = rates.copy()
    bad.loc[bad.index >= T, "funding_rel"] = 99.0
    assert s.f7(bad, T) == base
    worse = rates.copy()
    worse.loc[T - pd.Timedelta(hours=1), "funding_rel"] = 99.0
    assert s.f7(worse, T) != base


def test_fl_and_coverage_count_missing_hours():
    rates = hourly_funding(T - pd.Timedelta(days=7), 7 * 24, rel=3e-5).drop(
        pd.date_range(T - pd.Timedelta(hours=30), periods=20, freq="1h"))
    fl, n = s.fl(rates, T, 7)
    assert n == 7 * 24 - 20
    assert fl == pytest.approx(3e-5 * 8760)
    assert np.isnan(s.fl(rates.iloc[:0], T, 7)[0])


# --- volume / listing ---------------------------------------------------------------------------


def test_daily_volume_median_needs_27_of_30_days_and_uses_completed_days_only():
    b = bars(T - pd.Timedelta(days=30), 30 * 96, close=100.0, volume=200.0)  # 96 bars * 200 * 100
    assert s.hist_volume_median(b, T) == pytest.approx(96 * 200 * 100)
    # 4 days without any bar -> only 26 values -> not computable
    gap = b.drop(b.index[(b.index >= T - pd.Timedelta(days=10)) & (b.index < T - pd.Timedelta(days=6))])
    assert np.isnan(s.hist_volume_median(gap, T))
    # 3 missing days still leave 27 values
    gap3 = b.drop(b.index[(b.index >= T - pd.Timedelta(days=10)) & (b.index < T - pd.Timedelta(days=7))])
    assert s.hist_volume_median(gap3, T) == pytest.approx(96 * 200 * 100)
    # filled (synthetic) bars carry no volume and do not make a day present
    filled = b.copy()
    filled.loc[filled.index >= T - pd.Timedelta(days=4), "filled"] = True
    assert np.isnan(s.hist_volume_median(filled, T))


def test_daily_volume_median_ignores_bars_after_t():
    b = bars(T - pd.Timedelta(days=31), 32 * 96, close=100.0, volume=200.0)
    assert s.hist_volume_median(b, T) == s.hist_volume_median(_corrupt_after(b, T, ["volume", "close"]), T)


def test_forward_volume_median_from_daily_values_before_t():
    days = pd.date_range(T - pd.Timedelta(days=30), periods=31, freq="D")
    vals = pd.Series(5e6, index=days)
    vals[T] = 1e12  # the snapshot after t's own midnight is not known at t
    assert s.forward_volume_median(vals, T) == 5e6
    assert np.isnan(s.forward_volume_median(vals.iloc[5:], T))


# --- basis -------------------------------------------------------------------------------------


def _basis_panel(n_days=40, prem=0.001):
    start = T - pd.Timedelta(days=n_days)
    spot = bars(start, n_days * 96 + 96, close=100.0)
    perp = bars(start, n_days * 96 + 96, close=100.0 * (1 + prem))
    return perp, spot


def test_venue_basis_excludes_filled_bars_and_missing_legs():
    perp, spot = _basis_panel(2)
    spot.iloc[3, spot.columns.get_loc("filled")] = True
    perp = perp.drop(perp.index[5])
    b = s.venue_basis(perp, spot)
    assert np.isnan(b.iloc[3]) and b.index[5] not in b.dropna().index
    assert b.dropna().iloc[0] == pytest.approx(0.001)


def test_b24_needs_48_valid_bars_in_the_last_24h():
    perp, spot = _basis_panel(3)
    b = s.venue_basis(perp, spot)
    assert s.b24_hist(b, T) == pytest.approx(0.001)
    sparse = b.copy()
    lo, hi = T - pd.Timedelta(hours=24), T - pd.Timedelta(hours=11, minutes=45)
    sparse[(sparse.index >= lo) & (sparse.index < hi)] = np.nan  # 49 bars removed -> 47 left
    assert np.isnan(s.b24_hist(sparse, T))


def test_b24_and_b30med_ignore_data_after_t():
    perp, spot = _basis_panel(40)
    b = s.venue_basis(perp, spot)
    bad = b.copy()
    bad[bad.index > T - pd.Timedelta("15min")] = 5.0
    assert s.b24_hist(bad, T) == s.b24_hist(b, T)
    assert s.b30med(lambda t: s.b24_hist(bad, t), T) == s.b30med(lambda t: s.b24_hist(b, t), T)


def test_b30med_needs_27_daily_values():
    vals = {T - pd.Timedelta(days=k): (0.0 if k < 27 else np.nan) for k in range(30)}
    assert s.b30med(lambda t: vals[t], T) == 0.0
    vals[T - pd.Timedelta(days=0)] = np.nan
    assert np.isnan(s.b30med(lambda t: vals[t], T))


# --- the whole table: eligibility and look-ahead ------------------------------------------------


def _hist_inputs(prem=0.001, rel=2e-5, volume=1e4, days=60):
    from luna.v4.data import HistoricalInputs

    start = T - pd.Timedelta(days=days)
    n = (days + 3) * 96
    perp = {a: bars(start, n, close=100.0 * (1 + prem), volume=volume) for a in ("AAA", "BBB")}
    spot = {a: bars(start, n, close=100.0, volume=volume) for a in ("AAA", "BBB")}
    fund = {a: hourly_funding(start, (days + 3) * 24, rel=rel) for a in ("AAA", "BBB")}
    empty = {a: pd.DataFrame() for a in ("AAA", "BBB")}
    return HistoricalInputs(perp=perp, spot=spot, mark=empty, index=empty, funding=fund, spreads=empty)


def test_hist_table_eligibility_flags():
    inp = _hist_inputs(volume=1e4)  # 96 * 1e4 * 100 = 9.6e7 per day >= $1M
    tab = s.hist_signal_table(inp, [T])
    row = tab.loc[(T, "AAA")]
    assert row["elig_h1"] and row["elig_h2_7"] and row["elig_h2_30"]
    assert row["F7"] == pytest.approx(2e-5 * 8760)
    low = s.hist_signal_table(_hist_inputs(volume=1.0), [T]).loc[(T, "AAA")]
    assert not low["elig_h1"] and not low["elig_h2_7"]


def test_h2_needs_30_days_of_listing():
    inp = _hist_inputs(days=29)
    row = s.hist_signal_table(inp, [T]).loc[(T, "AAA")]
    assert not row["elig_h2_7"]


def test_hist_table_is_unchanged_when_everything_after_t_is_corrupted():
    inp = _hist_inputs()
    base = s.hist_signal_table(inp, [T])
    for a in ("AAA", "BBB"):
        inp.perp[a] = _corrupt_after(inp.perp[a], T, ["close", "volume", "high", "low"])
        inp.spot[a] = _corrupt_after(inp.spot[a], T, ["close", "volume"], value=3.0)
        inp.funding[a] = _corrupt_after(inp.funding[a], T, ["funding_rel", "funding_abs"], value=-7.0)
    pd.testing.assert_frame_equal(s.hist_signal_table(inp, [T]), base)


def test_forward_table_uses_previous_days_aggregates_only():
    from luna.v4.data import ForwardDaily

    days = pd.date_range(T - pd.Timedelta(days=40), periods=41, freq="D")
    idx = pd.MultiIndex.from_product([days, ["AAA"]], names=["day", "asset"])
    ex = pd.DataFrame({"perp_vol_quote": 5e6, "spot_vol_usd": 5e6}, index=idx)
    daily = pd.DataFrame({"basis_median": 0.002, "basis_n": 1440}, index=idx)
    fd = ForwardDaily(exec=ex, daily=daily, quality=pd.DataFrame(),
                      listing={"AAA": T - pd.Timedelta(days=100)})
    fund = {"AAA": hourly_funding(T - pd.Timedelta(days=40), 41 * 24, rel=1e-5)}
    base = s.forward_signal_table(fd, fund, [T])
    row = base.loc[(T, "AAA")]
    assert row["elig_h1"] and row["B24"] == pytest.approx(0.002) and row["B30med"] == pytest.approx(0.002)
    ex2, daily2 = ex.copy(), daily.copy()
    ex2.loc[(T, "AAA"), :] = 0.0
    daily2.loc[(T, "AAA"), :] = [9.0, 1440]
    fund2 = {"AAA": _corrupt_after(fund["AAA"], T, ["funding_rel"], 5.0)}
    fd2 = ForwardDaily(exec=ex2, daily=daily2, quality=pd.DataFrame(), listing=fd.listing)
    pd.testing.assert_frame_equal(s.forward_signal_table(fd2, fund2, [T]), base)
    # fewer than 720 valid snapshots the previous day -> no B24
    daily3 = daily.copy()
    daily3.loc[(T - pd.Timedelta(days=1), "AAA"), "basis_n"] = 719
    fd3 = ForwardDaily(exec=ex, daily=daily3, quality=pd.DataFrame(), listing=fd.listing)
    assert np.isnan(s.forward_signal_table(fd3, fund, [T]).loc[(T, "AAA"), "B24"])
