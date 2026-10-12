"""PIT funding panel (S-DATA-1, S-H2-1): a period is visible at t only when it ended (p + 1h <= t)
and, in forward, when it was received by t; labels exist only once their whole window ended."""

import numpy as np
import pandas as pd
import pytest

from luna.v6.data import FundingPanel

T0 = pd.Timestamp("2025-10-06T00:00Z")


def panel(hours=2000, assets=("A", "B"), value=1e-5):
    idx = pd.date_range(T0, periods=hours, freq="h")
    return FundingPanel(pd.DataFrame({a: value * (i + 1) for i, a in enumerate(assets)}, index=idx))


def test_fl_uses_only_ended_periods_and_coverage():
    p = panel()
    t = T0 + pd.Timedelta(hours=720)
    fl = p.fl(t, 720, 648)
    assert fl["A"] == pytest.approx(1e-5) and fl["B"] == pytest.approx(2e-5)
    # one hour earlier only 719 periods have ended -> still >= 648
    assert p.fl(t - pd.Timedelta("1h"), 720, 648).notna().all()
    assert p.fl(T0 + pd.Timedelta(hours=647), 720, 648).isna().all()  # 647 ended periods < 648


def test_fl_never_sees_the_period_that_is_still_running():
    p = panel()
    t = T0 + pd.Timedelta(hours=800)
    base = p.fl(t, 720, 648)
    rel = p.rel.copy()
    rel.loc[t - pd.Timedelta("1h") + pd.Timedelta("1h"):, :] = 9.0  # period starting at t and later
    assert FundingPanel(rel).fl(t, 720, 648).equals(base)
    rel2 = p.rel.copy()
    rel2.loc[t - pd.Timedelta("1h"), "A"] = 9.0  # the last ended period counts
    assert FundingPanel(rel2).fl(t, 720, 648)["A"] != base["A"]


def test_late_arrival_is_invisible_until_received():
    p = panel()
    t = T0 + pd.Timedelta(hours=900)
    known = pd.DataFrame({c: p.rel.index + pd.Timedelta("1h") for c in p.rel.columns}, index=p.rel.index)
    known.loc[t - pd.Timedelta("2h"):, "A"] = t + pd.Timedelta("5h")  # received late
    rel = p.rel.copy()
    rel.loc[t - pd.Timedelta("2h"):, "A"] = 1.0
    late = FundingPanel(rel, known_at=known)
    assert late.fl(t, 720, 648)["A"] == pytest.approx(1e-5)
    assert late.fl(t + pd.Timedelta("5h"), 720, 648)["A"] > 1e-5


def test_label_exists_only_after_its_window_and_reports_known_share():
    p = panel()
    s = T0 + pd.Timedelta(hours=800)
    with pytest.raises(ValueError):
        p.label(s, 168, s + pd.Timedelta(hours=167))
    y, share = p.label(s, 168, s + pd.Timedelta(hours=168))
    assert y["A"] == pytest.approx(1e-5) and (share == 1.0).all()
    rel = p.rel.copy()
    rel.loc[s: s + pd.Timedelta(hours=99), "A"] = np.nan
    y2, sh2 = FundingPanel(rel).label(s, 168, s + pd.Timedelta(hours=168))
    assert sh2["A"] == pytest.approx(68 / 168) and y2["A"] == pytest.approx(1e-5)


def test_index_must_be_a_tz_aware_hourly_grid():
    idx = pd.date_range(T0, periods=10, freq="30min")
    with pytest.raises(ValueError):
        FundingPanel(pd.DataFrame({"A": 1.0}, index=idx))
    with pytest.raises(ValueError):
        FundingPanel(pd.DataFrame({"A": [1.0, 2.0]}, index=pd.DatetimeIndex(["2025-01-01", "2025-01-01 01:00"])))


def test_abs_rate_is_nan_until_the_period_has_ended_even_if_known_earlier():
    hidx = pd.date_range("2026-01-01", periods=4, freq="h", tz="UTC")
    rel = pd.DataFrame({"ETH": 1e-5}, index=hidx)
    known = pd.DataFrame({"ETH": hidx + pd.Timedelta("30min")}, index=hidx)  # 'received' mid-period
    p = FundingPanel(rel, abs_=rel * 100, known_at=known)
    assert np.isnan(p.abs_rate("ETH", hidx[1], hidx[1] + pd.Timedelta("45min")))
    assert p.abs_rate("ETH", hidx[1], hidx[1] + pd.Timedelta("1h")) == pytest.approx(1e-3)
