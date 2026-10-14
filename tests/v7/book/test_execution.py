"""Book execution (spec S-4.2, S-4.4, S-5.3, S-5.4, S-6.1, S-6.2, DEC-43, E10) on synthetic bars."""

import numpy as np
import pandas as pd
import pytest

from luna.v7.book.execution import (
    INVALID_TERMINAL,
    MAKER_WINDOW,
    ExecView,
    Fill,
    NoTrade,
    Pending,
    RunInvalid,
    Trade,
    forced_at,
    halted_exit,
    maker_at,
    settle,
    taker_at,
)
from luna.v7.data.instances import Instance
from luna.v7.data.terminal import TerminalEvent
from luna.v7.refs import restrict
from luna.v7.timeutil import MIN, H

from .engine_fixtures import END, F_M, F_T, HMIN, LO, S0, SLIP, b15, fund, m1

TE = LO + 8 * H + 20 * MIN
INST = Instance(0, S0, None, None)
E_REF = 10_000.0


def view(b1=None, trades=None, inst=INST, px=100.0) -> ExecView:
    bb = b15(px)
    x = restrict(b1 if b1 is not None else m1(px), bb, bb[["open_ts", "c"]], fund(1e-4, px), inst, END)
    return ExecView.build("AAAUSDT", x, trades)


def buy(qty=0.001, opening=True, reason="entry"):
    return Trade("AAAUSDT", 1, qty, None, opening, reason, "AAAUSDT#0@x")


def sell(qty=0.001, opening=False, reason="rank"):
    return Trade("AAAUSDT", -1, qty, None, opening, reason, "AAAUSDT#0@x")


def trades(rows):
    return pd.DataFrame(rows, columns=["ts", "price", "qty"]).astype({"ts": "int64"})


def test_taker_fill_prices_and_costs():
    f = taker_at(view(), buy(), TE)
    assert isinstance(f, Fill) and f.ts == TE and f.liquidity == "taker"
    assert f.ref_mid == 100.0
    assert f.price == pytest.approx(100.0 * (1 + HMIN + SLIP), rel=1e-12)
    assert f.fee == pytest.approx(F_T * 0.001 * f.price, rel=1e-12)
    assert f.spread_cost == pytest.approx(0.001 * 100 * HMIN, rel=1e-12)
    assert f.slip_cost == pytest.approx(0.001 * 100 * SLIP, rel=1e-12)
    s = taker_at(view(), sell(), TE)
    assert s.price == pytest.approx(100.0 * (1 - HMIN - SLIP), rel=1e-12)
    # an entry sized by notional: q = notional / P(t_e) (S-5.3)
    e = taker_at(view(), Trade("AAAUSDT", 1, None, 0.1, True, "entry", "p"), TE)
    assert e.qty == pytest.approx(0.1 / 100.0, rel=1e-12)


def test_maker_trade_through_only():
    """S-6.2: only trades strictly through L, ts in (t_e, t_e + 30 min], qty = min(remaining, cumulative)."""
    lim = 100.0 * (1 - HMIN)  # buy limit at the touch P(t_e) - h-hat(t)
    q_usd = 0.001 * E_REF  # order qty in base units at E_ref (10 base units)
    at_touch = trades([(TE + MIN, lim, 1e6), (TE + 2 * MIN, lim, 1e6)])
    outs = maker_at(view(trades=at_touch), buy(), TE, HMIN, E_REF)
    assert len(outs) == 1 and isinstance(outs[0], Fill)  # trades at the touch never fill
    assert outs[0].liquidity == "taker" and outs[0].ts == TE + MAKER_WINDOW
    assert outs[0].qty == pytest.approx(0.001, rel=1e-12)

    rows = [
        (TE, lim - 0.01, 1e6),  # ts == t_e: not > t_e
        (TE + MIN, lim, 1e6),  # at the touch
        (TE + 3 * MIN, lim - 0.01, 4.0),  # through: 4 units
        (TE + 5 * MIN, lim + 0.01, 1e6),  # above a buy limit
        (TE + 7 * MIN, lim - 0.02, 3.0),  # through: 3 units
        (TE + MAKER_WINDOW + 1, lim - 0.05, 1e6),  # after W
    ]
    outs = maker_at(view(trades=trades(rows)), buy(), TE, HMIN, E_REF)
    mk = [o for o in outs if isinstance(o, Fill) and o.liquidity == "maker"]
    assert [o.ts for o in mk] == [TE + 3 * MIN, TE + 7 * MIN]
    assert [o.qty * E_REF for o in mk] == pytest.approx([4.0, 3.0], rel=1e-12)
    for o in mk:
        assert o.price == pytest.approx(lim, rel=1e-15) and o.ref_mid == o.price
        assert o.fee == pytest.approx(F_M * o.qty * lim, rel=1e-12)
        assert o.spread_cost == 0.0 and o.slip_cost == 0.0
        assert o.adverse == pytest.approx((-(100.0 / lim - 1),) * 3)  # flat mid after a buy at L < mid
    rest = [o for o in outs if isinstance(o, Fill) and o.liquidity == "taker"]
    assert len(rest) == 1 and rest[0].ts == TE + MAKER_WINDOW
    assert rest[0].qty * E_REF == pytest.approx(q_usd - 7.0, rel=1e-12)
    assert rest[0].fee == pytest.approx(F_T * rest[0].qty * rest[0].price, rel=1e-12)

    # cumulative through-quantity larger than the order: capped at the remaining quantity, no taker part
    big = trades([(TE + 2 * MIN, lim - 0.01, 6.0), (TE + 4 * MIN, lim - 0.01, 6.0)])
    outs = maker_at(view(trades=big), buy(), TE, HMIN, E_REF)
    assert [o.liquidity for o in outs] == ["maker", "maker"]
    assert [o.qty * E_REF for o in outs] == pytest.approx([6.0, 4.0], rel=1e-12)
    # a sell fills only from trades strictly ABOVE its limit
    slim = 100.0 * (1 + HMIN)
    s_rows = trades([(TE + MIN, slim, 1e6), (TE + 2 * MIN, slim - 0.01, 1e6),
                     (TE + 3 * MIN, slim + 0.01, 2.0)])
    outs = maker_at(view(trades=s_rows), sell(), TE, HMIN, E_REF)
    assert [(o.liquidity, round(o.qty * E_REF, 9)) for o in outs] == [("maker", 2.0), ("taker", 8.0)]


def test_halted_pending_exit_no_synthetic_fill():
    tau = TE
    # last traded bar before the halt has a distinctive price; nothing trades in [tau - 90 min, tau + 30 min)
    path = lambda ts: np.where(ts < tau - 90 * MIN, 100.0, np.where(ts < tau + 40 * MIN, 90.0, 95.0))  # noqa: E731
    b1 = m1(path, dead=((tau - 90 * MIN, tau + 30 * MIN),))
    v = view(b1)
    assert isinstance(taker_at(v, buy(), tau), NoTrade)  # S-5.4: no entry while halted
    p = taker_at(v, sell(), tau)
    assert isinstance(p, Pending) and p.due == tau
    f = halted_exit(v, p)
    assert isinstance(f, Fill)
    assert f.ts == tau + 31 * MIN  # first volume > 0 bar opening >= due, booked at its close time
    assert f.ref_mid == 90.0  # that bar's close (not the last traded bar before the halt: 100)
    assert f.price == pytest.approx(90.0 * (1 - (25e-4 + 3 * SLIP)), rel=1e-12)  # max(h, 25 bps), 3 x slip
    assert "halted_exit" in f.flags
    # a forced exit due while halted waits the same way
    fp = forced_at(v, sell(reason="leg_stop"), tau - MIN)
    assert isinstance(fp, Pending) and fp.due == tau
    # no executable bar of the instance before the window end: no fill is ever invented
    dead = m1(100.0, dead=((tau - 90 * MIN, END),))
    assert halted_exit(view(dead), Pending(sell(), tau)) is None
    # E10: the instance ceased first -> terminal (the bars of a later instance are never used)
    ceased = Instance(0, S0, tau - 60 * MIN, tau + 7 * 24 * H)
    assert halted_exit(view(m1(100.0), inst=ceased), Pending(sell(), tau)) == "terminal"


def test_forced_exit_first_bar_after_trigger_plus_one_minute():
    path = lambda ts: np.where(ts < TE + 2 * MIN, 100.0, 80.0)  # noqa: E731
    v = view(m1(path, dead=((TE + MIN, TE + 2 * MIN),)))  # the bar at trigger + 1 min is missing
    f = forced_at(v, sell(reason="leg_stop"), TE)
    assert isinstance(f, Fill) and f.ts == TE + 2 * MIN and f.ref_mid == 80.0  # first bar >= trigger + 1 min
    assert f.price == pytest.approx(80.0 * (1 - HMIN - SLIP), rel=1e-12)
    with pytest.raises(ValueError):
        forced_at(v, buy(), TE)


def test_missing_fill_proxy_entry_blocked_exit_fallback():
    v = view(px=100.0)
    old = b15(100.0, end=TE - 26 * H)  # no 15m pair inside h_fill(TE)'s 24 h window
    x2 = restrict(m1(100.0), old, old[["open_ts", "c"]], fund(1e-4, 100.0), INST, END)
    v2 = ExecView.build("AAAUSDT", x2)
    assert v.h_fill(TE) == pytest.approx(HMIN)
    assert v2.h_fill(TE) is None
    nt = taker_at(v2, buy(), TE)
    assert isinstance(nt, NoTrade) and nt.reason == "proxy_missing"
    f = taker_at(v2, sell(), TE)
    assert isinstance(f, Fill) and "proxy_fallback" in f.flags
    assert f.price == pytest.approx(100.0 * (1 - 25e-4 - 3 * SLIP), rel=1e-12)


def test_terminal_settlement_a_and_bc():
    inst = Instance(0, S0, TE, TE + 7 * 24 * H)
    v = view(m1(100.0), inst=inst)
    a = TerminalEvent(inst, TE, "A", TE - 15 * MIN, 97.0, "official_historical")
    f = settle(v, sell(), a)
    assert isinstance(f, Fill) and f.liquidity == "settlement" and f.ts == TE - 15 * MIN
    assert f.price == f.ref_mid == 97.0 and f.spread_cost == 0.0 and f.slip_cost == 0.0
    assert f.fee == pytest.approx(F_T * 0.001 * 97.0, rel=1e-12)
    for k in ("B", "C"):
        ev = TerminalEvent(inst, TE, k, TE if k == "B" else None, None, None)
        r = settle(v, sell(), ev)
        assert isinstance(r, RunInvalid) and r.reason == INVALID_TERMINAL
    with pytest.raises(ValueError):
        settle(v, sell(), None)  # the classified event of the held instance is required
    other = TerminalEvent(Instance(1, TE + H, None, None), TE, "A", TE, 97.0, "official_historical")
    with pytest.raises(ValueError):
        settle(v, sell(), other)  # E10: another instance's settlement never closes this position
