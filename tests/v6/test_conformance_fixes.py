"""Regression tests for the methodology-conformance review findings (covariance set, repair allowance,
H1 quantity step and cooldown, FEAS at day 180, clusters from every entry, forward risk exits, the
spot exit fallback, coverage over all assets, gate records after a kill)."""

import pandas as pd
import pytest

from luna.v6 import book as bk
from luna.v6 import criteria
from luna.v6 import engine as eng_mod
from luna.v6 import forward_exec as fx
from luna.v6 import verdict as vd
from luna.v6.engine import H3Config, H3Engine
from luna.v6.h1 import H1Config, H1Engine
from tests.v6.test_forward_exec import M, S, T, streams
from tests.v6.test_h1 import Fixed720, h1_world
from tests.v6.world import FixedModel, build_world

DAY = pd.Timedelta("1D")


def h3(w, k=4, equity0=10_000.0):
    return H3Engine(w.market, FixedModel(w.funding_levels), w.prices, w.vol, w.insts, H3Config("H3-k4", k),
                    equity0=equity0)


# --- S-H3-6: the covariance universe is eligible + held + BTC ------------------------------------------

def test_covariance_is_estimated_on_eligible_plus_held_plus_btc(monkeypatch):
    w = build_world()
    seen = []
    real = eng_mod.rm.covariance

    def spy(prices, vol, names, t):
        seen.append(list(names))
        return real(prices, vol, names, t)

    monkeypatch.setattr(eng_mod.rm, "covariance", spy)
    r = h3(w).run(w.start, w.start + 2 * DAY)
    ok = [d for d in r.decisions if d.get("status") == "OK"]
    assert seen and ok
    assert len(seen[0]) == ok[0]["n_eligible"]  # every eligible name, not only the selection
    assert len(seen[0]) > len(ok[0]["long"]) + len(ok[0]["short"]) and "BTC" in seen[0]


# --- S-H3-10: intraday repair uses the post-rounding checks with the step allowance -------------------

def test_intraday_repair_checks_with_the_step_notional_allowance(monkeypatch):
    got = []
    real = bk.checks

    def spy(n, L, S_, cov, E, step_notional=0.0):
        got.append(step_notional)
        return real(n, L, S_, cov, E, step_notional)

    monkeypatch.setattr(bk, "checks", spy)
    w = build_world()
    names = list(w.prices.columns)
    from luna.v6 import riskmodel as rm

    t = w.start
    cov = rm.covariance(w.prices, w.vol, names, t)
    insts = {a: bk.Inst(100.0, 0.001, 0.001) for a in names}
    n = {a: (500.0 if i % 2 else -500.0) for i, a in enumerate(names[:8])}
    bk.intraday_repair(n, cov, 10_000.0, insts, 0.25)
    assert got and all(x == 0.25 for x in got)


# --- S-H1-2: quantity step and minimums; S-H1-4: cooldown after any exit -----------------------------

def test_h1_quantity_step_is_the_larger_step_with_separate_minimums():
    w, insts = h1_world()
    insts["A00"] = {"spot": {"step": 0.1, "min_qty": 0.2}, "perp": {"step": 0.01, "min_qty": 0.5}}
    e = H1Engine(w.market, Fixed720(w.funding_levels), insts, H1Config("H1-m1.5", 1.5))
    assert e._qty_step("A00") == (0.1, 0.2, 0.5)


def test_h1_economic_exit_also_starts_the_cooldown():
    w, insts = h1_world()
    e = H1Engine(w.market, Fixed720(w.funding_levels), insts, H1Config("H1-m1.5", 1.5))
    from luna.v6.h1 import _Pos, _St

    st = _St(usd=10_000.0)
    st.pos["A00"] = _Pos(1.0, 1.0, 100.0, 100.0, 100.0)
    e.counters = {"unfilled_pairs": 0}
    e._close(st, "A00", w.start, "exit")
    assert st.cooldown.get("A00") == w.start + pd.Timedelta("24h")


# --- S-STAT-F: a FEAS slot's confirmation replay is laddered at day 180 -----------------------------------

def _fam():
    return vd.ForwardFamily(cand=(), feas=("H1-m1.5",))


def test_feas_slot_closes_at_day_180_on_model_invalid_in_the_confirmation_window():
    f = _fam()
    f.look(90, {}, lambda c, r: vd.Verdict(c, "INSUFFICIENT", "", []), nw_p={}, feasibility={"H1-m1.5": True})

    def bad(c, rej):
        return vd.Verdict(c, "MODEL INVALID", "MODEL INVALID", ["MODEL INVALID: H2a gate INVALID"])
    r = f.look(180, {}, bad, nw_p={})
    assert f.status["H1-m1.5"] == "CLOSED" and r["p"]["H1-m1.5"] == 1.0


def test_feas_slot_stays_pending_at_day_180_on_a_point_failure():
    f = _fam()
    f.look(90, {}, lambda c, r: vd.Verdict(c, "INSUFFICIENT", "", []), nw_p={}, feasibility={"H1-m1.5": True})
    f.look(180, {}, lambda c, r: vd.Verdict(c, "NO-GO", "NO-GO", ["NO-GO: P1/P3 <= 0"]), nw_p={})
    assert f.status["H1-m1.5"] == "PENDING"


# --- P8/P9 clusters: from every entry, not from merged episodes ------------------------------------------

def test_entries_lists_every_opening_from_flat_including_reentries():
    ts = [pd.Timestamp("2026-01-01T00:15Z") + i * DAY for i in range(4)]
    F = pd.DataFrame({"asset": ["X", "X", "X", "Y"], "leg": ["perp"] * 4, "ts": ts,
                      "dq": [1.0, -1.0, 1.0, -2.0]})
    assert criteria.entries(F) == [ts[0], ts[2], ts[3]]


# --- S-H1-X: risk exits — both legs at their first snapshots, neither waits ------------------------------

def test_risk_exit_perp_leg_does_not_wait_for_the_spot_leg():
    s = streams(ticks=[(-30 * S, 100.0, 100.2)], depths=[(10 * M, [(100.0, 5)], [(100.2, 5)])],
                fbooks=[(3 * M, [(99.9, 50.0)], [(100.1, 50.0)]), (12 * M, [(99.9, 50.0)], [(100.1, 50.0)])])
    spot, perp = fx.FwdMarket(s, None, ["ETH"]).pair_fill("ETH", -2.0, T, economic=False)
    assert spot[0].ts == T + 10 * M and perp[0].ts == T + 3 * M


# --- S-H1-X / D-X1: the spot exit's taker fallback continues on later snapshots ---------------------------

def test_spot_exit_taker_fallback_continues_until_flat():
    s = streams(ticks=[(-30 * S, 100.0, 100.2), (30 * M, 100.0, 100.2)],
                depths=[(55 * M, [(100.0, 1.0)], [(100.2, 5)]), (70 * M, [(99.9, 5.0)], [(100.2, 5)])])
    r = fx.maker_first_spot(s, "ETH", -3.0, T)
    assert sum(f.dq for f in r.fills) == pytest.approx(-3.0)
    assert [f.ts for f in r.fills] == [T + 60 * M, T + 70 * M]


def test_spot_entry_taker_fallback_cancels_the_rest():
    s = streams(ticks=[(-30 * S, 100.0, 100.2), (30 * M, 100.0, 100.2)],
                depths=[(55 * M, [(100.0, 5)], [(100.2, 1.0)]), (70 * M, [(100.0, 5)], [(100.3, 5.0)])])
    r = fx.maker_first_spot(s, "ETH", 3.0, T)
    assert sum(f.dq for f in r.fills) == pytest.approx(1.0)


# --- S-DATA-5: coverage and freshness over all assets --------------------------------------------------

def test_a_fresh_snapshot_of_any_asset_makes_the_quarter_evaluable():
    s = fx.Streams()
    s.fut_ticks["AAA"] = fx.Stream([fx.Tick(T - pd.Timedelta("1h"), 1, 1)])  # stale
    s.fut_ticks["BBB"] = fx.Stream([fx.Tick(T - 10 * S, 1, 1)])
    assert fx.coverage(s, T, T)["share"].iloc[0] == 1.0


# --- S-H2-G: the gate keeps its PIT records after a kill ----------------------------------------------

def test_gate_records_continue_after_the_r4_kill():
    w = build_world(shocks=[("A00", pd.Timestamp("2025-05-06T12:00Z"), 4.0)])
    r = h3(w, equity0=1000.0).run(w.start, w.start + 12 * DAY)
    assert r.counters["kill"] == 1
    kt = [e["t"] for e in r.events if e["event"] == "kill"][0]
    assert any(g.s > kt for g in r.gate_records)
