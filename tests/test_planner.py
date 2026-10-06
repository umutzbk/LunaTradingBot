import pandas as pd
import pytest

from luna.config import CostConfig
from luna.execution.costs import CostModel
from luna.planner import plan_entry, plan_exit, round_qty
from luna.portfolio.portfolio import Portfolio, Position
from luna.types import Candidate, ExitSpec, Fill, Instrument, Leg, StopSpec

TS = pd.Timestamp("2026-01-01 00:00", tz="UTC")


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
    res = plan_entry(cand(), 180.0, mids, instruments(), "perp", TS, 0.01)
    assert res.reject_reason is None
    o = {x.asset: x for x in res.orders}
    assert o["ETH"].side == "buy" and o["ETH"].qty * 100 == pytest.approx(100.0)
    assert o["BTC"].side == "sell" and o["BTC"].qty * 80 == pytest.approx(80.0)
    assert o["ETH"].tif == "IOC" and o["ETH"].limit_price == pytest.approx(101.0)
    assert o["BTC"].limit_price == pytest.approx(80 * 0.99)
    assert o["ETH"].intent == "open"
    assert o["ETH"].position_id == o["BTC"].position_id
    assert o["ETH"].leg_group == o["BTC"].leg_group
    assert o["ETH"].client_id == f"{o['ETH'].position_id}:entry:ETH:open:202601010000"
    short = plan_entry(cand(direction=-1), 180.0, mids, instruments(), "perp", TS, 0.01)
    assert {x.asset: x.side for x in short.orders} == {"ETH": "sell", "BTC": "buy"}


def test_plan_entry_rounds_to_step():
    one = cand(legs=(Leg("ETH", 1.0),))
    out = plan_entry(one, 100.0, {"ETH": 3.0}, instruments(step=0.5), "perp", TS, 0.01)
    assert out.orders[0].qty == 33.0  # 33.33 -> 33.0 (toward zero)
    # legs rounding to 0 or under min_qty are dropped
    assert plan_entry(one, 0.05, {"ETH": 100.0}, instruments(), "perp", TS, 0.01).orders == []
    small = instruments(min_qty=5.0)
    assert plan_entry(one, 100.0, {"ETH": 100.0}, small, "perp", TS, 0.01).orders == []


def test_plan_entry_spot_post_only_limit():
    costs = CostModel(
        CostConfig(maker_fee=0.0, taker_fee=0.0, half_spread_bps={"default": 10}, slippage_bps=0)
    )
    c = cand(legs=(Leg("ETH", 1.0),))
    (o,) = plan_entry(c, 100.0, {"ETH": 100.0}, instruments(), "spot", TS, 0.01, costs=costs).orders
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
    plan = plan_exit(pos, mids, instruments(), "perp", TS, 0.01)
    assert plan.dust == {}
    o = {x.asset: x for x in plan.orders}
    assert o["ETH"].side == "sell" and o["ETH"].qty == 1.0
    assert o["BTC"].side == "buy" and o["BTC"].qty == 1.25
    assert all(x.intent == "close" and x.position_id == "p1" for x in o.values())


def _spot_costs():
    return CostModel(
        CostConfig(maker_fee=0.0, taker_fee=0.0, half_spread_bps={"default": 10}, slippage_bps=0)
    )


def test_plan_entry_all_or_nothing_when_one_leg_too_small():
    inst = instruments()
    inst["BTC"] = Instrument("BTC", "BTC", "kraken_futures", "perp", 0.001, 0.1, 0.001, 0.0, 0.0002, 0.0005)
    mids = {"ETH": 100.0, "BTC": 80.0}
    # BTC leg notional = 0.2*0.8/1.8... tiny gross makes BTC qty 0.0005 < min_qty
    res = plan_entry(cand(), 0.1, mids, inst, "perp", TS, 0.01)
    assert res.orders == [] and res.reject_reason == "below_min_notional"
    # a leg below min_notional (but above min_qty) also rejects the whole pair
    inst["BTC"] = Instrument("BTC", "BTC", "kraken_futures", "perp", 0.001, 0.1, 0.001, 90.0, 0.0002, 0.0005)
    res = plan_entry(cand(), 180.0, mids, inst, "perp", TS, 0.01)
    assert res.orders == [] and res.reject_reason == "below_min_notional"


def test_plan_entry_spot_sell_limit_and_costs_keyword_only():
    costs = _spot_costs()
    c = cand(legs=(Leg("ETH", 1.0),), direction=-1)
    (o,) = plan_entry(c, 100.0, {"ETH": 100.0}, instruments(), "spot", TS, 0.01, costs=costs).orders
    assert o.side == "sell" and o.limit_price == pytest.approx(100 * 1.001)
    with pytest.raises(TypeError):
        plan_entry(c, 100.0, {"ETH": 100.0}, instruments(), "spot", TS, 0.01, costs)  # type: ignore[misc]


def test_invalid_inputs_raise():
    one = cand(legs=(Leg("ETH", 1.0),))
    for bad in (0.0, -1.0, float("nan"), float("inf")):
        with pytest.raises(ValueError):
            plan_entry(one, 100.0, {"ETH": bad}, instruments(), "perp", TS, 0.01)
    with pytest.raises(ValueError):
        plan_entry(one, 100.0, {}, instruments(), "perp", TS, 0.01)
    with pytest.raises(ValueError):
        round_qty(1.0, 0.0)


def _pos(qty):
    return Position(
        "p1", "pairs", "ETH-BTC", (Leg("ETH", 1.0), Leg("BTC", -0.8)), 1,
        qty, {}, TS, 0.0, StopSpec("price", 1.0, 0.02), ExitSpec(10), 1.0, "range",
    )


def test_plan_exit_spot_uses_post_only_sell():
    pos = _pos({"ETH": 1.0})
    plan = plan_exit(pos, {"ETH": 100.0}, instruments(), "spot", TS, 0.01, costs=_spot_costs())
    (o,) = plan.orders
    assert o.tif == "POST_ONLY" and o.side == "sell" and o.limit_price == pytest.approx(100.1)
    assert o.intent == "close"


@pytest.mark.parametrize("residue", [5.5e-17, 0.0004])
def test_plan_exit_reports_dust_and_position_can_close(residue):
    pf = Portfolio("perp", 1000.0)
    pos = _pos({})
    pf.open_position(pos)
    pf.apply_fill(Fill("c", TS, "ETH", "buy", residue, 100.0, 0.0, "taker"), "p1", 100.0)
    pf.apply_fill(Fill("c2", TS, "BTC", "sell", 1.0, 80.0, 0.0, "taker"), "p1", 80.0)
    start = 1000.0
    plan = plan_exit(pos, {"ETH": 100.0, "BTC": 80.0}, instruments(), "perp", TS, 0.01)
    assert plan.dust == {"ETH": pytest.approx(residue)}
    assert [o.asset for o in plan.orders] == ["BTC"]
    pf.apply_fill(Fill("c3", TS, "BTC", "buy", 1.0, 80.0, 0.0, "taker"), "p1", 80.0)
    pf.write_off_dust("p1", "ETH", 101.0, TS)
    pf.close_position("p1", TS, "exit")
    pf.mark({"ETH": 101.0, "BTC": 80.0})
    assert pos.net_pnl() == pytest.approx(pf.equity() - start)
