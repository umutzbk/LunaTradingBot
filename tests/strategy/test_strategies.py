import math

import numpy as np
import pandas as pd
import pytest

from luna.config import CostConfig, load_settings
from luna.engine.backtest import run_backtest
from luna.features.core import atr, ewma_zscore, rolling_beta
from luna.portfolio.portfolio import Position
from luna.ranking.ranker import AcceptAllRanker
from luna.strategy.mean_reversion import MeanReversion
from luna.strategy.momentum import Momentum
from luna.strategy.pairs import Pairs
from luna.types import Leg, StopSpec
from tests.helpers import assert_causal, synthetic_bars, synthetic_pair

SETTINGS = load_settings()
START = pd.Timestamp("2000-01-01", tz="UTC")
END = pd.Timestamp("2100-01-01", tz="UTC")
COLS = ["entry", "exit_long", "exit_short", "stop_distance", "regime"]


def _zero_cost_settings():
    """Zero fees/spread/slippage, large capital, loss limits and emergency stop out of the way."""
    lim = SETTINGS.accounts["perp"].model_copy(update={
        "capital_usd": 1_000_000.0, "max_drawdown": 0.99, "daily_loss_limit": 0.99,
        "emergency_stop_frac": 0.99, "max_orders_per_day": 10_000,
    })
    zero = CostConfig(maker_fee=0.0, taker_fee=0.0, half_spread_bps={"default": 0.0}, slippage_bps=0.0)
    return SETTINGS.model_copy(update={
        "accounts": {**SETTINGS.accounts, "perp": lim},
        "costs": {**SETTINGS.costs, "perp": zero},
    })


def _gross_r(panel, strat, keys) -> pd.Series:
    res = run_backtest(panel, {}, [strat], {strat.name: keys}, AcceptAllRanker(), "perp",
                       _zero_cost_settings(), START, END)
    t = res.trades
    return (t["pnl_gross"] / t["risk_amount"]).astype(float)


def _pos(direction, stop, exit_spec, key="BTC", legs=None, bars_held=0):
    return Position(
        id="p", strategy="s", key=key, legs=legs or (Leg(key, 1.0),), direction=direction, qty={},
        entry_price={}, entry_ts=pd.Timestamp("2024-01-01", tz="UTC"), entry_ref=100.0,
        stop=stop, exit=exit_spec, risk_amount=1.0, regime="NONE", bars_held=bars_held,
    )


# --- grids ---------------------------------------------------------------------------------------


def test_grids_sizes():
    assert len(Momentum.GRID) == 8
    assert len(MeanReversion.GRID) == 8
    assert len(Pairs.GRID) == 18
    assert {(g["fast"], g["slow"]) for g in Momentum.GRID} == {(48, 192), (96, 480)}
    assert {g["donchian"] for g in Momentum.GRID} == {96, 288}
    assert {g["k_atr"] for g in Momentum.GRID} == {2, 3}
    assert {g["W"] for g in MeanReversion.GRID} == {96, 288}
    assert {g["z_in"] for g in MeanReversion.GRID} == {2.0, 2.5}
    assert {g["z_out"] for g in MeanReversion.GRID} == {0.0, 0.5}
    assert {g["W"] for g in Pairs.GRID} == {96, 288, 672}
    assert {g["z_in"] for g in Pairs.GRID} == {1.5, 2.0, 2.5}
    assert {g["z_out"] for g in Pairs.GRID} == {0.0, 0.5}
    for cls in (Momentum, MeanReversion, Pairs):
        ids = [cls(g).params_id for g in cls.GRID]
        assert len(set(ids)) == len(ids)
    assert MeanReversion({"W": 96, "z_in": 2.0, "z_out": 0.5}).params_id == "W96_zin2.0_zout0.5"
    assert Momentum({"fast": 48, "slow": 192, "donchian": 96, "k_atr": 2}).params_id == "F48_S192_N96_k2"


# --- causality / warm-up -------------------------------------------------------------------------


def _frame(strat, panel, key):
    sf = strat.signals(panel, [key])[key]
    return pd.DataFrame({c: getattr(sf, c) for c in COLS})


@pytest.mark.parametrize("use_regime", [True, False])
def test_strategies_causal(use_regime):
    trend = synthetic_bars(3_000, "trend", seed=1, drift=0.0002)
    mom = Momentum({"fast": 48, "slow": 192, "donchian": 96, "k_atr": 2}, use_regime=use_regime)
    assert_causal(lambda d: _frame(mom, {"BTC": d}, "BTC"), trend)
    ou = synthetic_bars(3_000, "ou", seed=2)
    mr = MeanReversion({"W": 96, "z_in": 2.0, "z_out": 0.5}, use_regime=use_regime)
    assert_causal(lambda d: _frame(mr, {"BTC": d}, "BTC"), ou)
    a, b = synthetic_pair(3_000, "cointegrated", seed=3)
    pr = Pairs({"W": 96, "z_in": 1.5, "z_out": 0.0}, use_regime=use_regime)
    assert_causal(lambda d: _frame(pr, {"ETH": d, "BTC": b.loc[d.index]}, "ETH/BTC"), a)
    if not use_regime:  # the checks above are not vacuous
        assert (_frame(mom, {"BTC": trend}, "BTC")["entry"] != 0).any()
        assert (_frame(mr, {"BTC": ou}, "BTC")["entry"] != 0).any()
        assert (_frame(pr, {"ETH": a, "BTC": b}, "ETH/BTC")["entry"] != 0).any()


def _all_frames(n, use_regime):
    """(strategy, signal frame) for every grid point on n-bar synthetic data."""
    trend = synthetic_bars(n, "trend", seed=7, drift=0.0002)
    ou = synthetic_bars(n, "ou", seed=8)
    a, b = synthetic_pair(n, "cointegrated", seed=9)
    out = []
    for g in Momentum.GRID:
        s = Momentum(g, use_regime=use_regime)
        out.append((s, s.signals({"BTC": trend}, ["BTC"])["BTC"]))
    for g in MeanReversion.GRID:
        s = MeanReversion(g, use_regime=use_regime)
        out.append((s, s.signals({"BTC": ou}, ["BTC"])["BTC"]))
    for g in Pairs.GRID:
        s = Pairs(g, use_regime=use_regime)
        out.append((s, s.signals({"ETH": a, "BTC": b}, ["ETH/BTC"])["ETH/BTC"]))
    return out


def test_strategies_emit_nothing_during_warmup():
    # 2,500 bars: the regime's trailing-quantile thresholds need 480 + 2,160 bars -> UNKNOWN all along
    for strat, sf in _all_frames(2_500, use_regime=True):
        assert (sf.regime == "UNKNOWN").all()
        assert (sf.entry == 0).all(), strat.params_id
        assert (sf.entry.iloc[:480] == 0).all()
    # without the regime gate, entries appear only once every feature is finite, and every
    # firing row gives a finite candidate (no NaN can reach sizing)
    fired = 0
    for strat, sf in _all_frames(2_500, use_regime=False):
        rows = np.flatnonzero(sf.entry.to_numpy() != 0)
        fired += len(rows)
        first_finite = int(np.flatnonzero(sf.stop_distance.notna().to_numpy())[0])
        assert (rows >= first_finite).all()
        for i in rows:
            c = strat.candidate_at(sf, int(i), {})
            assert c is not None
            assert math.isfinite(c.stop.level) and c.stop.distance_frac > 0
            assert all(math.isfinite(v) for v in c.features.values())
            assert all(math.isfinite(leg.weight) for leg in c.legs)
        for i in range(first_finite):
            assert strat.candidate_at(sf, i, {}) is None
    assert fired > 0


# --- regime gates --------------------------------------------------------------------------------


def _patch_regime(monkeypatch, module, label_fn):
    monkeypatch.setattr(module, "classify_regime",
                        lambda bars: pd.Series(label_fn(len(bars)), index=bars.index, dtype=object))


def test_regime_gates(monkeypatch):
    import luna.strategy.mean_reversion as mr_mod
    import luna.strategy.momentum as mom_mod

    ou = synthetic_bars(3_000, "ou", seed=2)
    params = {"W": 96, "z_in": 2.0, "z_out": 0.5}
    ungated = MeanReversion(params, use_regime=False).signals({"X": ou}, ["X"])["X"].entry
    assert (ungated != 0).sum() > 5
    for label, expect_entries in (("RANGE", True), ("HIGH_VOL", False), ("TREND_UP", False),
                                  ("UNKNOWN", False)):
        _patch_regime(monkeypatch, mr_mod, lambda n, lab=label: [lab] * n)
        sf = MeanReversion(params).signals({"X": ou}, ["X"])["X"]
        assert (sf.regime == label).all()
        if expect_entries:
            pd.testing.assert_series_equal(sf.entry, ungated)
        else:
            assert (sf.entry == 0).all()
        # use_regime=False ignores the label but still reports it
        sf = MeanReversion(params, use_regime=False).signals({"X": ou}, ["X"])["X"]
        pd.testing.assert_series_equal(sf.entry, ungated)
        assert (sf.regime == label).all()

    up = synthetic_bars(3_000, "trend", seed=3, drift=0.0003)
    mp = {"fast": 48, "slow": 192, "donchian": 96, "k_atr": 2}
    ungated = Momentum(mp, use_regime=False).signals({"X": up}, ["X"])["X"].entry
    assert (ungated == 1).sum() > 2
    _patch_regime(monkeypatch, mom_mod, lambda n: ["TREND_UP"] * n)
    pd.testing.assert_series_equal(Momentum(mp).signals({"X": up}, ["X"])["X"].entry, ungated)
    for label in ("TREND_DOWN", "RANGE", "HIGH_VOL", "UNKNOWN"):
        _patch_regime(monkeypatch, mom_mod, lambda n, lab=label: [lab] * n)
        assert (Momentum(mp).signals({"X": up}, ["X"])["X"].entry == 0).all()


def test_pairs_regime_from_leg_a_blocks_high_vol(monkeypatch):
    import luna.strategy.pairs as pairs_mod

    a, b = synthetic_pair(3_000, "cointegrated", seed=4)
    p = {"W": 96, "z_in": 1.5, "z_out": 0.0}
    ungated = Pairs(p, use_regime=False).signals({"A": a, "B": b}, ["A/B"])["A/B"].entry
    assert (ungated != 0).sum() > 5
    seen = []

    def fake(bars):
        seen.append(bars)
        return pd.Series(["HIGH_VOL"] * 1_500 + ["TREND_DOWN"] * 1_500, index=bars.index, dtype=object)

    monkeypatch.setattr(pairs_mod, "classify_regime", fake)
    sf = Pairs(p).signals({"A": a, "B": b}, ["A/B"])["A/B"]
    assert len(seen) == 1 and seen[0] is a
    assert (sf.entry.iloc[:1_500] == 0).all()
    pd.testing.assert_series_equal(sf.entry.iloc[1_501:], ungated.iloc[1_501:])  # edge may fire at 1500
    assert sf.regime.iloc[-1] == "TREND_DOWN"


# --- candidates and exits ------------------------------------------------------------------------


def test_momentum_candidate_and_trailing_stop():
    bars = synthetic_bars(3_000, "trend", seed=5, drift=0.0003)
    strat = Momentum({"fast": 48, "slow": 192, "donchian": 96, "k_atr": 3}, use_regime=False)
    sf = strat.signals({"BTC": bars}, ["BTC"])["BTC"]
    k = int(np.flatnonzero(sf.entry.to_numpy() == 1)[0])
    a96 = atr(bars, 96)
    close = bars["close"]
    assert close.iloc[k] > bars["high"].iloc[k - 96:k].max()  # Donchian breakout of previous N bars
    c = strat.candidate_at(sf, k, {})
    assert c.direction == 1 and c.strategy == "momentum" and c.stop.kind == "price"
    assert c.stop.level == pytest.approx(close.iloc[k] - 3 * a96.iloc[k])
    assert c.stop.distance_frac == pytest.approx(3 * a96.iloc[k] / close.iloc[k])
    assert c.exit.max_hold_bars == 2880 and c.exit.trailing_atr_mult == 3
    assert c.features["trend_strength"] > 0.5
    assert "donchian" in c.reason

    pos = _pos(1, c.stop, c.exit)
    i = k + 1
    atr_i = float(a96.iloc[i])
    # price far above: trail rises to close - 3*ATR
    assert strat.should_exit(sf, i, pos, {"BTC": 200.0}) is None
    assert pos.trail_level == pytest.approx(200.0 - 3 * atr_i)
    # a lower close (still above the trail) never lowers it
    assert strat.should_exit(sf, i, pos, {"BTC": 200.0 - 1.5 * atr_i}) is None
    assert pos.trail_level == pytest.approx(200.0 - 3 * atr_i)
    assert strat.should_exit(sf, i, pos, {"BTC": 200.0 - 3 * atr_i}) == "trailing"
    # short mirror; an unmoved level is the initial stop
    pos = _pos(-1, StopSpec("price", 110.0, 0.1), c.exit)
    assert strat.should_exit(sf, i, pos, {"BTC": 111.0}) == "stop"
    pos = _pos(-1, StopSpec("price", 110.0, 0.1), c.exit)
    assert strat.should_exit(sf, i, pos, {"BTC": 50.0}) is None
    assert pos.trail_level == pytest.approx(50.0 + 3 * atr_i)
    assert strat.should_exit(sf, i, pos, {"BTC": 50.0 + 3 * atr_i + 0.01}) == "trailing"
    pos = _pos(1, c.stop, c.exit, bars_held=2880)
    assert strat.should_exit(sf, i, pos, {"BTC": 1e6}) == "time"


def test_mean_reversion_candidate_and_directional_zstop():
    ou = synthetic_bars(3_000, "ou", seed=6)
    strat = MeanReversion({"W": 96, "z_in": 2.0, "z_out": 0.5}, use_regime=False)
    sf = strat.signals({"BTC": ou}, ["BTC"])["BTC"]
    close = ou["close"]
    z = ewma_zscore(close, 96)
    sigma = close.ewm(span=96, min_periods=96, adjust=False).std()
    k = int(np.flatnonzero(sf.entry.to_numpy() == 1)[0])  # long: z below -2
    assert z.iloc[k] < -2.0 and abs(z.iloc[k - 1]) <= 2.0
    c = strat.candidate_at(sf, k, {})
    assert c.stop.kind == "zscore"
    assert c.stop.level == pytest.approx(z.iloc[k] - 1.5)
    assert c.stop.distance_frac == pytest.approx(1.5 * sigma.iloc[k] / close.iloc[k])
    assert c.exit.max_hold_bars == 4 * 96 and c.exit.take_profit_z == 0.5

    j = k + 1
    long = _pos(1, StopSpec("zscore", -3.7, 0.01), c.exit)  # entered at z = -2.2
    short = _pos(-1, StopSpec("zscore", 3.7, 0.01), c.exit)  # entered at z = +2.2
    for zv, lr, sr in ((-3.8, "stop", "signal"), (3.8, "signal", "stop"), (-3.6, None, "signal"),
                       (-0.4, "signal", "signal"), (-1.0, None, "signal"), (1.0, "signal", None),
                       (3.6, "signal", None)):
        sf.z.iloc[j] = zv
        sf.exit_long.iloc[j] = zv >= -0.5
        sf.exit_short.iloc[j] = zv <= 0.5
        assert strat.should_exit(sf, j, long, {"BTC": 1.0}) == lr, zv
        assert strat.should_exit(sf, j, short, {"BTC": 1.0}) == sr, zv
    sf.z.iloc[j] = -3.0
    sf.exit_long.iloc[j] = False
    long.bars_held = 384
    assert strat.should_exit(sf, j, long, {"BTC": 1.0}) == "time"


def test_mean_reversion_take_profit_with_zero_z_out():
    ou = synthetic_bars(3_000, "ou", seed=6)
    sf = MeanReversion({"W": 96, "z_in": 2.0, "z_out": 0.0}, use_regime=False).signals(
        {"BTC": ou}, ["BTC"])["BTC"]
    z = sf.z
    pd.testing.assert_series_equal(sf.exit_long, (z >= 0).fillna(False), check_names=False)
    pd.testing.assert_series_equal(sf.exit_short, (z <= 0).fillna(False), check_names=False)


def test_pairs_candidate_freezes_beta():
    a, b = synthetic_pair(3_000, "cointegrated", seed=10)
    strat = Pairs({"W": 288, "z_in": 2.0, "z_out": 0.5}, use_regime=False)
    sf = strat.signals({"ETH": a, "BTC": b}, ["ETH/BTC"])["ETH/BTC"]
    la, lb = np.log(a["close"]), np.log(b["close"])
    beta = rolling_beta(la, lb, 288)
    s = la - beta * lb
    pd.testing.assert_series_equal(sf.legs_beta, beta, check_names=False)
    pd.testing.assert_series_equal(sf.ref, s, check_names=False)
    for t in (700, 1500, 2999):  # z_t: EWMA z-score of the spread history taken with beta_t
        fixed = la.iloc[:t + 1] - beta.iloc[t] * lb.iloc[:t + 1]
        assert sf.z.iloc[t] == pytest.approx(ewma_zscore(fixed, 288).iloc[-1], rel=1e-6)
    rows = np.flatnonzero(sf.entry.to_numpy() != 0)
    assert len(rows) > 3
    k = int(rows[0])
    c = strat.candidate_at(sf, k, {})
    assert c.legs == (Leg("ETH", 1.0), Leg("BTC", -float(beta.iloc[k])))
    assert c.direction == -np.sign(sf.z.iloc[k])
    fixed = la.iloc[:k + 1] - beta.iloc[k] * lb.iloc[:k + 1]
    sigma = fixed.ewm(span=288, min_periods=288, adjust=False).std().iloc[-1]
    assert c.stop.distance_frac == pytest.approx(1.5 * sigma, rel=1e-6)
    assert c.stop.level == pytest.approx(sf.z.iloc[k] - c.direction * 1.5)
    assert c.exit.max_hold_bars == 288
    pos = _pos(c.direction, c.stop, c.exit, key="ETH/BTC", legs=c.legs)
    sf.z.iloc[k + 1] = c.stop.level - c.direction * 0.01
    assert strat.should_exit(sf, k + 1, pos, {}) == "stop"


# --- synthetic profitability (run_backtest, AcceptAllRanker, zero costs) -------------------------


def test_ou_series_mean_reversion_gross_positive():
    # use_regime=False: regime forced to RANGE semantics (no gate) -- the synthetic OU path has
    # HIGH_VOL / trend labels from its own trailing quantiles that would only thin the sample.
    ou = synthetic_bars(12_000, "ou", seed=11)
    r = _gross_r({"BTC": ou}, MeanReversion({"W": 96, "z_in": 2.0, "z_out": 0.0}, use_regime=False),
                 ["BTC"])
    assert len(r) >= 30
    assert r.mean() > 0.1


def test_trend_series_momentum_gross_positive():
    # use_regime=False: a constant-drift path is labelled TREND_UP only above its own trailing
    # 70th percentile of trend strength, which would just thin the sample.
    up = synthetic_bars(12_000, "trend", seed=12, drift=0.0003)
    down = synthetic_bars(12_000, "trend", seed=13, drift=-0.0003)
    strat = Momentum({"fast": 48, "slow": 192, "donchian": 96, "k_atr": 2}, use_regime=False)
    r = _gross_r({"UP": up, "DN": down}, strat, ["UP", "DN"])
    assert len(r) >= 30
    assert r.mean() > 0.1


def test_cointegrated_pair_gross_positive():
    a, b = synthetic_pair(12_000, "cointegrated", seed=14)
    r = _gross_r({"A": a, "B": b}, Pairs({"W": 288, "z_in": 2.0, "z_out": 0.0}, use_regime=False),
                 ["A/B"])
    assert len(r) >= 30
    assert r.mean() > 0.1


def test_independent_pair_gross_near_zero():
    a, b = synthetic_pair(20_000, "independent", seed=15)
    r = _gross_r({"A": a, "B": b}, Pairs({"W": 96, "z_in": 1.5, "z_out": 0.0}, use_regime=False),
                 ["A/B"])
    assert len(r) >= 30
    assert abs(r.mean()) < 0.15
