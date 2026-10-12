"""Erratum 001, R6: V5 risk_amount = leg_stop_frac x |qty| x entry_price, where entry_price is the
average FILL price (luna/portfolio/portfolio.py at fabd419: `entry_price` = "avg entry at fill prices
per asset", updated by `_trade(pos_q, entry, delta, fill.price)`; luna/carry/core.py:691). The
fill-time mid stays the leg-stop reference (D-E5); it is not the R6 price."""

import math
from types import SimpleNamespace

import pandas as pd
import pytest

from luna.v6 import v5risk
from luna.v6.engine import H3Config, H3Engine, _State, avg_entry
from luna.v6.h1 import H1Config, H1Engine, _Pos, _St
from tests.v6.world import FixedModel, build_world


@pytest.mark.parametrize("q, avg, dq, px, want_q, want_avg", [
    (0.0, 0.0, 2.0, 101.0, 2.0, 101.0),  # open at the fill price
    (2.0, 101.0, 2.0, 105.0, 4.0, 103.0),  # increase: quantity-weighted fill prices
    (4.0, 103.0, -1.0, 90.0, 3.0, 103.0),  # partial reduce: average unchanged
    (3.0, 103.0, -3.0, 90.0, 0.0, 0.0),  # exact close
    (3.0, 103.0, -5.0, 90.0, -2.0, 90.0),  # flip: the new side starts at the fill price
    (-2.0, 50.0, -2.0, 52.0, -4.0, 51.0),  # short increase
])
def test_avg_entry_is_the_v5_trade_rule_on_fill_prices(q, avg, dq, px, want_q, want_avg):
    nq, na = avg_entry(q, avg, dq, px)
    assert nq == pytest.approx(want_q) and na == pytest.approx(want_avg)


def _h3():
    w = build_world()
    return H3Engine(w.market, FixedModel(w.funding_levels), w.prices, w.vol, w.insts, H3Config("H3-k4", 4))


def test_h3_r6_uses_the_average_fill_price_not_the_fill_time_mid():
    eng = _h3()
    st = _State(cash=0.0)
    st.pos["A00"], st.entry["A00"], st.entry_px["A00"] = 2.0, 100.0, 101.0  # mid 100, filled at 101
    assert eng._leg_risk(st, "A00", 2.0, 300.0) == pytest.approx(0.15 * 2 * 101.0)
    assert eng._leg_risk(st, "A00", 3.0, 300.0) == pytest.approx(0.15 * (2 * 101.0 + 1 * 300.0))  # growth at mid
    assert eng._leg_risk(st, "A00", 1.0, 300.0) == pytest.approx(0.15 * 1 * 101.0)  # reduce: scaled


def test_h3_books_the_average_fill_price_on_every_fill():
    w = build_world()
    eng = H3Engine(w.market, FixedModel(w.funding_levels), w.prices, w.vol, w.insts, H3Config("H3-k4", 4),
                   equity0=10_000.0)
    seen = {}
    real = eng._leg_risk

    def spy(st, a, q_after, mid):
        if a in st.pos:
            seen[a] = (st.pos[a], dict(st.entry_px), dict(st.entry))
        return real(st, a, q_after, mid)

    eng._leg_risk = spy
    r = eng.run(w.start, w.start + 3 * pd.Timedelta("1D"))
    checked = 0
    for a, (q, epx, emid) in seen.items():
        f = r.fills[(r.fills["asset"] == a) & (r.fills["intent"] != "terminal")]
        same_side = bool(((f["dq"] > 0).all() or (f["dq"] < 0).all())) if len(f) else False
        if same_side and abs(float(f["dq"].sum()) - q) < 1e-9:  # only increases: the average is plain
            want = float((f["dq"].abs() * f["px"]).sum() / f["dq"].abs().sum())
            assert epx[a] == pytest.approx(want)  # the average FILL price
            assert epx[a] != pytest.approx(emid[a], rel=1e-9)  # not the fill-time mid
            checked += 1
    assert checked > 0


def test_h1_r6_uses_each_leg_s_average_fill_price(monkeypatch):
    from tests.v6.test_conformance_fixes import _CashMarket

    class _M720:
        h = 720

        def forecast(self, t):
            return pd.Series({"X": 1e-3, "Y": 1e-3})

    m = _CashMarket()
    m.assets = ["X", "Y"]
    m.funding = SimpleNamespace(newest_end=lambda a, t: t, fl=lambda t, h, k: pd.Series({"X": 1e-4, "Y": 1e-4}))
    insts = {a: {"spot": {"step": 1.0, "min_qty": 1.0}, "perp": {"step": 1.0, "min_qty": 1.0}} for a in ("X", "Y")}
    e = H1Engine(m, _M720(), insts, H1Config("H1-m1.5", 1.5), equity0=1e6)
    from collections import defaultdict
    e.counters = defaultdict(int)
    st = _St(usd=1e6)
    st.pos["X"] = _Pos(2.0, 2.0, 1000.0, 1000.0, 2000.0, spot_px=1004.0, perp_px=998.0)
    got = {}

    def spy(opening, gross_after, risk_after, E, **kw):
        got["risk"] = list(risk_after)
        return []

    monkeypatch.setattr(v5risk, "violations", spy)
    e._decide(st, pd.Timestamp("2026-01-10T00:00Z"), 1e6, [], [])
    assert got["risk"][0] == pytest.approx(0.15 * (2 * 1004.0 + 2 * 998.0))  # the held pair at its fill prices


def test_h1_books_average_fill_prices_per_leg():
    from tests.v6.test_h1 import Fixed720, h1_world

    w, insts = h1_world()
    e = H1Engine(w.market, Fixed720(w.funding_levels * 4), insts, H1Config("H1-m1.5", 1.5), equity0=10_000.0)
    snaps = []
    real = e._decide

    def spy(st, t, E, decisions, gates, gate_only=False):
        snaps.append({a: (ps.spot_px, ps.perp_px, ps.risk()) for a, ps in st.pos.items()})
        return real(st, t, E, decisions, gates, gate_only=gate_only)

    e._decide = spy
    r = e.run(w.start, w.start + 3 * pd.Timedelta("1D"))
    o = r.fills[r.fills["intent"] == "open"]
    held = next(s for s in snaps if s)  # the first decision after the entries
    assert held
    for a, (spx, ppx, risk) in held.items():
        g = o[o["asset"] == a]
        sp, pp = g[g["leg"] == "spot"], g[g["leg"] == "perp"]
        assert spx == pytest.approx(float((sp["dq"] * sp["px"]).sum() / sp["dq"].sum()))
        assert ppx == pytest.approx(float((pp["dq"].abs() * pp["px"]).sum() / pp["dq"].abs().sum()))
        assert spx != pytest.approx(float(sp["mid"].iloc[0]), rel=1e-12)  # a fill price, not the mid
        assert risk == pytest.approx(0.15 * float((g["dq"].abs() * g["px"]).sum()))
