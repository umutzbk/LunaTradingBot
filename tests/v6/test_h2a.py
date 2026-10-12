"""H2a (S-H2-1): pooled PIT OLS Y(s,h) = a + b X(s), expanding over samples with s + h <= t."""

import numpy as np
import pandas as pd
import pytest

from luna.v6 import forecast as fc
from luna.v6.data import FundingPanel
from tests.v6.fixtures import T0, ar_funding

DAY = pd.Timedelta("1D")


def test_s0_is_the_first_daily_date_with_x_for_ten_assets():
    p = ar_funding()
    m = fc.H2a(p, 168)
    # 648 ended periods are first available at T0 + 648h = 27 days -> first 00:00 with coverage
    assert m.s0() == T0 + 27 * DAY


def test_warmup_until_sixty_sample_dates():
    p = ar_funding()
    m = fc.H2a(p, 168)
    s0 = m.s0()
    # samples need s + 168h <= t: the 60th sample date is s0 + 59 d, usable at s0 + 66 d
    assert m.fit(s0 + 65 * DAY) is None
    f = m.fit(s0 + 66 * DAY)
    assert f is not None and f.n_dates == 60


def test_fit_uses_only_samples_whose_label_ended_and_never_future_data():
    p = ar_funding()
    m = fc.H2a(p, 168)
    t = m.s0() + 100 * DAY
    base = m.fit(t)
    rel = p.rel.copy()
    rel.loc[t:, :] = 0.5  # everything from t on (incl. the running period) is changed
    other = fc.H2a(FundingPanel(rel), 168).fit(t)
    assert (other.a, other.b, other.n_dates) == (base.a, base.b, base.n_dates)
    assert other.max_label_end <= t and base.max_sample_date + pd.Timedelta(hours=168) <= t
    rel2 = p.rel.copy()
    rel2.loc[t - pd.Timedelta("1h"), :] = 0.5  # a period that ended at t is a valid input
    assert fc.H2a(FundingPanel(rel2), 168).fit(t).a != base.a


def test_720h_and_168h_are_separate_models():
    p = ar_funding(hours=24 * 260)
    t = T0 + 200 * DAY
    a, b = fc.H2a(p, 168).fit(t), fc.H2a(p, 720).fit(t)
    assert a.h == 168 and b.h == 720 and (a.a, a.b) != (b.a, b.b)
    assert b.max_sample_date + pd.Timedelta(hours=720) <= t


def test_winsor_cutoffs_come_from_the_training_sample_and_prediction_is_clipped_to_them():
    p = ar_funding()
    m = fc.H2a(p, 168)
    t = m.s0() + 100 * DAY
    f = m.fit(t)
    x = pd.Series({"A00": 1.0, "A01": -1.0, "A02": f.lo_x + (f.hi_x - f.lo_x) / 2})
    y = f.predict(x)
    assert y["A00"] == pytest.approx(f.a + f.b * f.hi_x) and y["A01"] == pytest.approx(f.a + f.b * f.lo_x)
    # an extreme value at prediction time (after the last sample) does not move the fit
    rel = p.rel.copy()
    rel.loc[t - pd.Timedelta("100h"): t - pd.Timedelta("1h"), "A00"] = 1.0
    f2 = fc.H2a(FundingPanel(rel), 168).fit(t)
    assert (f2.lo_x, f2.hi_x) == pytest.approx((f.lo_x, f.hi_x))


def test_ols_and_winsor_mechanics():
    rng = np.random.default_rng(0)
    x = rng.normal(size=500)
    y = 0.3 + 0.5 * x
    a, b, lo_x, hi_x, lo_y, hi_y = fc.winsor_ols(x, y)
    assert (lo_x, hi_x) == pytest.approx(tuple(np.quantile(x, [0.01, 0.99])))
    xc = np.clip(x, lo_x, hi_x)
    yc = np.clip(y, lo_y, hi_y)
    bb, aa = np.polyfit(xc, yc, 1)
    assert (a, b) == pytest.approx((aa, bb))


def test_forecast_is_missing_for_an_asset_without_fl30():
    p = ar_funding()
    rel = p.rel.copy()
    t = fc.H2a(p, 168).s0() + 100 * DAY
    rel.loc[t - pd.Timedelta("200h"):, "A03"] = np.nan  # 520 of 720 -> < 648
    out = fc.H2a(FundingPanel(rel), 168).forecast(t)
    assert np.isnan(out["A03"]) and out.drop("A03").notna().all()
    with pytest.raises(fc.ModelWarmup):
        fc.H2a(p, 168).forecast(T0 + 30 * DAY)


def test_fit_excludes_samples_whose_label_is_below_ninety_percent_known():
    p = ar_funding()
    m = fc.H2a(p, 168)
    t = m.s0() + 100 * DAY
    base = m.fit(t)
    rel = p.rel.copy()
    s = m.s0() + 10 * DAY
    rel.loc[s: s + pd.Timedelta(hours=20), "A00"] = np.nan  # 147/168 = 87.5 % known -> sample dropped
    f = fc.H2a(FundingPanel(rel), 168).fit(t)
    assert f.n_samples < base.n_samples
