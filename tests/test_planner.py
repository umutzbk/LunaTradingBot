import pandas as pd
import pytest

from luna.config import CostConfig
from luna.execution.costs import CostModel
from luna.planner import plan_entry, plan_exit, round_qty
from luna.portfolio.portfolio import Position
from luna.types import Candidate, ExitSpec, Instrument, Leg, StopSpec

TS = pd.Timestamp("2026-01-01 00:00")


def instruments(step=0.001, min_qty=0.001):
    return {
        a: Instrument(a, a, "kraken_futures", "perp", step, 0.1, min_qty, 0.0, 0.0002, 0.0005)
        for a in ("ETH", "BTC")
    }


def cand(legs=None, direction=1):
    legs = legs or (Leg("ETH", 1.0), Leg("BTC", -0.8))
    return Candidate(
        TS, "pairs", "ETH-BTC", legs, direction, 0.0,
        StopSpec("price", 1.0, 0.02), ExitSpec(10), "range", "t", {},
    )


def test_round_qty_toward_zero():
    assert round_qty(1.2349, 0.01) == pytest.approx(1.23)
    assert round_qty(-1.2399, 0.01) == pytest.approx(-1.23)
    assert round_qty(0.3, 0.1) == pytest.approx(0.3)  # float noise safe
    assert round_qty(0.004, 0.01) == 0.0


def test_plan_entry_pairs_weights():
    mids = {"ETH": 100.0, "BTC": 80.0}
    o = {x.asset: x for x in plan_entry(cand(), 180.0, mids, instruments(), "perp", TS, 0.01)}
    assert o["ETH"].side == "buy" and o["ETH"].qty * 100 == pytest.approx(100.0)
    assert o["BTC"].side == "sell" and o["BTC"].qty * 80 == pytest.approx(80.0)
    assert o["ETH"].tif == "IOC" and o["ETH"].limit_price == pytest.approx(101.0)
    assert o["BTC"].limit_price == pytest.approx(80 * 0.99)
    assert o["ETH"].intent == "open"
    assert o["ETH"].position_id == o["BTC"].position_id
    assert o["ETH"].leg_group == o["BTC"].leg_group
    assert o["ETH"].client_id == f"{o['ETH'].position_id}:entry:ETH:open:202601010000"
    short = plan_entry(cand(direction=-1), 180.0, mids, instruments(), "perp", TS, 0.01)
    assert {x.asset: x.side for x in short} == {"ETH": "sell", "BTC": "buy"}


def test_plan_entry_rounds_to_step():
    one = cand(legs=(Leg("ETH", 1.0),))
    out = plan_entry(one, 100.0, {"ETH": 3.0}, instruments(step=0.5), "perp", TS, 0.01)
    assert out[0].qty == 33.0  # 33.33 -> 33.0 (toward zero)
    # legs rounding to 0 or under min_qty are dropped
    assert plan_entry(one, 0.05, {"ETH": 100.0}, instruments(), "perp", TS, 0.01) == []
    small = instruments(min_qty=5.0)
    assert plan_entry(one, 100.0, {"ETH": 100.0}, small, "perp", TS, 0.01) == []


def test_plan_entry_spot_post_only_limit():
    costs = CostModel(
        CostConfig(maker_fee=0.0, taker_fee=0.0, half_spread_bps={"default": 10}, slippage_bps=0)
    )
    c = cand(legs=(Leg("ETH", 1.0),))
    (o,) = plan_entry(c, 100.0, {"ETH": 100.0}, instruments(), "spot", TS, 0.01, costs=costs)
    assert o.tif == "POST_ONLY" and o.limit_price == pytest.approx(100 * 0.999)
    with pytest.raises(ValueError):
        plan_entry(c, 100.0, {"ETH": 100.0}, instruments(), "spot", TS, 0.01)


def test_plan_exit_flattens_all_legs():
    pos = Position(
        "p1", "pairs", "ETH-BTC", (Leg("ETH", 1.0), Leg("BTC", -0.8)), 1,
        {"ETH": 1.0, "BTC": -1.25}, {"ETH": 100.0, "BTC": 80.0}, TS, 0.0,
        StopSpec("price", 1.0, 0.02), ExitSpec(10), 1.0, "range",
    )
    mids = {"ETH": 100.0, "BTC": 80.0}
    o = {x.asset: x for x in plan_exit(pos, mids, instruments(), "perp", TS, 0.01)}
    assert o["ETH"].side == "sell" and o["ETH"].qty == 1.0
    assert o["BTC"].side == "buy" and o["BTC"].qty == 1.25
    assert all(x.intent == "close" and x.position_id == "p1" for x in o.values())
