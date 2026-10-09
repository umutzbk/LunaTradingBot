"""V2 hooks used by V3: every default path must behave exactly as before (golden)."""

import numpy as np
import pandas as pd

from luna.v2.portfolio_risk import project_weights
from luna.v2.xsec import XSecParams, daily_closes, eligible, lookback_returns, raw_targets, scores


def test_default_params_id_unchanged():
    assert XSecParams(30, 1).params_id == "L30_k1_g1_v0"
    assert XSecParams(30, 1, 1.5, 0.1).params_id == "L30_k1_g1.5_v0.1"
    assert XSecParams(30, 1, rebalance="daily").params_id == "L30_k1_g1_v0_daily"


def test_non_default_signal_is_in_params_id():
    assert XSecParams(30, 1, signal="lowvol").params_id == "L30_k1_g1_v0_lowvol"


def test_momentum_scores_are_lookback_returns(v2_panel):
    dc = daily_closes(v2_panel)
    t = dc.index[100]
    pd.testing.assert_series_equal(scores(dc, t, XSecParams(30, 1)), lookback_returns(dc, t, 30))


def test_raw_targets_default_ranks_by_lookback_return(v2_panel):
    dc = daily_closes(v2_panel)
    t = dc.index[100]
    assets = ["BTC", "ETH", "AAA", "BBB"]
    got = raw_targets(dc, assets, XSecParams(30, 1), "perp", t)
    rets = lookback_returns(dc, t, 30)[assets]
    assert got[rets.idxmax()] == 0.5 and got[rets.idxmin()] == -0.5 and got.abs().sum() == 1.0


def test_eligible_default_unchanged_with_params(v2_panel):
    dc = daily_closes(v2_panel)
    t = dc.index[100]
    real = {a: True for a in v2_panel}
    uni = sorted(v2_panel)
    assert eligible(dc, real, uni, 30, t) == eligible(dc, real, uni, 30, t, params=XSecParams(30, 1))


def test_spot_projection_without_vol_target_is_unchanged():
    raw = pd.Series({"BTC": 0.5, "ETH": 0.5})
    cov = pd.DataFrame(np.eye(2) * 1e-3, index=raw.index, columns=raw.index)
    p = project_weights(raw, cov, cov, controls="spot", gross=1.0, vol_target=None)
    assert p.weights.tolist() == [0.4, 0.4] and p.vol_scale == 1.0


def test_spot_vol_target_scales_down_when_vol_is_high():
    raw = pd.Series({"BTC": 0.5, "ETH": 0.5})
    hi = pd.DataFrame(np.eye(2) * 4e-3, index=raw.index, columns=raw.index)
    p = project_weights(raw, hi, hi, controls="spot", gross=1.0, vol_target=0.10)
    assert 0 < p.vol_scale < 1 and (p.weights < 0.4).all()


def test_spot_vol_target_never_scales_up():
    raw = pd.Series({"BTC": 0.5, "ETH": 0.5})
    lo = pd.DataFrame(np.eye(2) * 1e-8, index=raw.index, columns=raw.index)
    q = project_weights(raw, lo, lo, controls="spot", gross=1.0, vol_target=0.10)
    assert q.vol_scale == 1.0 and q.weights.tolist() == [0.4, 0.4]


def test_spot_vol_target_fails_closed_without_a_usable_covariance():
    raw = pd.Series({"BTC": 0.5, "ETH": 0.5})
    cov = pd.DataFrame(np.eye(2) * 1e-3, index=raw.index, columns=raw.index)
    cov.loc["ETH", "ETH"] = np.nan
    p = project_weights(raw, cov, cov, controls="spot", gross=1.0, vol_target=0.10)
    assert (p.weights == 0).all() and p.reason
    missing = cov.drop(index="ETH", columns="ETH")
    q = project_weights(raw, missing, missing, controls="spot", gross=1.0, vol_target=0.10)
    assert (q.weights == 0).all() and q.reason
