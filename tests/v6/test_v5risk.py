"""Erratum 001: the retained V5 carry.risk controls (values and measurement of luna/risk/engine.py at
fabd419), H3 book-level (no partial book) and H1 per entry."""

import pandas as pd
import pytest

from luna.v6 import v5risk
from luna.v6.engine import H3Config, H3Engine
from tests.v6.world import FixedModel, build_world

DAY = pd.Timedelta("1D")


def test_each_retained_rule_at_its_v5_boundary():
    E = 1000.0
    assert v5risk.violations([266.0], [266.0], E) == []  # 266 x 0.15 = 39.9 <= 40
    assert v5risk.violations([267.0], [267.0], E) == ["R5_risk_per_trade"]
    assert v5risk.violations([100.0], [100.0] * 10 + [70.0], E) == ["R6_open_risk"]  # 1070 x 0.15 > 160
    assert v5risk.violations([100.0], [100.0] * 10, E) == []
    assert v5risk.violations([10.0], [10.0], E, positions_after=5) == ["R7_max_positions"]
    assert v5risk.violations([10.0], [10.0], E, positions_after=4) == []
    assert "R8_gross_leverage" in v5risk.violations([10.0], [1251.0], E)
    assert v5risk.violations([10.0], [10.0], E, orders_today=28, new_orders=2) == []
    assert v5risk.violations([10.0], [10.0], E, orders_today=29, new_orders=2) == ["R14_orders_per_day"]
    assert v5risk.violations([], [10.0], E, orders_today=40, new_orders=3) == []  # closes only: no R14 veto


def test_r9_leg_notional_cap():
    assert "R9_leg_notional" in v5risk.violations([301.0], [301.0], 1000.0)


def test_values_are_the_v5_carry_profile_at_fabd419():
    assert (v5risk.R5_MAX_RISK_PER_TRADE, v5risk.R6_MAX_OPEN_RISK, v5risk.R7_MAX_POSITIONS, v5risk.R8_MAX_GROSS,
            v5risk.R9_MAX_LEG, v5risk.R14_MAX_ORDERS_PER_DAY, v5risk.V5_LEG_STOP) == (0.04, 0.16, 4, 1.25, 0.30, 30, 0.15)


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
