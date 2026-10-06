import numpy as np
import pandas as pd
import pytest

from luna.portfolio.portfolio import Position
from luna.strategy.naive import NaiveMeanReversion, NaiveMomentum, NaivePairs
from luna.types import ExitSpec, Leg, StopSpec
from tests.helpers import assert_causal, synthetic_bars, synthetic_pair

COLS = ["entry", "exit_long", "exit_short", "stop_distance"]


def _frame_of(strategy, key, bars):
    sf = strategy.signals({key: bars}, [key])[key]
    return pd.DataFrame({c: getattr(sf, c) for c in COLS})


def test_naive_signals_causal():
    bars = synthetic_bars(900, "trend", seed=1)
    assert_causal(lambda d: _frame_of(NaiveMomentum(), "BTC", d), bars)
    ou = synthetic_bars(900, "ou", seed=2)
    assert_causal(lambda d: _frame_of(NaiveMeanReversion(), "BTC", d), ou)
    a, b = synthetic_pair(900, "cointegrated", seed=3)

    def pairs(d):
        sf = NaivePairs().signals({"ETH": d, "BTC": b.loc[d.index]}, ["ETH/BTC"])["ETH/BTC"]
        return pd.DataFrame({c: getattr(sf, c) for c in COLS})

    assert_causal(pairs, a)


def test_naive_momentum_enters_on_cross():
    n = 400
    close = np.concatenate([np.linspace(200, 100, 200), np.linspace(100, 300, 200)])
    bars = synthetic_bars(n, "random_walk", seed=0)
    bars["close"] = close
    bars["high"] = close * 1.001
    bars["low"] = close * 0.999
    strat = NaiveMomentum({"fast": 10, "slow": 40, "atr_n": 10})
    sf = strat.signals({"BTC": bars}, ["BTC"])["BTC"]
    longs = sf.entry[sf.entry == 1]
    assert len(longs) == 1
    k = sf.entry.index.get_loc(longs.index[0])
    assert k > 200
    assert (sf.entry.drop(longs.index) == 0).all()
    cand = strat.candidate_at(sf, k, {})
    assert cand is not None and cand.direction == 1
    assert cand.stop.kind == "price"
    assert cand.stop.level == pytest.approx(close[k] - 3 * sf.atr.iloc[k])
    assert cand.stop.distance_frac == pytest.approx(3 * sf.atr.iloc[k] / close[k])
    assert cand.regime == "NONE" and cand.exit.max_hold_bars == 2880


def _pos(direction, stop, exit_spec, bars_held=0, key="BTC"):
    return Position(
        id="p", strategy="s", key=key, legs=(Leg(key, 1.0),), direction=direction, qty={},
        entry_price={}, entry_ts=pd.Timestamp("2024-01-01", tz="UTC"), entry_ref=100.0,
        stop=stop, exit=exit_spec, risk_amount=1.0, regime="NONE", bars_held=bars_held,
    )


def test_naive_mr_exit_and_stop_flags():
    n = 300
    close = np.full(n, 100.0)
    close[200] = 130.0  # huge up spike -> short entry
    bars = synthetic_bars(n, "random_walk", seed=0)
    bars["close"] = close
    strat = NaiveMeanReversion()
    sf = strat.signals({"BTC": bars}, ["BTC"])["BTC"]
    assert sf.entry.iloc[200] == -1
    assert (sf.entry.drop(sf.entry.index[200]) == 0).all()
    cand = strat.candidate_at(sf, 200, {})
    assert cand.direction == -1 and cand.stop.kind == "zscore" and cand.stop.level == 3.5
    sigma = close[:201].std()  # placeholder sanity: distance is positive and finite
    assert 0 < cand.stop.distance_frac and sigma > 0
    pos = _pos(-1, cand.stop, cand.exit)
    sf.z.iloc[250] = 3.6
    assert strat.should_exit(sf, 250, pos, {"BTC": 100.0}) == "stop"
    sf.z.iloc[251] = 0.2
    sf.exit_short.iloc[251] = True
    assert strat.should_exit(sf, 251, pos, {"BTC": 100.0}) == "signal"
    sf.z.iloc[252] = 1.5
    sf.exit_short.iloc[252] = False
    assert strat.should_exit(sf, 252, pos, {"BTC": 100.0}) is None
    pos.bars_held = 288
    assert strat.should_exit(sf, 252, pos, {"BTC": 100.0}) == "time"


def test_momentum_should_exit_variants():
    bars = synthetic_bars(700, "trend", seed=4)
    strat = NaiveMomentum()
    sf = strat.signals({"BTC": bars}, ["BTC"])["BTC"]
    i = 650
    pos = _pos(1, StopSpec("price", 90.0, 0.1), ExitSpec(2880))
    assert strat.should_exit(sf, i, pos, {"BTC": 89.0}) == "stop"
    sf.exit_long.iloc[i] = True
    assert strat.should_exit(sf, i, pos, {"BTC": 120.0}) == "signal"
    sf.exit_long.iloc[i] = False
    assert strat.should_exit(sf, i, pos, {"BTC": 120.0}) is None
    pos.bars_held = 2880
    assert strat.should_exit(sf, i, pos, {"BTC": 120.0}) == "time"


def test_naive_pairs_uses_beta_one():
    a, b = synthetic_pair(700, "cointegrated", seed=5)
    strat = NaivePairs()
    assert strat.params["span"] == 288
    sf = strat.signals({"ETH": a, "BTC": b.iloc[50:]}, ["ETH/BTC"])["ETH/BTC"]
    assert sf.legs_beta is not None and (sf.legs_beta == 1.0).all()
    assert len(sf.entry) == 650  # intersection of indexes
    expected = np.log(a["close"].iloc[50:]) - np.log(b["close"].iloc[50:])
    pd.testing.assert_series_equal(sf.ref, expected, check_names=False)
    idx = np.flatnonzero(sf.entry.to_numpy() != 0)
    assert len(idx) > 0
    cand = strat.candidate_at(sf, int(idx[0]), {})
    assert cand.legs == (Leg("ETH", 1.0), Leg("BTC", -1.0))
    assert cand.direction == -np.sign(sf.z.iloc[idx[0]])
    assert cand.stop.kind == "zscore"


def test_candidate_at_none_on_nan():
    bars = synthetic_bars(700, "trend", seed=6)
    strat = NaiveMomentum()
    sf = strat.signals({"BTC": bars}, ["BTC"])["BTC"]
    assert strat.candidate_at(sf, 5, {}) is None  # entry == 0
    sf.entry.iloc[5] = 1  # forced entry during ATR warm-up -> NaN
    assert strat.candidate_at(sf, 5, {}) is None
