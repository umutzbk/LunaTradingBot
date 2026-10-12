"""Criteria (spec §7): episodes, clusters, evidence, concentration, temporal stability, attribution."""

from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from luna.v6 import criteria as cr
from luna.v6.engine import H3Config, H3Engine
from tests.v6.world import FixedModel, build_world

T = pd.Timestamp("2026-01-01T00:00Z")
D = pd.Timedelta("1D")


def fill(asset, ts, dq, mid=100.0, fee=0.1, ss=0.05, leg="perp"):
    return {"asset": asset, "ts": ts, "dq": dq, "mid": mid, "fee": fee, "spread_slip": ss, "leg": leg}


def test_episodes_merge_reentries_within_seven_days_and_split_after():
    f = pd.DataFrame([fill("A", T, 1), fill("A", T + 2 * D, -1),
                      fill("A", T + 5 * D, 1), fill("A", T + 6 * D, -1),  # 3 days later: same episode
                      fill("A", T + 20 * D, 1), fill("A", T + 21 * D, -1)])  # 14 days later: new
    s = pd.DataFrame(columns=["asset", "ts", "amount"])
    e = cr.episodes(f, s)
    assert len(e) == 2 and e.iloc[0]["end"] == T + 6 * D


def test_h1_spot_and_perp_legs_form_one_episode():
    f = pd.DataFrame([fill("A", T, 1, leg="spot"), fill("A", T, -1, leg="perp"),
                      fill("A", T + 3 * D, -1, leg="spot"), fill("A", T + 3 * D, 1, leg="perp")])
    e = cr.episodes(f, pd.DataFrame(columns=["asset", "ts", "amount"]))
    assert len(e) == 1


def test_clusters_are_greedy_from_the_first_entry():
    ts = [T, T + 3 * D, T + 6 * D, T + 7 * D, T + 13 * D, T + 15 * D]
    assert cr.clusters(ts) == 3  # [0,3,6], [7,13], [15]


def test_asset_contributions_sum_to_the_equity_change():
    w = build_world()
    r = H3Engine(w.market, FixedModel(w.funding_levels), w.prices, w.vol, w.insts, H3Config("H3-k4", 4),
                 equity0=10_000.0).run(w.start, w.start + 6 * D)
    C = cr.asset_contributions(r.fills, r.settlements)
    assert C.sum() == pytest.approx(r.daily["net"].sum(), abs=1e-6)
    eps = cr.episodes(r.fills, r.settlements)
    assert eps["net"].sum() == pytest.approx(C.sum(), abs=1e-6)


def run_with(daily_net, active, e0=1000.0):
    d = pd.DataFrame({"net": daily_net, "active": active})
    return SimpleNamespace(daily=d, equity0=e0)


def test_temporal_rules():
    n = 60  # 20-day sub-windows: evaluable needs >= 15 active days in each
    ok = cr.temporal(run_with([1.0] * n, [True] * n))
    assert ok["evaluable"] and ok["pass"]
    one_neg = cr.temporal(run_with([1.0] * 40 + [-0.5] * 20, [True] * n))
    assert one_neg["pass"]  # 2 of 3 positive, the negative one above the -3 % floor
    floor = cr.temporal(run_with([1.0] * 20 + [-2.0] * 20 + [1.0] * 20, [True] * n))
    assert floor["pass"] is False and floor["why"] == "floor"  # -40 USD = -4 % of 1000
    flat = cr.temporal(run_with([1.0] * n, [True] * 20 + [False] * 40))
    assert not flat["evaluable"] and flat["pass"] is None  # only one evaluable sub-window
    two = cr.temporal(run_with([1.0] * 20 + [-0.1] * 20 + [1.0] * 20, [False] * 20 + [True] * 40))
    assert two["evaluable"] and two["pass"] is False  # exactly two evaluable -> both must be positive
    short = cr.temporal(run_with([1.0] * 30, [True] * 30))
    assert not short["evaluable"]  # 10-day sub-windows can never reach 15 active days


def test_temporal_split_floor_n_over_3_and_remainder_last():
    r = cr.temporal(run_with([1.0] * 31, [True] * 31))
    assert [p["days"] for p in r["parts"]] == [10, 10, 11]


def test_concentration_rules():
    def mk(c):
        fills = []
        for a, v in c.items():
            fills += [fill(a, T, 1, mid=100.0, fee=0.0, ss=0.0), fill(a, T + D, -1, mid=100.0 + v, fee=0.0, ss=0.0)]
        return SimpleNamespace(fills=pd.DataFrame(fills), settlements=pd.DataFrame(columns=["asset", "ts", "amount"]))
    r = mk({"A": 4.0, "B": 3.0, "C": 3.0})
    c = cr.concentration(r, "H3", 10.0)
    assert c["P8a"]["pass"] and c["P8b"]["pass"] and not c["P8d"]["evaluable"]
    assert c["P8c"]["neff"] == pytest.approx(100 / 34)
    r2 = mk({"A": 9.0, "B": 0.5, "C": 0.5})
    c2 = cr.concentration(r2, "H3", 10.0)
    assert not c2["P8a"]["pass"] and c2["P8b"]["pass"]  # A dominates (9 > 5); without A net is still 1 > 0
    r4 = mk({"A": 12.0, "B": -1.0, "C": -1.0})
    assert not cr.concentration(r4, "H3", 10.0)["P8b"]["pass"]  # without A: 10 - 12 < 0
    r3 = mk({"A": 5.0, "B": 5.0})
    assert not cr.concentration(r3, "H1", 10.0)["P8a"]["evaluable"]  # fewer than 3 assets


def test_evidence_thresholds_differ_by_kind():
    fills = []
    for i, a in enumerate("ABC"):
        t0 = T + i * 8 * D
        fills += [fill(a, t0, 1), fill(a, t0 + 2 * D, -1)]
    daily = pd.DataFrame({"active": [True] * 70})
    r = SimpleNamespace(fills=pd.DataFrame(fills), settlements=pd.DataFrame(columns=["asset", "ts", "amount"]),
                        daily=daily)
    e1 = cr.evidence(r, "H1")
    assert e1["episodes"] == 3 and e1["clusters"] == 3 and e1["underlyings"] == 3 and e1["ok"]
    assert not cr.evidence(r, "H3")["ok"]  # H3 needs 10 episodes, 4 clusters, 6 underlyings
    r.daily = pd.DataFrame({"active": [True] * 59})
    assert not cr.evidence(r, "H1")["ok"]


def daily_run(rets, carry=None, kill=0, liq=0, e0=1000.0, active=True):
    eq = e0 * np.cumprod(1 + np.asarray(rets))
    prev = np.concatenate([[e0], eq[:-1]])
    net = eq - prev
    carry = np.full(len(rets), 0.1) if carry is None else np.asarray(carry)
    d = pd.DataFrame({"net": net, "ret": rets, "carry": carry, "carry_ret": carry / prev, "active": active})
    return SimpleNamespace(daily=d, fills=pd.DataFrame(columns=["asset", "ts", "dq", "mid", "fee", "spread_slip", "leg"]),
                           settlements=pd.DataFrame(columns=["asset", "ts", "amount"]),
                           counters={"kill": kill, "liquidation": liq}, equity0=e0)


def test_point_criteria_thresholds():
    rng = np.random.default_rng(0)
    good = daily_run(rng.normal(0.003, 0.004, 100))
    ev = cr.evaluate("H3-k4", "H3", "holdout", good, {"S-COST2X": good, "S-PIT10": good, "S-FB-CONS": good}, "VALID")
    assert ev.P["P1"]["pass"] and ev.P["P3"]["pass"] and ev.P["P4"]["pass"] and ev.P["P5"]["pass"]
    assert ev.P["P6"]["pass"] and ev.P["P7"]["pass"] and ev.P["P10"]["pass"] and ev.P["P11"]["pass"] and ev.P["P12"]["pass"]
    crash = daily_run([0.001] * 50 + [-0.031] + [0.001] * 49)
    ev2 = cr.evaluate("H3-k4", "H3", "holdout", crash, {}, "INCONCLUSIVE")
    assert not ev2.P["P6"]["pass"] and not ev2.P["P12"]["pass"] and not ev2.P["P7"]["evaluable"]
    dd = daily_run([0.002] * 20 + [-0.011] * 10 + [0.002] * 70)
    assert not cr.evaluate("H3-k4", "H3", "holdout", dd, {}, "VALID").P["P5"]["pass"]  # > 10 % drawdown
    flip = daily_run(-np.abs(rng.normal(0.003, 0.004, 100)))
    ev3 = cr.evaluate("H3-k4", "H3", "holdout", good, {"S-PIT10": flip}, "VALID")
    assert ev3.P["P11"]["pass"] is False and ev3.P["P11"]["flips"] == ["S-PIT10"]
    assert cr.evaluate("H3-k4", "H3", "holdout", daily_run([0.001] * 100, kill=1), {}, "VALID").P["P10"]["pass"] is False


def test_evidence_needs_independent_clusters_not_only_episodes():
    fills = []
    for i, a in enumerate("ABC"):  # three assets entered within the same week -> one cluster
        t0 = T + i * D
        fills += [fill(a, t0, 1), fill(a, t0 + 2 * D, -1)]
    r = SimpleNamespace(fills=pd.DataFrame(fills), settlements=pd.DataFrame(columns=["asset", "ts", "amount"]),
                        daily=pd.DataFrame({"active": [True] * 70}))
    e = cr.evidence(r, "H1")
    assert e["episodes"] == 3 and e["clusters"] == 1 and not e["ok"]


def test_effective_contributors_use_only_positive_contributions():
    fills = []
    for a, v in {"A": 3.0, "B": 3.0, "C": 3.0, "D": -3.0}.items():
        fills += [fill(a, T, 1, mid=100.0, fee=0.0, ss=0.0), fill(a, T + D, -1, mid=100.0 + v, fee=0.0, ss=0.0)]
    r = SimpleNamespace(fills=pd.DataFrame(fills), settlements=pd.DataFrame(columns=["asset", "ts", "amount"]))
    c = cr.concentration(r, "H3", 6.0)
    assert c["P8c"]["neff"] == pytest.approx(3.0) and not c["P8c"]["pass"]  # a loser does not count as a contributor


def test_p11_flags_a_carry_sign_flip_even_when_net_keeps_its_sign():
    rng = np.random.default_rng(3)
    rets = rng.normal(0.003, 0.004, 100)
    base = daily_run(rets, carry=np.full(100, 0.1))
    flipped = daily_run(rets, carry=np.full(100, -0.1))
    ev = cr.evaluate("H3-k4", "H3", "holdout", base, {"S-FB-CONS": flipped}, "VALID")
    assert ev.P["P11"]["flips"] == ["S-FB-CONS"]
