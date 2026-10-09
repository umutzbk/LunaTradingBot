import dataclasses

import numpy as np
import pandas as pd
import pytest

from luna.engine.backtest import default_instruments
from luna.execution.costs import CostModel
from luna.planner import plan_entry, plan_exit
from luna.portfolio.constructor import construct
from luna.portfolio.portfolio import Portfolio, Position
from luna.types import Candidate, ExitSpec, Leg, StopSpec, default_order_model
from luna.v2.fills import maker_fill_rate, missed_fills
from luna.v2.strategies import make_tsmom
from tests.v2.equivalence import assert_equivalent

TS = pd.Timestamp("2024-03-01 12:00", tz="UTC")


def _no_wicks(df):
    """Bars whose range is exactly open..close: post-only orders fill only when the price moves
    through the limit, so some maker entries miss."""
    out = df.copy()
    out["high"] = out[["open", "close"]].max(axis=1)
    out["low"] = out[["open", "close"]].min(axis=1)
    return out


def _cand(d=1):
    return Candidate(
        TS,
        "tsmom_1h",
        "BTC",
        (Leg("BTC", 1.0),),
        d,
        100.0,
        StopSpec("price", 98.0, 0.02),
        ExitSpec(100),
        "NONE",
        "test",
        {},
    )


def _setup(settings, kind):
    inst = default_instruments(["BTC"], kind, settings)
    return inst, CostModel(settings.costs[kind]), settings.accounts[kind].max_price_band


def test_default_order_model():
    assert default_order_model("perp") == "taker" and default_order_model("spot") == "maker"


@pytest.mark.parametrize("kind", ["perp", "spot"])
def test_default_order_model_reproduces_v1_orders(settings, kind):
    inst, costs, band = _setup(settings, kind)
    a = plan_entry(_cand(), 50.0, {"BTC": 100.0}, inst, kind, TS, band, costs=costs).orders
    b = plan_entry(
        _cand(),
        50.0,
        {"BTC": 100.0},
        inst,
        kind,
        TS,
        band,
        costs=costs,
        order_model=default_order_model(kind),
    ).orders
    assert a == b


def test_perp_maker_orders_are_post_only(settings):
    inst, costs, band = _setup(settings, "perp")
    o = plan_entry(
        _cand(), 50.0, {"BTC": 100.0}, inst, "perp", TS, band, costs=costs, order_model="maker"
    ).orders[0]
    assert o.tif == "POST_ONLY" and o.limit_price == pytest.approx(100.0 * (1 - 1e-4))


def test_spot_taker_orders_are_ioc_within_band(settings):
    inst, costs, band = _setup(settings, "spot")
    o = plan_entry(
        _cand(), 50.0, {"BTC": 100.0}, inst, "spot", TS, band, costs=costs, order_model="taker"
    ).orders[0]
    assert o.tif == "IOC" and o.limit_price == pytest.approx(100.0 * (1 + 0.005))
    pos = Position(
        "p",
        "tsmom_1h",
        "BTC",
        (Leg("BTC", 1.0),),
        1,
        {"BTC": 0.5},
        {"BTC": 100.0},
        TS,
        100.0,
        StopSpec("price", 98.0, 0.02),
        ExitSpec(100),
        1.0,
        "NONE",
    )
    x = plan_exit(pos, {"BTC": 100.0}, inst, "spot", TS, band, costs=costs, order_model="taker").orders[0]
    assert x.tif == "IOC" and x.limit_price == pytest.approx(100.0 * (1 - 0.005))


def test_constructor_round_trip_follows_order_model(settings):
    inst, costs, _ = _setup(settings, "perp")
    pf = Portfolio("perp", 265.0)
    lim = settings.accounts["perp"]
    args = ([_cand()], pf, lim, costs, inst, {"BTC": 100.0}, pd.DataFrame(), lambda c: 1.0)
    taker = construct(*args)[0][0]
    maker = construct(*args, liquidity="maker")[0][0]
    risk = lim.risk_per_trade * 265.0
    assert taker.gross_notional == pytest.approx(risk / (0.02 + costs.round_trip_frac("BTC", "taker")))
    assert maker.gross_notional == pytest.approx(risk / (0.02 + costs.round_trip_frac("BTC", "maker")))


@pytest.mark.parametrize(("tf", "kind", "model"), [("1h", "perp", "maker"), ("1h", "spot", "taker")])
def test_tradelist_matches_backtest_alt_order_model(v2_panel, settings, tf, kind, model):
    T = make_tsmom(tf)
    panel = {"BTC": _no_wicks(v2_panel["BTC"])}
    res, tl = assert_equivalent(T(T.GRID[0]), panel, "BTC", kind, settings, min_trades=5, order_model=model)
    if model == "maker":
        assert "unfilled" in set(res.candidates["status"])


def test_maker_fill_rate():
    st = pd.DataFrame({"status": ["filled", "unfilled", "partial", "rejected"]})
    assert maker_fill_rate(st) == pytest.approx(2 / 3)
    assert np.isnan(maker_fill_rate(pd.DataFrame({"status": ["rejected"]})))


def test_missed_fill_replay_uses_taker(v2_panel, settings):
    from luna.engine.backtest import run_backtest
    from luna.engine.walkforward import Window, WindowModel
    from luna.ranking.ranker import AcceptAllRanker
    from luna.research.pairs_select import PairSelection
    from luna.research.tradelists import build_tradelist
    from tests.v2.equivalence import END, START, engine_settings

    T = make_tsmom("1h")
    strat = T(T.GRID[0])
    s = engine_settings(settings, "perp")
    sub = {"BTC": _no_wicks(v2_panel["BTC"])}
    res = run_backtest(
        sub,
        {},
        [strat],
        {strat.name: ["BTC"]},
        AcceptAllRanker(),
        "perp",
        s,
        START,
        END,
        instruments=default_instruments(["BTC"], "perp", s),
        order_model="maker",
    )
    unfilled = res.candidates[res.candidates.status == "unfilled"]
    assert len(unfilled)
    idx = sub["BTC"].index
    w = Window(idx[0], idx[0], idx[0], idx[-1] + pd.Timedelta("15min"))
    model = WindowModel(
        w,
        ["BTC"],
        PairSelection(0, 0, [], [], [], pd.DataFrame()),
        {strat.name: strat.params_id},
        {},
        "",
        AcceptAllRanker(),
        [strat],
        {},
    )
    out = missed_fills(res, [model], sub, {}, s, "perp")
    assert len(out) == len(unfilled)
    assert list(out.columns) == ["ts", "strategy", "key", "direction", "r_net_taker", "exit_reason"]
    row = out.dropna(subset=["r_net_taker"]).iloc[0]
    sf = strat.signals(sub, ["BTC"])["BTC"]
    entry = pd.Series(0, index=sf.entry.index, dtype="int8")
    entry.loc[row.ts] = int(row.direction)
    tl = build_tradelist(
        dataclasses.replace(sf, entry=entry),
        sub,
        {},
        CostModel(s.costs["perp"]),
        "perp",
        strategy=strat,
        band=s.accounts["perp"].max_price_band,
        order_model="taker",
    )
    assert row.r_net_taker == pytest.approx(float(tl.iloc[0]["r_net"]))
