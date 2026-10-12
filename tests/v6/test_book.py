"""H3 deterministic book (S-H3-2 ... S-H3-12). The critical invariant: the book must not reproduce
the V5 failure mode (high-vol longs against low-vol shorts at equal dollars)."""

import math

import numpy as np
import pandas as pd
import pytest

from luna.v6 import book as bk
from luna.v6.riskmodel import Cov


def make_cov(vols: dict, betas: dict | None = None, rho=0.5):
    names = list(vols)
    if "BTC" not in names:
        names = ["BTC"] + names
        vols = {"BTC": 0.5, **vols}
    n = len(names)
    C = pd.DataFrame(np.full((n, n), rho) + (1 - rho) * np.eye(n), index=names, columns=names)
    sig = pd.Series(vols)[names]
    S = C * np.outer(sig, sig)
    beta = S["BTC"] / S.loc["BTC", "BTC"]
    if betas:
        beta = beta.copy()
        for a, b in betas.items():
            beta[a] = b
    return Cov(names, C, S, sig, beta, 0.3, 1440)


def legs(names, mid=100.0, step=0.01, min_qty=0.01):
    return {a: bk.Inst(mid=mid, step=step, min_qty=min_qty) for a in names}


LONG = ["L1", "L2", "L3", "L4"]
SHORT = ["S1", "S2", "S3", "S4"]


def test_v5_failure_mode_2_5x_vol_is_refused_and_opens_nothing():
    vols = {**{a: 1.5 for a in LONG}, **{a: 0.6 for a in SHORT}}  # 2.5x long-side vol, equal correlations
    cov = make_cov(vols)
    eq = {**{a: 1.0 for a in LONG}, **{a: -1.0 for a in SHORT}}
    assert bk.side_risk_ratio(eq, LONG, SHORT, cov) == pytest.approx(2.5)  # what V5 (equal dollars) held
    b = bk.build(LONG, SHORT, cov, 10_000.0, legs(LONG + SHORT))
    assert b.status == "INFEASIBLE" and b.n == {} and b.qty == {}  # basket ratio > 2.25 -> no book


def test_2x_vol_imbalance_is_rebalanced_by_risk_within_the_ratio_and_dollar_limits():
    vols = {**{a: 1.2 for a in LONG}, **{a: 0.6 for a in SHORT}}
    cov = make_cov(vols)
    b = bk.build(LONG, SHORT, cov, 10_000.0, legs(LONG + SHORT))
    assert b.status == "OK" and b.diag["cap_bound"]
    nL = sum(b.n[a] for a in LONG)
    nS = -sum(b.n[a] for a in SHORT)
    q = bk.side_risk_ratio(b.n, LONG, SHORT, cov)
    assert 1 / 1.5 - 1e-9 <= q <= 1.5 + 1e-9 and q < 2.0
    assert nL < nS and abs(nL - nS) <= 0.2 * (nL + nS) + 1.0


def test_risk_parity_and_dollar_cap_and_ratio_rule_values():
    cov = make_cov({**{a: 0.8 for a in LONG}, **{a: 0.6 for a in SHORT}}, rho=0.3)
    b = bk.build(LONG, SHORT, cov, 1e6, legs(LONG + SHORT, step=1e-6, min_qty=1e-6))
    d = b.diag
    assert d["phi_L"] + d["phi_S"] == pytest.approx(1.0)
    assert d["phi_L"] == pytest.approx(d["sigt_S"] / (d["sigt_L"] + d["sigt_S"]))
    assert not d["cap_bound"] and d["q"] == pytest.approx(1.0)
    cov2 = make_cov({**{a: 1.8 for a in LONG}, **{a: 0.6 for a in SHORT}}, rho=0.3)  # basket ratio 3 > 2.25
    b2 = bk.build(LONG, SHORT, cov2, 1e6, legs(LONG + SHORT, step=1e-6, min_qty=1e-6))
    assert b2.status == "INFEASIBLE" and b2.n == {} and "dollar" in b2.reason


def test_sigma_star_scaling_and_gross_cap():
    cov = make_cov({a: 0.8 for a in LONG + SHORT})
    b = bk.build(LONG, SHORT, cov, 1e6, legs(LONG + SHORT, step=1e-6, min_qty=1e-6))
    w = pd.Series(b.n) / 1e6
    vol = math.sqrt(w @ cov.Sigma.loc[w.index, w.index] @ w)
    assert vol == pytest.approx(0.08, rel=1e-3) or w.abs().sum() == pytest.approx(1.0, rel=1e-6)
    assert w.abs().sum() <= 1.0 + 1e-9
    lowvol = make_cov({a: 0.02 for a in LONG + SHORT}, rho=0.0)  # G would exceed 1
    b2 = bk.build(LONG, SHORT, lowvol, 1e6, legs(LONG + SHORT, step=1e-6, min_qty=1e-6))
    assert (pd.Series(b2.n).abs().sum() / 1e6) == pytest.approx(1.0, rel=1e-4)


def test_beta_imbalance_is_scaled_down_to_the_limit():
    cov = make_cov({a: 0.8 for a in LONG + SHORT}, betas={**{a: 2.5 for a in LONG}, **{a: 0.2 for a in SHORT}})
    b = bk.build(LONG, SHORT, cov, 1e6, legs(LONG + SHORT, step=1e-6, min_qty=1e-6))
    assert b.status == "OK" and b.diag["beta_scaled"]
    beta_net = sum(b.n[a] * cov.beta[a] for a in b.n) / 1e6
    assert abs(beta_net) <= 0.05 + 1e-6


def test_min_notional_removal_drops_the_smallest_and_rebuilds():
    vols = {"L1": 0.5, "L2": 0.6, "L3": 0.7, "L4": 6.0, "S1": 0.5, "S2": 0.6, "S3": 0.7, "S4": 0.8}
    cov = make_cov(vols, rho=0.2)
    b = bk.build(LONG, SHORT, cov, 1000.0, legs(LONG + SHORT, step=0.01, min_qty=0.01))
    assert b.status == "OK" and b.diag["dropped"] == ["L4"]  # the 6.0-vol leg is tiny under inverse vol
    assert all(abs(v) >= 10.0 for v in b.n.values())


def test_one_side_asset_loss_makes_the_book_infeasible_and_opens_nothing():
    cov = make_cov({a: 0.8 for a in LONG + ["S1", "S2"]})
    b = bk.build(LONG, ["S1", "S2"], cov, 1e5, legs(LONG + ["S1", "S2"]))
    assert b.status == "INFEASIBLE" and b.n == {}
    tiny = bk.build(LONG, SHORT, make_cov({a: 0.8 for a in LONG + SHORT}), 30.0, legs(LONG + SHORT))
    assert tiny.status == "INFEASIBLE" and tiny.n == {}  # every leg below 10 USD -> sides fall below 3


def test_post_rounding_checks():
    cov = make_cov({a: 0.8 for a in LONG + SHORT})
    good = {**{a: 100.0 for a in LONG}, **{a: -100.0 for a in SHORT}}
    ok = bk.checks(good, LONG, SHORT, cov, 10_000.0, step_notional=1.0)
    assert ok["ok"]
    lop = {**{a: 200.0 for a in LONG}, **{a: -100.0 for a in SHORT}}
    bad = bk.checks(lop, LONG, SHORT, cov, 10_000.0, step_notional=1.0)
    assert not bad["dollar_ok"] and not bad["q_ok"] and not bad["ok"]


def test_selection_zones_buffer_and_entry_economics():
    names = [f"X{i:02d}" for i in range(12)]
    s = pd.Series(np.linspace(6e-5, -5e-5, 12), index=names)  # X00 highest forecast
    cost = pd.Series(1e-4, index=names)
    sel = bk.select(s, held={}, cost=cost, k=4)
    assert sel.kp == 4 and sel.r == 2
    assert sel.short == names[:4] and sel.long == list(reversed(names[-4:]))
    # hysteresis: a held short at rank 6 (inside k'+r = 6) stays; at rank 7 it exits
    sel2 = bk.select(s, held={"X05": -1, "X06": -1}, cost=cost, k=4)
    assert "X05" in sel2.short and ("X06", "rank") in sel2.exits
    # entry needs c * 168 > 2 cost
    big = pd.Series(1.0, index=names)
    sel3 = bk.select(s, held={}, cost=big, k=4)
    assert sel3.short == [] and sel3.long == []


def test_kprime_shrinks_and_below_three_is_infeasible():
    names = [f"X{i}" for i in range(7)]
    s = pd.Series(np.linspace(5e-5, -5e-5, 7), index=names)
    sel = bk.select(s, held={}, cost=pd.Series(0.0, index=names), k=6)
    assert sel.kp == 3 and sel.r == 0
    five = bk.select(s.iloc[:5], held={}, cost=pd.Series(0.0, index=names[:5]), k=4)
    assert five.infeasible


def test_held_leg_with_negative_expected_carry_beyond_exit_cost_is_exited():
    names = [f"X{i:02d}" for i in range(12)]
    s = pd.Series(np.linspace(6e-5, -5e-5, 12), index=names)
    cost = pd.Series(1e-6, index=names)
    sel = bk.select(s, held={"X00": 1}, cost=cost, k=4)  # long the highest forecast: c << 0
    assert ("X00", "carry") in sel.exits or ("X00", "rank") in sel.exits


def test_band_keeps_small_differences_and_repairs_to_target_when_checks_fail():
    cov = make_cov({a: 0.8 for a in LONG + SHORT})
    target = {**{a: 100.0 for a in LONG}, **{a: -100.0 for a in SHORT}}
    held = {**{a: 110.0 for a in LONG}, **{a: -95.0 for a in SHORT}, "OLD": 50.0}
    out = bk.apply_band(target, held, LONG, SHORT, cov, 10_000.0, step_notional=1.0)
    assert out.status == "OK" and out.n["OLD"] == 0.0
    assert out.n["L1"] == 110.0 and out.n["S1"] == -95.0  # within 25 %: kept
    held2 = {**{a: 130.0 for a in LONG}, **{a: -100.0 for a in SHORT}}
    out2 = bk.apply_band(target, held2, LONG, SHORT, cov, 10_000.0, step_notional=1.0)
    assert out2.n["L1"] == 100.0  # 30 % away -> resized
    held3 = {**{a: 124.0 for a in LONG}, **{a: -80.0 for a in SHORT}}  # each within band, book fails dollar
    out3 = bk.apply_band(target, held3, LONG, SHORT, cov, 10_000.0, step_notional=1.0)
    assert out3.repaired and out3.n == {a: target[a] for a in LONG + SHORT}


def test_intraday_repair_beta_then_shrink_only_then_exit_all():
    cov = make_cov({a: 0.8 for a in LONG + SHORT})
    insts = legs(LONG + SHORT)
    # a stop took one short: dollars 400 long vs 300 short -> ratio 1.33 within [2/3, 1.5]; fine
    n = {**{a: 100.0 for a in LONG}, **{a: -100.0 for a in SHORT[:3]}}
    r = bk.intraday_repair(n, cov, 10_000.0, insts)
    assert r.status == "OK" and r.n == n
    # two shorts lost -> < 3 legs on a side -> exit all
    n2 = {**{a: 100.0 for a in LONG}, **{a: -100.0 for a in SHORT[:2]}}
    assert bk.intraday_repair(n2, cov, 10_000.0, insts).status == "EXIT_ALL"
    # dollar failure: long 4 x 100 vs short 3 x 50 -> shrink only the long side
    n3 = {**{a: 100.0 for a in LONG}, **{a: -50.0 for a in SHORT[:3]}}
    r3 = bk.intraday_repair(n3, cov, 10_000.0, insts)
    assert r3.status == "OK"
    assert all(abs(r3.n[a]) <= abs(n3[a]) + 1e-9 for a in r3.n)  # reduce only
    assert r3.n["S1"] == -50.0 and bk.checks(r3.n, LONG, SHORT[:3], cov, 10_000.0, 0.0)["ok"]


def test_intraday_repair_exits_all_when_the_two_intervals_do_not_intersect():
    # long side: low dollars but very high risk; short: high dollars, low risk
    cov = make_cov({**{a: 3.0 for a in LONG}, **{a: 0.3 for a in SHORT[:3]}}, rho=0.0)
    n = {**{a: 50.0 for a in LONG}, **{a: -100.0 for a in SHORT[:3]}}
    r = bk.intraday_repair(n, cov, 10_000.0, legs(LONG + SHORT[:3]))
    assert r.status == "EXIT_ALL"


def test_min_notional_drops_the_smallest_first_and_the_rebuild_can_keep_the_next():
    vols = {"L1": 0.5, "L2": 0.6, "L3": 0.7, "L4": 4.0, "L5": 3.0, "S1": 0.5, "S2": 0.6, "S3": 0.7, "S4": 0.8}
    cov = make_cov(vols, rho=0.2)
    L5 = ["L1", "L2", "L3", "L4", "L5"]
    b = bk.build(L5, SHORT, cov, 1000.0, legs(L5 + SHORT, step=0.01, min_qty=0.01))
    assert b.status == "OK" and b.diag["dropped"][0] == "L4"  # the 4.0-vol leg is the smallest
