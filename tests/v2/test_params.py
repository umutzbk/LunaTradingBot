import pandas as pd
import pytest

from luna.v2.params import (
    FAMILY_TIMEFRAMES,
    atr_bars,
    breakout_grid,
    max_hold_bars,
    tsmom_grid,
    xsec_grid,
)


def test_tsmom_15m_contains_v1_naive_momentum():
    assert {"fast": 96, "slow": 480, "atr_n": 96, "stop_atr": 3.0, "max_hold": 2880} in tsmom_grid("15m")


@pytest.mark.parametrize(("tf", "n"), [("15m", 6), ("1h", 6), ("4h", 6), ("1d", 4)])
def test_grid_sizes(tf, n):
    assert len(tsmom_grid(tf)) == n and len(breakout_grid(tf)) == n


def test_grid_values_in_bars():
    assert {(g["fast"], g["slow"]) for g in tsmom_grid("1d")} == {(4, 20), (12, 60)}
    assert {g["donchian"] for g in breakout_grid("4h")} == {12, 60, 180}
    assert {g["k_atr"] for g in breakout_grid("1h")} == {2.0, 3.0}
    assert {g["donchian"]: g["max_hold"] for g in breakout_grid("1h")} == {48: 2880, 240: 2880, 720: 5760}


def test_atr_and_hold_rules():
    assert [atr_bars(t) for t in ("15m", "1h", "4h", "1d")] == [96, 24, 14, 14]
    assert max_hold_bars(pd.Timedelta(days=5)) == 2880 and max_hold_bars(pd.Timedelta(days=60)) == 11520


def test_xsec_grid_sizes_and_risk_grid_is_frozen():
    assert len(xsec_grid("perp")) == 24 and len(xsec_grid("spot")) == 6
    assert {(g["gross"], g["vol_target"]) for g in xsec_grid("perp")} == {
        (1.0, .10), (1.0, .15), (1.5, .10), (1.5, .15)}
    assert {(g["lookback_days"], g["k"]) for g in xsec_grid("spot")} == {
        (L, k) for L in (14, 30, 90) for k in (1, 2)}
    assert set(xsec_grid("spot")[0]) == {"lookback_days", "k"}


def test_spot_has_no_15m():
    assert "15m" not in FAMILY_TIMEFRAMES["spot"]["tsmom"]
    assert FAMILY_TIMEFRAMES["perp"]["breakout"] == ("15m", "1h", "4h", "1d")
    assert FAMILY_TIMEFRAMES["perp"]["xsec"] == ("1d",) == FAMILY_TIMEFRAMES["spot"]["xsec"]
