import numpy as np
import pandas as pd
import pytest

from luna.config import CostConfig, RiskLimits
from luna.execution.costs import CostModel
from luna.portfolio.constructor import Selection, construct
from luna.portfolio.portfolio import Portfolio, Position
from luna.types import Candidate, ExitSpec, Fill, Instrument, Leg, StopSpec

TS = pd.Timestamp("2024-01-01 12:00", tz="UTC")
ASSETS = ("A", "B", "C", "D", "E")

LIMITS = RiskLimits(
    capital_usd=265, risk_per_trade=0.0075, max_risk_per_trade=0.01, max_open_risk=0.03,
    max_positions=3, max_gross_leverage=2.0, max_leg_notional_frac=1.0, daily_loss_limit=0.02,
    max_drawdown=0.15, max_orders_per_day=30, max_price_band=0.003, emergency_stop_frac=0.02,
    liq_distance_mult=10, allow_short=True, live_enabled=False, highvol_risk_mult=0.5,
    corr_bucket_threshold=0.7,
)
# round trip = 2 * (0.0004 + 0.0001 + 0.00025) = 0.0015
COSTS = CostModel(CostConfig(maker_fee=0.0002, taker_fee=0.0004, half_spread_bps={"default": 1},
                             slippage_bps=2.5))
MIDS = {a: 100.0 for a in ASSETS}


def insts(min_notional=0.0):
    return {a: Instrument(a, a, "kraken_futures", "perp", 1e-6, 0.01, 1e-6, min_notional, 0.0002, 0.0004)
            for a in ASSETS}


def cand(key="A", direction=1, stop=0.02, score=1.0, legs=None, regime="NONE"):
    legs = legs or (Leg(key, 1.0),)
    return Candidate(TS, "s", key, legs, direction, 100.0, StopSpec("price", 100.0, stop),
                     ExitSpec(10), regime, "t", {}, score=score)


def independent_returns(n=2880, seed=0):
    rng = np.random.default_rng(seed)
    return pd.DataFrame(rng.normal(0, 0.002, (n, len(ASSETS))), columns=list(ASSETS))


def run(cands, pf=None, limits=LIMITS, inst=None, returns=None, regime_mult=lambda c: 1.0, kind="perp"):
    pf = pf or Portfolio(kind, 265.0)
    return construct(cands, pf, limits, COSTS, inst or insts(), MIDS,
                     independent_returns() if returns is None else returns, regime_mult)


def test_constructor_sizing_example():
    sels, skipped = run([cand(stop=0.02)])
    assert skipped == []
    (s,) = sels
    assert isinstance(s, Selection)
    assert s.risk_amount == pytest.approx(265 * 0.0075)
    assert s.gross_notional == pytest.approx(265 * 0.0075 / (0.02 + 0.0015))
    assert s.gross_notional == pytest.approx(92.4, abs=0.1)


def test_constructor_regime_mult_halves_risk():
    (s,), _ = run([cand(regime="HIGH_VOL")], regime_mult=lambda c: 0.5 if c.regime == "HIGH_VOL" else 1.0)
    assert s.risk_amount == pytest.approx(265 * 0.0075 * 0.5)


def test_constructor_respects_max_positions():
    cands = [cand(a, score=s) for a, s in zip("ABCD", (4, 3, 2, 1), strict=True)]
    sels, skipped = run(list(reversed(cands)))  # sorted by score inside
    assert [s.cand.key for s in sels] == ["A", "B", "C"]
    assert [(c.key, r) for c, r in skipped] == [("D", "max_positions")]


def test_constructor_counts_existing_positions():
    pf = Portfolio("perp", 265.0)
    for a in "ABC":
        pf.open_position(Position(a, "s", a, (Leg(a, 1.0),), 1, {}, {}, TS, 100.0,
                                  StopSpec("price", 98.0, 0.02), ExitSpec(10), 1.0, "NONE"))
    _, skipped = run([cand("D")], pf=pf)
    assert skipped[0][1] == "max_positions"


def test_constructor_corr_bucket():
    rng = np.random.default_rng(1)
    base = rng.normal(0, 0.002, 2880)
    ret = independent_returns()
    ret["B"] = 0.9 * base + np.sqrt(1 - 0.81) * rng.normal(0, 0.002, 2880)
    ret["A"] = base
    assert ret["A"].corr(ret["B"]) > 0.85
    sels, skipped = run([cand("A", score=2), cand("B", score=1)], returns=ret)
    assert [s.cand.key for s in sels] == ["A"]
    assert [(c.key, r) for c, r in skipped] == [("B", "corr_bucket")]
    # opposite direction is a different bucket
    sels, skipped = run([cand("A", score=2), cand("B", direction=-1, score=1)], returns=ret)
    assert len(sels) == 2 and skipped == []


def test_constructor_opposite_exposure_and_same_asset():
    pair = cand("A/B", legs=(Leg("A", 1.0), Leg("B", -1.0)), score=1)
    sels, skipped = run([cand("B", score=2), pair])
    assert [(c.key, r) for c, r in skipped] == [("A/B", "opposite_exposure")]
    # same asset, same direction -> correlation 1 -> same bucket
    _, skipped = run([cand("A", score=2), cand("A", score=1)])
    assert skipped[0][1] == "corr_bucket"


def test_constructor_open_risk():
    lim = LIMITS.model_copy(update={"max_open_risk": 0.01})
    sels, skipped = run([cand("A", score=2), cand("B", score=1)], limits=lim)
    assert len(sels) == 1 and skipped[0][1] == "open_risk"


def test_constructor_skips_below_minimum():
    sels, skipped = run([cand("A")], inst=insts(min_notional=100.0))
    assert sels == []
    assert skipped[0][1] == "below_min_notional"


def test_constructor_clips_to_leverage():
    lim = LIMITS.model_copy(update={"max_gross_leverage": 0.2})
    (s,), _ = run([cand("A", stop=0.02)], limits=lim)
    assert s.gross_notional == pytest.approx(0.2 * 265)
    assert s.risk_amount == pytest.approx(0.2 * 265 * (0.02 + 0.0015))
    lim = LIMITS.model_copy(update={"max_leg_notional_frac": 0.1})
    (s,), _ = run([cand("A", stop=0.002)], limits=lim)
    assert s.gross_notional == pytest.approx(0.1 * 265)
    # pairs: the larger leg is capped, gross = cap * sum|w| / max|w|
    pair = cand("A/B", legs=(Leg("A", 1.0), Leg("B", -0.5)), stop=0.002)
    (s,), _ = run([pair], limits=lim)
    assert s.gross_notional == pytest.approx(0.1 * 265 * 1.5)


def test_constructor_short_not_allowed_on_spot():
    _, skipped = run([cand("A", direction=-1)], kind="spot")
    assert skipped[0][1] == "short_not_allowed"
    sels, _ = run([cand("A", direction=1)], kind="spot")
    assert len(sels) == 1


def test_constructor_leverage_cap_reason():
    pf = Portfolio("perp", 265.0)
    pf.open_position(Position("x", "s", "E", (Leg("E", 1.0),), 1, {}, {}, TS, 100.0,
                              StopSpec("price", 98.0, 0.02), ExitSpec(10), 1.0, "NONE"))
    pf.apply_fill(Fill("f", TS, "E", "buy", 5.3, 100.0, 0.0, "taker"), "x", 100.0)  # 530 = 2.0 x 265
    _, skipped = run([cand("A")], pf=pf)
    assert skipped[0][1] == "leverage_cap"


def test_constructor_short_overlap_is_uncorrelated():
    rng = np.random.default_rng(2)
    base = rng.normal(0, 0.002, 2880)
    ret = independent_returns()
    ret["A"], ret["B"] = base, base  # perfectly correlated ...
    ret.iloc[:-95, [0, 1]] = np.nan  # ... but only 95 joint observations
    sels, skipped = run([cand("A", score=2), cand("B", score=1)], returns=ret)
    assert len(sels) == 2 and skipped == []
    ret = independent_returns()
    ret["A"], ret["B"] = base, base
    ret.iloc[:-96, [0, 1]] = np.nan  # 96 joint observations: enough
    _, skipped = run([cand("A", score=2), cand("B", score=1)], returns=ret)
    assert skipped[0][1] == "corr_bucket"


def test_constructor_counts_pending_entries():
    """Working (unfilled) entries count for max_positions / opposite_exposure / corr_bucket."""
    pf = Portfolio("perp", 265.0)
    pend = [cand("A")]
    sels, skipped = construct([cand("A", direction=-1)], pf, LIMITS, COSTS, insts(), MIDS,
                              independent_returns(), lambda c: 1.0, pending=pend)
    assert sels == [] and [r for _, r in skipped] == ["opposite_exposure"]
    sels, skipped = construct([cand("A")], pf, LIMITS, COSTS, insts(), MIDS,
                              independent_returns(), lambda c: 1.0, pending=pend)
    assert sels == [] and [r for _, r in skipped] == ["corr_bucket"]  # same asset = corr 1
    r = independent_returns()
    r["B"] = r["A"] + np.random.default_rng(1).normal(0, 0.0002, len(r))  # B ~ A
    sels, skipped = construct([cand("B"), cand("C")], pf, LIMITS, COSTS, insts(), MIDS, r,
                              lambda c: 1.0, pending=pend)
    assert [s.cand.key for s in sels] == ["C"] and [(c.key, x) for c, x in skipped] == [("B", "corr_bucket")]
    sels, skipped = construct([cand("D")], pf, LIMITS, COSTS, insts(), MIDS, independent_returns(),
                              lambda c: 1.0, pending=[cand("A"), cand("B"), cand("C")])
    assert sels == [] and [r for _, r in skipped] == ["max_positions"]
    # no pending entries: unchanged (the backtest's case)
    sels, _ = construct([cand("A")], pf, LIMITS, COSTS, insts(), MIDS, independent_returns(), lambda c: 1.0)
    assert [s.cand.key for s in sels] == ["A"]
