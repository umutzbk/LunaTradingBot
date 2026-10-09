"""Research V3 inputs: perp-spot premium from the other venue, under the V2 cutoff (spec §3A, §5)."""

import math

import pandas as pd
import pytest

from luna.report.walkforward import Inputs
from luna.v2.data import V2_DATA_END, HoldoutLeak
from luna.v3.data import add_premium, with_premium


def test_perp_account_premium_is_perp_over_spot(v2_panel):
    perp = {"BTC": v2_panel["BTC"]}
    spot = {"BTC": v2_panel["BTC"].assign(close=v2_panel["BTC"]["close"] * 0.99)}
    p = add_premium(perp, spot, "perp")["BTC"]["premium"]
    assert p.iloc[10] == pytest.approx(1 / 0.99 - 1)


def test_spot_account_premium_is_also_perp_over_spot(v2_panel):
    perp = {"BTC": v2_panel["BTC"]}
    spot = {"BTC": v2_panel["BTC"].assign(close=v2_panel["BTC"]["close"] * 0.99)}
    s = add_premium(spot, perp, "spot")["BTC"]["premium"]
    assert s.iloc[10] == pytest.approx(1 / 0.99 - 1)


def test_premium_nan_where_other_bar_is_filled_or_missing(v2_panel):
    other = v2_panel["BTC"].copy()
    other.iloc[100, other.columns.get_loc("filled")] = True
    other = other.drop(other.index[200])
    out = add_premium({"BTC": v2_panel["BTC"]}, {"BTC": other}, "perp")["BTC"]
    assert math.isnan(out["premium"].iloc[100]) and math.isnan(out["premium"].iloc[200])
    assert math.isfinite(out["premium"].iloc[300])


def test_premium_nan_where_own_bar_is_filled(v2_panel):
    own = v2_panel["BTC"].copy()
    own.iloc[50, own.columns.get_loc("filled")] = True
    out = add_premium({"BTC": own}, {"BTC": v2_panel["BTC"]}, "perp")["BTC"]
    assert math.isnan(out["premium"].iloc[50])


def test_asset_without_other_venue_gets_nan_premium(v2_panel):
    out = add_premium({"AAA": v2_panel["AAA"]}, {}, "perp")
    assert out["AAA"]["premium"].isna().all()


def test_other_columns_are_untouched(v2_panel):
    out = add_premium({"BTC": v2_panel["BTC"]}, {"BTC": v2_panel["BTC"]}, "perp")["BTC"]
    pd.testing.assert_frame_equal(out.drop(columns="premium"), v2_panel["BTC"])


def test_other_venue_bar_after_cutoff_is_a_leak(v2_panel):
    late = v2_panel["BTC"].copy()
    late.index = pd.date_range(V2_DATA_END - pd.Timedelta("1D"), periods=len(late), freq="15min")
    inp = Inputs({"BTC": v2_panel["BTC"]}, {}, None, {}, [])
    with pytest.raises(HoldoutLeak):
        with_premium(inp, {"BTC": late}, "perp")
