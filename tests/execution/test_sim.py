import pandas as pd
import pytest

from luna.config import CostConfig
from luna.execution.costs import CostModel
from luna.execution.sim import SimBroker
from luna.types import Instrument, OrderRequest

T0 = pd.Timestamp("2026-01-01 00:00", tz="UTC")
T1 = pd.Timestamp("2026-01-01 01:00", tz="UTC")
T2 = pd.Timestamp("2026-01-01 02:00", tz="UTC")


def costs() -> CostModel:
    return CostModel(
        CostConfig(
            maker_fee=0.0002,
            taker_fee=0.0005,
            half_spread_bps={"default": 1},
            slippage_bps=2,
        )
    )


def inst(kind="perp") -> dict[str, Instrument]:
    return {
        "BTC": Instrument(
            "BTC", "PF_XBTUSD", "kraken_futures", kind, 0.001, 0.5, 0.001, 0.0, 0.0002, 0.0005
        )
    }


def bar(o=100.0, h=101.0, low=99.0, c=100.0, filled=False, name=T1) -> pd.Series:
    return pd.Series({"open": o, "high": h, "low": low, "close": c, "filled": filled}, name=name)


def order(side="buy", limit=101.0, tif="IOC", ts=T0, qty=2.0) -> OrderRequest:
    return OrderRequest("c1", ts, "BTC", side, qty, limit, tif, "open", "p1", "entry")


def test_ioc_fills_at_next_open_with_costs():
    b = SimBroker("perp", costs(), inst())
    b.submit(order())
    (f,) = b.on_bar(T1, {"BTC": bar()})
    assert f.price == pytest.approx(100 * (1 + 0.0001 + 0.0002))
    assert f.fee == pytest.approx(f.qty * f.price * 0.0005)
    assert f.liquidity == "taker" and f.ts == T1
    assert b.on_bar(T2, {"BTC": bar()}) == []  # consumed


def test_sell_ioc_and_cost_mult():
    b = SimBroker("perp", costs(), inst(), cost_mult=2.0)
    b.submit(order(side="sell", limit=99.0))
    (f,) = b.on_bar(T1, {"BTC": bar()})
    assert f.price == pytest.approx(100 * (1 - 0.0006))
    assert f.fee == pytest.approx(f.qty * f.price * 0.001)


def test_ioc_rejected_outside_band():
    b = SimBroker("perp", costs(), inst())
    b.submit(order(limit=100.01))  # fill 100.03 > limit
    assert b.on_bar(T1, {"BTC": bar()}) == []
    assert b.on_bar(T2, {"BTC": bar()}) == []  # dropped, not retried
    b.submit(order(side="sell", limit=99.99, ts=T1))  # fill 99.97 < limit
    assert b.on_bar(T2, {"BTC": bar()}) == []


def test_not_eligible_on_decision_bar():
    b = SimBroker("perp", costs(), inst())
    b.submit(order(ts=T1))
    assert b.on_bar(T1, {"BTC": bar()}) == []  # same bar as decision
    assert len(b.on_bar(T2, {"BTC": bar()})) == 1  # next bar


def test_post_only_requires_trade_through():
    b = SimBroker("spot", costs(), inst("spot"))
    b.submit(order(limit=99.0, tif="POST_ONLY"))
    assert b.on_bar(T1, {"BTC": bar(low=99.0)}) == []  # touch only, no fill
    assert b.on_bar(T2, {"BTC": bar(low=98.0)}) == []  # cancelled after one bar
    b.submit(order(limit=99.0, tif="POST_ONLY", ts=T2))
    (f,) = b.on_bar(T2 + pd.Timedelta(hours=1), {"BTC": bar(low=98.9)})
    assert f.price == 99.0 and f.liquidity == "maker"
    assert f.fee == pytest.approx(f.qty * 99.0 * 0.0002)


def test_post_only_sell_through_high():
    b = SimBroker("spot", costs(), inst("spot"))
    b.submit(order(side="sell", limit=101.0, tif="POST_ONLY"))
    assert b.on_bar(T1, {"BTC": bar(h=101.0)}) == []
    b.submit(order(side="sell", limit=101.0, tif="POST_ONLY", ts=T1))
    (f,) = b.on_bar(T2, {"BTC": bar(h=101.1)})
    assert f.price == 101.0


def test_no_fill_on_filled_bar():
    b = SimBroker("perp", costs(), inst())
    b.submit(order())
    assert b.on_bar(T1, {"BTC": bar(filled=True)}) == []
    assert b.on_bar(T2, {"BTC": bar()}) == []


def test_stop_fill_gap_uses_open():
    b = SimBroker("perp", costs(), inst())
    f = b.stop_fill("BTC", "sell", 2.0, 95.0, bar(o=90.0), "p1")
    assert f.price == pytest.approx(90.0 * (1 - 0.0003))
    assert f.liquidity == "taker" and f.ts == T1
    f2 = b.stop_fill("BTC", "sell", 2.0, 95.0, bar(o=100.0), "p1")  # no gap: stop price
    assert f2.price == pytest.approx(95.0 * (1 - 0.0003))
    f3 = b.stop_fill("BTC", "buy", 2.0, 105.0, bar(o=110.0), "p1")
    assert f3.price == pytest.approx(110.0 * (1 + 0.0003))


def test_stop_fill_rejects_filled_or_unnamed_bar():
    b = SimBroker("perp", costs(), inst())
    with pytest.raises(ValueError):
        b.stop_fill("BTC", "sell", 2.0, 95.0, bar(filled=True), "p1")
    with pytest.raises(ValueError):
        b.stop_fill("BTC", "sell", 2.0, 95.0, bar(name=None), "p1")
    f = b.stop_fill("BTC", "sell", 2.0, 95.0, bar(), "p1")
    assert f.client_id == "p1:stop:BTC:202601010100"
