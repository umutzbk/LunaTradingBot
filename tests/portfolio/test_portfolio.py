import numpy as np
import pandas as pd
import pytest

from luna.portfolio.portfolio import Portfolio, Position
from luna.types import ExitSpec, Fill, Leg, StopSpec

TS = pd.Timestamp("2024-01-01 00:00", tz="UTC")


def _pos(pid: str, assets=("BTC",), direction: int = 1, risk: float = 2.0) -> Position:
    return Position(
        id=pid,
        strategy="trend",
        key=pid,
        legs=tuple(Leg(a, 1.0) for a in assets),
        direction=direction,
        qty={},
        entry_price={},
        entry_ts=TS,
        entry_ref=100.0,
        stop=StopSpec("price", 95.0, 0.05),
        exit=ExitSpec(max_hold_bars=10),
        risk_amount=risk,
        regime="trend",
    )


def _fill(asset, side, qty, price, fee=0.0, i=0) -> Fill:
    return Fill(f"c{i}", TS, asset, side, qty, price, fee, "taker")


def _perp(*pids, capital=1000.0, **kw) -> Portfolio:
    pf = Portfolio("perp", capital)
    for pid in pids:
        pf.open_position(_pos(pid, **kw))
    return pf


# --- perp -------------------------------------------------------------------------------------


def test_perp_long_then_close_realizes_pnl():
    pf = _perp("p1")
    pf.apply_fill(_fill("BTC", "buy", 1, 100.0, fee=0.05), "p1", ref_price=100.0)
    assert pf.cash == pytest.approx(1000.0 - 0.05)  # opening moves cash by the fee only
    assert pf.holdings["BTC"] == pytest.approx(1.0)
    pf.mark({"BTC": 105.0})
    assert pf.equity() == pytest.approx(1000.0 - 0.05 + 5.0)
    pf.apply_fill(_fill("BTC", "sell", 1, 110.0, fee=0.055), "p1", ref_price=110.0)
    assert pf.cash == pytest.approx(1000.0 + 10.0 - 0.05 - 0.055)
    assert pf.holdings.get("BTC", 0.0) == 0.0
    pf.mark({"BTC": 120.0})
    assert pf.equity() == pytest.approx(pf.cash)  # flat: nothing unrealized
    pos = pf.positions["p1"]
    assert pos.pnl["gross"] == pytest.approx(10.0)
    assert pos.pnl["fees"] == pytest.approx(0.105)
    assert pos.net_pnl() == pytest.approx(10.0 - 0.105)


def test_perp_short_pnl_sign():
    pf = _perp("p1", direction=-1)
    pf.apply_fill(_fill("BTC", "sell", 2, 100.0, fee=0.1), "p1", 100.0)
    assert pf.holdings["BTC"] == pytest.approx(-2.0)
    pf.mark({"BTC": 90.0})
    assert pf.equity() == pytest.approx(1000.0 - 0.1 + 20.0)
    pf.mark({"BTC": 110.0})
    assert pf.equity() == pytest.approx(1000.0 - 0.1 - 20.0)
    pf.apply_fill(_fill("BTC", "buy", 2, 95.0, fee=0.1), "p1", 95.0)
    assert pf.cash == pytest.approx(1000.0 + 10.0 - 0.2)
    assert pf.positions["p1"].pnl["gross"] == pytest.approx(10.0)


def test_partial_close_and_sign_flip():
    pf = _perp("p1")
    pf.apply_fill(_fill("BTC", "buy", 2, 100.0), "p1", 100.0)
    # long 2, sell 3 -> closes 2 at avg 100 (realize -20), opens short 1 @ 90
    pf.apply_fill(_fill("BTC", "sell", 3, 90.0), "p1", 90.0)
    assert pf.cash == pytest.approx(1000.0 - 20.0)
    assert pf.holdings["BTC"] == pytest.approx(-1.0)
    assert pf.avg_entry["BTC"] == pytest.approx(90.0)
    pos = pf.positions["p1"]
    assert pos.qty["BTC"] == pytest.approx(-1.0)
    assert pos.entry_price["BTC"] == pytest.approx(90.0)
    assert pos.pnl["gross"] == pytest.approx(-20.0)
    pf.mark({"BTC": 85.0})
    assert pf.equity() == pytest.approx(980.0 + 5.0)


def test_partial_close_keeps_avg_entry():
    pf = _perp("p1")
    pf.apply_fill(_fill("BTC", "buy", 1, 100.0), "p1", 100.0)
    pf.apply_fill(_fill("BTC", "buy", 1, 110.0), "p1", 110.0)
    assert pf.avg_entry["BTC"] == pytest.approx(105.0)
    assert pf.positions["p1"].entry_price["BTC"] == pytest.approx(105.0)
    pf.apply_fill(_fill("BTC", "sell", 1, 115.0), "p1", 115.0)  # realizes (115-105)*1
    assert pf.cash == pytest.approx(1010.0)
    assert pf.holdings["BTC"] == pytest.approx(1.0)
    assert pf.avg_entry["BTC"] == pytest.approx(105.0)
    pf.mark({"BTC": 118.0})
    assert pf.equity() == pytest.approx(1010.0 + 13.0)


def test_round_trip_identity_net_equals_equity_change_perp():
    """Round trip with adverse fills, fees and funding: position net == account equity change."""
    pf = _perp("p1")
    eq0 = pf.equity()
    pf.apply_fill(_fill("BTC", "buy", 2, 100.3, fee=0.1), "p1", ref_price=100.0)
    pf.apply_funding("BTC", 0.0001, mark=101.0, ts=TS)
    pf.apply_fill(_fill("BTC", "sell", 1, 104.8, fee=0.05, i=1), "p1", ref_price=105.0)
    pf.apply_funding("BTC", -0.0002, mark=103.0, ts=TS)
    pf.apply_fill(_fill("BTC", "sell", 1, 107.9, fee=0.05, i=2), "p1", ref_price=108.0)
    pos = pf.positions["p1"]
    assert pos.pnl["gross"] == pytest.approx((105.0 - 100.0) + (108.0 - 100.0))
    assert pos.pnl["spread_slip"] == pytest.approx(0.3 * 2 + 0.2 + 0.1)
    assert pos.pnl["fees"] == pytest.approx(0.2)
    assert pos.pnl["funding"] == pytest.approx(-0.0001 * 2 * 101.0 + 0.0002 * 1 * 103.0)
    pf.mark({"BTC": 50.0})
    assert pos.net_pnl() == pytest.approx(pf.equity() - eq0)
    closed = pf.close_position("p1", TS, "tp")
    assert closed is pos and "p1" not in pf.positions
    assert pf.closed == [(pos, TS, "tp")]


def test_favourable_fill_reduces_spread_slip():
    pf = _perp("p1")
    pf.apply_fill(_fill("BTC", "buy", 1, 99.9), "p1", ref_price=100.0)
    pf.apply_fill(_fill("BTC", "sell", 1, 100.0), "p1", ref_price=100.0)
    pos = pf.positions["p1"]
    assert pos.pnl["spread_slip"] == pytest.approx(-0.1)
    assert pos.net_pnl() == pytest.approx(pf.equity() - 1000.0)


def test_two_positions_same_asset_attribution():
    pf = _perp("a", "b")
    pf.apply_fill(_fill("BTC", "buy", 1, 100.0), "a", 100.0)
    pf.apply_fill(_fill("BTC", "buy", 3, 120.0), "b", 120.0)
    assert pf.holdings["BTC"] == pytest.approx(4.0)
    assert pf.avg_entry["BTC"] == pytest.approx(115.0)
    pf.apply_funding("BTC", 0.001, mark=100.0, ts=TS)
    assert pf.positions["a"].pnl["funding"] == pytest.approx(-0.1)
    assert pf.positions["b"].pnl["funding"] == pytest.approx(-0.3)
    pf.apply_fill(_fill("BTC", "sell", 1, 130.0), "a", 130.0)
    pf.apply_fill(_fill("BTC", "sell", 3, 130.0), "b", 130.0)
    a, b = pf.positions["a"], pf.positions["b"]
    assert a.pnl["gross"] == pytest.approx(30.0)
    assert b.pnl["gross"] == pytest.approx(30.0)
    assert a.net_pnl() + b.net_pnl() == pytest.approx(pf.equity() - 1000.0)


def test_funding_long_pays_short_receives():
    pl = _perp("L")
    pl.apply_fill(_fill("BTC", "buy", 2, 100.0), "L", 100.0)
    pl.apply_funding("BTC", 0.0001, mark=100.0, ts=TS)
    assert pl.cash == pytest.approx(1000.0 - 0.0001 * 2 * 100.0)
    assert pl.positions["L"].pnl["funding"] == pytest.approx(-0.02)

    ps = _perp("S", direction=-1)
    ps.apply_fill(_fill("BTC", "sell", 2, 100.0), "S", 100.0)
    ps.apply_funding("BTC", 0.0001, mark=100.0, ts=TS)
    assert ps.cash == pytest.approx(1000.0 + 0.02)
    assert ps.positions["S"].pnl["funding"] == pytest.approx(0.02)
    ps.apply_funding("BTC", -0.0001, mark=100.0, ts=TS)  # negative rate: short pays
    assert ps.cash == pytest.approx(1000.0)
    assert len(ps.funding_log) == 2


def test_funding_unknown_counts_without_cash():
    pf = _perp("p1", "idle")
    pf.apply_fill(_fill("BTC", "buy", 1, 100.0), "p1", 100.0)
    pf.apply_funding("BTC", None, mark=100.0, ts=TS)
    pf.apply_funding("BTC", None, mark=101.0, ts=TS + pd.Timedelta("1h"))
    assert pf.cash == pytest.approx(1000.0)
    assert pf.positions["p1"].pnl["funding_unknown_bars"] == 2
    assert pf.positions["p1"].pnl["funding"] == 0.0
    assert pf.positions["idle"].pnl["funding_unknown_bars"] == 0  # holds no BTC


def test_funding_offsetting_positions_each_book_their_share():
    pf = _perp("L", "S")
    pf.apply_fill(_fill("BTC", "buy", 1, 100.0), "L", 100.0)
    pf.apply_fill(_fill("BTC", "sell", 1, 100.0), "S", 100.0)
    assert "BTC" not in pf.holdings  # account net flat
    cash0 = pf.cash
    pf.apply_funding("BTC", 0.001, mark=100.0, ts=TS)
    fl, fs = pf.positions["L"].pnl["funding"], pf.positions["S"].pnl["funding"]
    assert fl == pytest.approx(-0.1)
    assert fs == pytest.approx(0.1)
    assert pf.cash - cash0 == pytest.approx(fl + fs)
    # continuity with an almost-offsetting short
    pf2 = _perp("L", "S")
    pf2.apply_fill(_fill("BTC", "buy", 1, 100.0), "L", 100.0)
    pf2.apply_fill(_fill("BTC", "sell", 0.999, 100.0), "S", 100.0)
    pf2.apply_funding("BTC", 0.001, mark=100.0, ts=TS)
    assert pf2.positions["L"].pnl["funding"] == pytest.approx(-0.1)
    assert pf2.positions["S"].pnl["funding"] == pytest.approx(0.0999)


def test_round_trip_identity_sign_flip_with_costs_and_funding():
    pf = _perp("p1")
    pf.apply_fill(_fill("BTC", "buy", 2, 100.4, fee=0.1), "p1", ref_price=100.0)
    pf.apply_fill(_fill("BTC", "sell", 3, 89.7, fee=0.13, i=1), "p1", ref_price=90.0)
    assert pf.holdings["BTC"] == pytest.approx(-1.0)
    pf.apply_funding("BTC", 0.0005, mark=88.0, ts=TS)  # short receives
    pf.apply_fill(_fill("BTC", "buy", 1, 85.2, fee=0.04, i=2), "p1", ref_price=85.0)
    pos = pf.positions["p1"]
    assert pos.is_flat()
    assert pos.pnl["gross"] == pytest.approx((90.0 - 100.0) * 2 + (90.0 - 85.0))
    assert pos.pnl["spread_slip"] == pytest.approx(0.4 * 2 + 0.3 * 3 + 0.2)
    assert pos.pnl["funding"] == pytest.approx(0.0005 * 88.0)
    pf.mark({"BTC": 70.0})
    assert pos.net_pnl() == pytest.approx(pf.equity() - 1000.0)


def test_mark_and_fill_reject_non_finite():
    pf = _perp("p1")
    for bad in (float("nan"), float("inf"), 0.0, -1.0):
        with pytest.raises(ValueError):
            pf.mark({"BTC": bad})
    for qty, price in ((float("inf"), 100.0), (1.0, float("inf")), (float("nan"), 100.0)):
        with pytest.raises(ValueError):
            pf.apply_fill(_fill("BTC", "buy", qty, price), "p1", 100.0)
    with pytest.raises(ValueError):
        pf.apply_fill(_fill("BTC", "buy", 1.0, 100.0), "p1", float("nan"))
    assert pf.cash == 1000.0 and pf.holdings == {} and pf.marks == {}


def test_funding_rejected_on_spot():
    pf = Portfolio("spot", 1000.0)
    with pytest.raises(ValueError):
        pf.apply_funding("BTC", 0.0001, mark=100.0, ts=TS)


def test_gross_leverage():
    pf = _perp("p1", "p2", assets=("BTC", "ETH"))
    pf.apply_fill(_fill("BTC", "buy", 10, 100.0), "p1", 100.0)
    pf.apply_fill(_fill("ETH", "sell", 50, 20.0), "p2", 20.0)
    pf.mark({"BTC": 100.0, "ETH": 20.0})
    assert pf.gross_notional() == pytest.approx(2000.0)
    assert pf.gross_leverage() == pytest.approx(2.0)
    pf.mark({"BTC": 110.0})  # equity 1100, notional 1100 + 1000
    assert pf.gross_leverage() == pytest.approx(2100.0 / 1100.0)


def test_open_risk_sums_open_positions():
    pf = _perp("a", "b", risk=2.5)
    assert pf.open_risk() == pytest.approx(5.0)
    pf.close_position("a", TS, "manual")
    assert pf.open_risk() == pytest.approx(2.5)


def test_liquidation_distance():
    pf = _perp("p1", "p2", assets=("BTC", "ETH"))
    assert pf.liquidation_distance("BTC", 0.01) == np.inf
    pf.apply_fill(_fill("BTC", "buy", 10, 100.0), "p1", 100.0)
    pf.apply_fill(_fill("ETH", "sell", 50, 20.0), "p2", 20.0)
    pf.mark({"BTC": 100.0, "ETH": 20.0})
    # (1000 - 2000*0.02) / 1000
    assert pf.liquidation_distance("BTC", 0.02) == pytest.approx(0.96)
    assert pf.liquidation_distance("SOL", 0.02) == np.inf


def test_close_position_requires_flat_legs():
    pf = _perp("p1")
    pf.apply_fill(_fill("BTC", "buy", 1, 100.0), "p1", 100.0)
    with pytest.raises(ValueError):
        pf.close_position("p1", TS, "x")
    pf.apply_fill(_fill("BTC", "sell", 1, 100.0), "p1", 100.0)
    pf.close_position("p1", TS, "x")
    with pytest.raises(KeyError):
        pf.close_position("p1", TS, "x")


def test_float_dust_is_flat():
    pf = _perp("p1")
    pf.apply_fill(_fill("BTC", "buy", 0.1, 100.0), "p1", 100.0)
    pf.apply_fill(_fill("BTC", "buy", 0.2, 100.0), "p1", 100.0)
    pf.apply_fill(_fill("BTC", "sell", 0.3, 100.0), "p1", 100.0)
    assert "BTC" not in pf.holdings and "BTC" not in pf.avg_entry
    pf.close_position("p1", TS, "x")


def test_fill_validation():
    pf = _perp("p1")
    with pytest.raises(KeyError):
        pf.apply_fill(_fill("BTC", "buy", 1, 100.0), "nope", 100.0)
    with pytest.raises(ValueError):
        pf.apply_fill(_fill("ETH", "buy", 1, 100.0), "p1", 100.0)  # not a leg of p1
    with pytest.raises(ValueError):
        pf.apply_fill(_fill("BTC", "buy", 0, 100.0), "p1", 100.0)
    with pytest.raises(ValueError):
        pf.open_position(_pos("p1"))  # duplicate id
    assert pf.cash == 1000.0 and pf.holdings == {}


# --- spot -------------------------------------------------------------------------------------


def _spot(pid: str = "p1") -> Portfolio:
    pf = Portfolio("spot", 1000.0)
    pf.open_position(_pos(pid))
    return pf


def test_spot_cannot_go_negative():
    pf = _spot()
    pf.apply_fill(_fill("BTC", "buy", 1, 100.0, fee=0.4), "p1", 100.0)
    assert pf.cash == pytest.approx(1000.0 - 100.0 - 0.4)
    cash_before = pf.cash
    with pytest.raises(ValueError):
        pf.apply_fill(_fill("BTC", "sell", 1.5, 100.0), "p1", 100.0)
    assert pf.cash == cash_before and pf.holdings["BTC"] == pytest.approx(1.0)  # no partial mutation
    pf.apply_fill(_fill("BTC", "sell", 1, 110.0, fee=0.44), "p1", 110.0)
    assert pf.cash == pytest.approx(1000.0 + 10.0 - 0.84)
    assert pf.holdings.get("BTC", 0.0) == 0.0


def test_spot_sell_without_holding_raises():
    pf = _spot()
    with pytest.raises(ValueError):
        pf.apply_fill(_fill("BTC", "sell", 1, 100.0), "p1", 100.0)
    assert pf.cash == 1000.0


def test_spot_equity_marks_holdings():
    pf = _spot()
    pf.apply_fill(_fill("BTC", "buy", 2, 100.0, fee=0.8), "p1", 100.0)
    pf.mark({"BTC": 120.0})
    assert pf.equity() == pytest.approx(1000.0 - 200.8 + 240.0)
    assert pf.gross_notional() == pytest.approx(240.0)
    assert pf.liquidation_distance("BTC", 0.01) == np.inf


def test_round_trip_identity_spot():
    pf = _spot()
    pf.apply_fill(_fill("BTC", "buy", 2, 99.95, fee=0.8), "p1", ref_price=100.0)
    pf.apply_fill(_fill("BTC", "sell", 2, 104.9, fee=0.84), "p1", ref_price=105.0)
    pos = pf.positions["p1"]
    assert pos.pnl["gross"] == pytest.approx(10.0)
    assert pos.pnl["spread_slip"] == pytest.approx(-0.1 + 0.2)
    assert pos.net_pnl() == pytest.approx(pf.equity() - 1000.0)
