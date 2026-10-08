import numpy as np
import pandas as pd

from luna.v2.xsec import XSecParams, daily_closes, eligible, raw_targets, rebalance_stamps


def _dc(rows: dict[str, list[float]], start="2024-01-01 23:45"):
    idx = pd.date_range(start, periods=len(next(iter(rows.values()))), freq="1D", tz="UTC")
    return pd.DataFrame(rows, index=idx)


def test_weekly_stamps_are_sunday_2345():
    idx = pd.date_range("2024-01-01", "2024-02-01", freq="15min", tz="UTC")
    s = rebalance_stamps(idx, "weekly")
    assert len(s) == 4 and (s.dayofweek == 6).all() and (s.strftime("%H:%M") == "23:45").all()
    d = rebalance_stamps(idx, "daily")
    assert len(d) == 31 and (d.strftime("%H:%M") == "23:45").all()


def test_params_id():
    assert XSecParams(30, 2, 1.5, 0.10).params_id == "L30_k2_g1.5_v0.1"
    assert XSecParams(14, 1, rebalance="daily").params_id == "L14_k1_g1_v0_daily"


def test_raw_targets_rank_ties_and_signs():
    dc = _dc({"A": [100, 105], "B": [100, 105], "C": [100, 97]})
    t = dc.index[-1]
    w = raw_targets(dc, ["A", "B", "C"], XSecParams(1, 1, 1.0, 0.1), "perp", t)
    assert w.to_dict() == {"A": 0.5, "B": 0.0, "C": -0.5}  # tie A/B -> A by name


def test_k_clipped_by_universe():
    dc = _dc({"A": [100, 110], "B": [100, 90], "C": [100, 100]})
    t = dc.index[-1]
    w = raw_targets(dc, ["A", "B", "C"], XSecParams(1, 2, 1.0, 0.1), "perp", t)
    assert (w != 0).sum() == 2 and w["A"] == 0.5 and w["B"] == -0.5
    assert (raw_targets(dc, ["A"], XSecParams(1, 2, 1.0, 0.1), "perp", t) == 0).all()
    assert raw_targets(dc, [], XSecParams(1, 2), "perp", t).empty


def test_spot_targets_long_only():
    dc = _dc({"A": [100, 110], "B": [100, 90], "C": [100, 100]})
    w = raw_targets(dc, ["A", "B", "C"], XSecParams(1, 2), "spot", dc.index[-1])
    assert (w >= 0).all() and w.to_dict() == {"A": 0.5, "B": 0.0, "C": 0.5}


def test_eligible_rules():
    n = 40
    dc = _dc({"A": list(np.linspace(100, 120, n)), "B": [np.nan] * 15 + list(np.linspace(1, 2, n - 15)),
              "C": list(np.linspace(50, 40, n))})
    t = dc.index[-1]
    real = {"A": True, "B": True, "C": False}
    assert eligible(dc, real, ["A", "B", "C", "Z"], 14, t) == ["A"]  # B: 25 closes < 31; C: no real bar
    assert eligible(dc, {"A": True}, ["A"], 45, t) == []  # lookback beyond history


def test_daily_closes_use_complete_days(v2_panel):
    dc = daily_closes({"BTC": v2_panel["BTC"]})
    assert (dc.index.strftime("%H:%M") == "23:45").all()
    assert dc["BTC"].iloc[0] == v2_panel["BTC"]["close"].iloc[95]
