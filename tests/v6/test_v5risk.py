"""Erratum 001: the retained V5 carry.risk controls (values and measurement of luna/risk/engine.py at
fabd419), H3 book-level (no partial book) and H1 per entry."""

import pandas as pd
import pytest

from luna.v6 import v5risk
from luna.v6.engine import H3Config, H3Engine
from tests.v6.world import FixedModel, build_world

DAY = pd.Timedelta("1D")


def V(opening, gross=None, risk=None, E=1000.0, **kw):
    gross = list(opening) if gross is None else gross
    risk = [0.15 * n for n in gross] if risk is None else risk
    return v5risk.violations(opening, gross, risk, E, **kw)


def test_each_retained_rule_at_its_v5_boundary():
    assert V([266.0]) == []  # 266 x 0.15 = 39.9 <= 40
    assert V([267.0]) == ["R5_risk_per_trade"]
    assert V([100.0], gross=[100.0] * 11, risk=[15.0] * 10 + [10.5]) == ["R6_open_risk"]  # 160.5 > 160
    assert V([100.0], gross=[100.0] * 10) == []
    assert V([10.0], positions_after=5) == ["R7_max_positions"]
    assert V([10.0], positions_after=4) == []
    assert "R8_gross_leverage" in V([10.0], gross=[1251.0], risk=[1.0])
    assert V([10.0], orders_before=[29]) == []  # 29 orders sent before this open: allowed
    assert V([10.0, 10.0], orders_before=[29, 30]) == ["R14_orders_per_day"]  # the 31st order is an open


def test_closes_and_reduces_are_exempt_from_r5_to_r9_and_r14_as_in_v5():
    """V5 engine.py at fabd419: R5-R9 apply only to opening orders; closes and reduces are exempt."""
    assert v5risk.violations([], [18.0] * 6, [999.0] * 6, 100.0, positions_after=9, orders_before=[]) == []


def test_r6_open_risk_of_held_legs_is_measured_at_the_entry_price():
    from luna.v6.engine import _State

    w = build_world()
    eng = H3Engine(w.market, FixedModel(w.funding_levels), w.prices, w.vol, w.insts, H3Config("H3-k4", 4))
    st = _State(cash=0.0)
    st.pos["A00"], st.entry_px["A00"] = 2.0, 100.0  # average entry FILL price
    # held 2 at entry 100, mid now 300; growing to 3: 0.15 x (2 x 100) + 0.15 x (1 x 300)
    assert eng._leg_risk(st, "A00", 3.0, 300.0) == pytest.approx(0.15 * 200 + 0.15 * 300)
    assert eng._leg_risk(st, "A00", 1.0, 300.0) == pytest.approx(0.15 * 100)  # reduced: scaled down
    assert eng._leg_risk(st, "A01", -1.0, 50.0) == pytest.approx(0.15 * 50)  # new leg at the decision mid


def test_r9_leg_notional_cap():
    assert "R9_leg_notional" in V([301.0])


def test_values_are_the_v5_carry_profile_at_fabd419():
    assert (v5risk.R5_MAX_RISK_PER_TRADE, v5risk.R6_MAX_OPEN_RISK, v5risk.R7_MAX_POSITIONS, v5risk.R8_MAX_GROSS,
            v5risk.R9_MAX_LEG, v5risk.R14_MAX_ORDERS_PER_DAY, v5risk.V5_LEG_STOP) == (0.04, 0.16, 4, 1.25, 0.30, 30, 0.15)


def test_non_finite_notional_or_risk_fails_closed():
    assert V([10.0], gross=[float("nan")], risk=[1.0]) != []
    assert V([10.0], gross=[10.0], risk=[float("nan")]) != []


def test_an_h3_book_violating_a_retained_rule_is_infeasible_not_partial(monkeypatch):
    w = build_world()
    monkeypatch.setattr(v5risk, "R9_MAX_LEG", 1e-6)  # every leg breaks R9
    r = H3Engine(w.market, FixedModel(w.funding_levels), w.prices, w.vol, w.insts, H3Config("H3-k4", 4),
                 equity0=10_000.0).run(w.start, w.start + 2 * DAY)
    assert r.fills.empty or (r.fills["intent"] == "terminal").all()
    assert any(str(d.get("reason", "")).startswith("V5 R9") for d in r.decisions)


def test_h1_entries_are_vetoed_one_by_one_by_a_retained_rule(monkeypatch):
    from tests.v6.test_h1 import h1_world, run

    w, insts = h1_world()
    base = run(w, insts, days=2)
    assert base.counters["entries"] >= 2
    monkeypatch.setattr(v5risk, "R7_MAX_POSITIONS", 1)  # one hedged pair = one position id
    w2, insts2 = h1_world()
    r = run(w2, insts2, days=2)
    assert r.counters["entries"] == 1 and r.counters["v5_vetoes"] >= 1
