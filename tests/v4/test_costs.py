"""V4 cost rows (spec §5.3, §5.4): SC-2 fees, spread quoted FULL and paid half per side."""

import pandas as pd
import pytest

from luna.config import load_settings
from luna.v4 import costs as c


def test_rows_are_exactly_the_four_mandatory_sensitivity_rows():
    rows = c.cost_rows()
    assert list(rows) == ["PRIMARY", "S-SPREAD10", "S-COST2X", "S-MAKER"]
    assert [r.counts_for_go for r in rows.values()] == [True, False, True, False]
    assert rows["S-SPREAD10"].historical_only and not rows["PRIMARY"].historical_only


def test_fees_are_sc2_from_settings():
    rows = c.cost_rows(load_settings().costs)
    p, m, x2 = rows["PRIMARY"], rows["S-MAKER"], rows["S-COST2X"]
    assert (p.fee("spot"), p.fee("perp")) == (0.008, 0.0005)
    assert (m.fee("spot"), m.fee("perp")) == (0.004, 0.0002)
    assert (x2.fee("spot"), x2.fee("perp")) == (0.016, 0.001)


def test_historical_spot_spread_is_full_quoted_and_half_is_paid():
    rows = c.cost_rows()
    assert rows["PRIMARY"].half_spread("spot", None) == pytest.approx(0.0010)  # 20 bps full
    assert rows["S-SPREAD10"].half_spread("spot", None) == pytest.approx(0.0005)
    assert rows["S-COST2X"].half_spread("spot", None) == pytest.approx(0.0020)
    assert rows["S-MAKER"].half_spread("spot", None) == pytest.approx(0.0010)


def test_observed_half_spread_is_used_for_perp_and_forward_and_doubled_in_cost2x():
    rows = c.cost_rows()
    assert rows["PRIMARY"].half_spread("perp", 0.0004) == 0.0004
    assert rows["S-COST2X"].half_spread("perp", 0.0004) == 0.0008
    assert rows["PRIMARY"].half_spread("spot", 0.0003, forward=True) == 0.0003


def test_perp_hist_spread_is_hourly_mean_or_counted_10bps_fallback():
    idx = pd.date_range("2026-03-01T00:00Z", periods=4, freq="15min")
    sp = pd.DataFrame({"spread_full": [0.001, 0.003, 0.002, 0.002]}, index=idx)
    half, fallback = c.perp_half_spread_hist(sp, pd.Timestamp("2026-03-01T00:15Z"))
    assert half == pytest.approx(0.002 / 2) and not fallback
    half, fallback = c.perp_half_spread_hist(sp, pd.Timestamp("2026-03-01T05:00Z"))
    assert half == pytest.approx(0.0010 / 2) and fallback
    assert c.perp_half_spread_hist(None, pd.Timestamp("2026-03-01T05:00Z")) == (0.0005, True)


def test_trade_cost_definition():
    fee, spread = c.trade_cost(qty=-2.0, mid=50.0, fee_rate=0.001, half_spread=0.0005)
    assert fee == pytest.approx(0.1) and spread == pytest.approx(0.05)
