"""The central ladder, the holdout decision and the forward sequential looks (synthetic inputs)."""

import numpy as np
import pytest

from luna.v6 import stats6
from luna.v6 import verdict as vd
from luna.v6.criteria import Evaluation


def good_eval(config="H3-k4", kind="H3", env="holdout", **over):
    P = {k: {"evaluable": True, "pass": True} for k in ("P1", "P3", "P4", "P5", "P6", "P7", "P10", "P11", "P12",
                                                        "P8a", "P8b", "P8c", "P8d", "P9")}
    ev = Evaluation(config, kind, env, net=10.0, carry=5.0, P=P, evidence={"ok": True}, gate="VALID",
                    stats={"p_go": 0.001, "lb_ok": True})
    for k, v in over.items():
        setattr(ev, k, v)
    return ev


def test_all_pass_is_advance_in_holdout_and_final_go_in_forward():
    assert vd.ladder(good_eval(), stat_reject=True).cls == "ADVANCE"
    assert vd.ladder(good_eval(env="forward"), stat_reject=True).cls == "FINAL GO"


def test_holdout_never_gives_final_go_and_h1_historical_never_final_go():
    assert vd.ladder(good_eval(config="H1-m1.5", kind="H1"), stat_reject=True).cls == "ADVANCE"
    v = vd.ladder(good_eval(config="H1-m1.5", kind="H1", env="forward"), stat_reject=True, execution=None)
    assert v.cls == "INVALID RUN"
    v2 = vd.ladder(good_eval(config="H1-m1.5", kind="H1", env="forward"), stat_reject=True, execution="S-H1-X")
    assert v2.cls == "FINAL GO"


def test_model_invalid_beats_any_pnl_and_inconclusive_gate_blocks_go():
    assert vd.ladder(good_eval(gate="INVALID"), stat_reject=True).cls == "MODEL INVALID"
    v = vd.ladder(good_eval(gate="INCONCLUSIVE"), stat_reject=True)
    assert v.cls == "MODEL INCONCLUSIVE"


def test_most_severe_wins_and_all_reasons_are_reported():
    ev = good_eval()
    ev.evidence = {"ok": False}
    ev.P["P5"] = {"evaluable": True, "pass": False}
    v = vd.ladder(ev, stat_reject=False)
    assert v.cls == "NO-GO"  # the drawdown failure is not hidden by INSUFFICIENT
    assert any("P5" in r for r in v.reasons) and any("INSUFFICIENT" in r for r in v.reasons)
    assert any("P2" in r for r in v.reasons)


def test_p2_needs_both_holm_and_lb():
    assert vd.ladder(good_eval(), stat_reject=False).cls == "INCONCLUSIVE"
    ev = good_eval(stats={"p_go": 0.001, "lb_ok": False})
    assert vd.ladder(ev, stat_reject=True).cls == "INCONCLUSIVE"


def test_unevaluable_concentration_or_temporal_is_inconclusive_not_fail():
    ev = good_eval()
    ev.P["P8d"] = {"evaluable": False, "pass": None}
    assert vd.ladder(ev, stat_reject=True).cls == "INCONCLUSIVE"
    ev.P["P9"] = {"evaluable": True, "pass": False}
    assert vd.ladder(ev, stat_reject=True).cls == "NO-GO"


def test_no_go_primary_label_and_feas_membership():
    ev = good_eval(config="H1-m1.5", kind="H1")
    ev.P["P1"] = {"evaluable": True, "pass": False}
    ev.P["P7"] = {"evaluable": True, "pass": False}
    ev.maker_bound = {"P1": True, "P3": True, "P5": True, "P6": True}
    v = vd.ladder(ev, stat_reject=False)
    assert v.cls == "NO-GO" and v.label == "NO-GO-PRIMARY"
    assert vd.feas_member(ev)  # P7 failure does not exclude FEAS
    ev.maker_bound = {"P1": True, "P3": True, "P5": False, "P6": True}
    assert not vd.feas_member(ev)
    ev.maker_bound = {"P1": False, "P3": True, "P5": True, "P6": True}
    assert vd.ladder(ev, stat_reject=False).label == "NO-GO" and not vd.feas_member(ev)


def test_holdout_decision_holm_over_exactly_four():
    evals = {c: good_eval(config=c, kind=("H1" if c.startswith("H1") else "H3")) for c in vd.FAMILY}
    for c, p in zip(vd.FAMILY, (0.011, 0.015, 0.5, 0.9)):
        evals[c].stats = {"p_go": p, "lb_ok": True}
    d = vd.holdout_decision(evals)
    assert d.holm["H3-k4"]["reject"] and d.holm["H3-k4"]["alpha"] == pytest.approx(0.0125)
    assert d.holm["H3-k6"]["reject"] and d.holm["H3-k6"]["alpha"] == pytest.approx(0.05 / 3)
    assert not d.holm["H1-m1.5"]["reject"]
    assert d.verdicts["H3-k4"].cls == "ADVANCE" and d.verdicts["H1-m1.5"].cls == "INCONCLUSIVE"
    with pytest.raises(ValueError):
        vd.holdout_decision({c: evals[c] for c in vd.FAMILY[:3]})


def test_p_go_is_intersection_union_over_two_blocks():
    rng = np.random.default_rng(0)
    net = rng.normal(0.001, 0.01, 120)
    carry = rng.normal(0.0002, 0.001, 120)
    s = stats6.p_go(net, carry, B=2000)
    assert s["p_net"] == max(s["p"]["net"].values()) and s["p_carry"] == max(s["p"]["carry"].values())
    assert s["p_go"] == max(s["p_net"], s["p_carry"]) and set(s["p"]["net"]) == {14, 28}


def test_newey_west_matches_the_bartlett_formula():
    rng = np.random.default_rng(1)
    x = rng.normal(0.001, 0.01, 200)
    n, L, m = len(x), 28, x.mean()
    d = x - m
    var = d @ d / n + sum(2 * (1 - l / (L + 1)) * (d[l:] @ d[:-l]) / n for l in range(1, L + 1))
    from math import erfc, sqrt
    assert stats6.newey_west_p(x) == pytest.approx(0.5 * erfc(m / sqrt(var / n) / sqrt(2)))


# --- forward looks (pure synthetic p-values) --------------------------------------------------------


def make_family(cand=("H3-k4", "H3-k6"), feas=("H1-m1.5",)):
    return vd.ForwardFamily(cand=tuple(cand), feas=tuple(feas))


def ladder_stub(final_go_if_rejected=True):
    def fn(c, rej):
        return vd.Verdict(c, "FINAL GO" if (rej and final_go_if_rejected) else "INCONCLUSIVE", "", [])
    return fn


def test_family_is_always_four_and_closed_or_untested_slots_get_p1():
    f = make_family()
    assert f.size == 4 and f.status["H1-m2.5"] == "CLOSED"
    r = f.look(90, {"H3-k4": 0.5, "H3-k6": 0.5}, ladder_stub(), nw_p={}, feasibility={"H1-m1.5": True})
    assert r["p"]["H1-m2.5"] == 1.0 and r["p"]["H1-m1.5"] == 1.0  # FEAS untested before 365
    assert all(len(r["holm"]) == 4 for _ in [0])


def test_denominator_never_shrinks_and_look_alphas():
    f = make_family()
    r = f.look(90, {"H3-k4": 0.0002, "H3-k6": 0.9}, ladder_stub(), nw_p={"H3-k4": (0.0, 0.0)}, feasibility={})
    assert r["alpha"] == 0.001 and r["holm"]["H3-k4"]["alpha"] == pytest.approx(0.001 / 4)
    assert f.status["H3-k4"] == "FINAL GO"
    r2 = f.look(180, {"H3-k6": 0.0004}, ladder_stub(), nw_p={"H3-k6": (0.0, 0.0)})
    assert r2["alpha"] == 0.001 and r2["p"]["H3-k4"] == 1.0  # already GO: p = 1, still in the family
    # sorted p: k6 0.0004 first -> threshold 0.001/4 = 0.00025 -> not rejected
    assert not r2["holm"]["H3-k6"]["reject"] and f.status["H3-k6"] == "PENDING"
    r3 = f.look(365, {"H3-k6": 0.0121, "H1-m1.5": 0.9}, ladder_stub())
    assert r3["alpha"] == 0.048 and r3["holm"]["H3-k6"]["alpha"] == pytest.approx(0.048 / 4)
    assert not r3["holm"]["H3-k6"]["reject"]


def test_interim_rejection_needs_newey_west_and_final_look_does_not():
    f = make_family(cand=("H3-k4",), feas=())
    f.look(90, {"H3-k4": 0.0001}, ladder_stub(), nw_p={"H3-k4": (1.0, 1.0)})
    assert f.status["H3-k4"] == "PENDING"
    f.look(180, {"H3-k4": 0.0001}, ladder_stub(), nw_p={})
    assert f.status["H3-k4"] == "PENDING"
    f.look(365, {"H3-k4": 0.0001}, ladder_stub())
    assert f.status["H3-k4"] == "FINAL GO"


def test_early_final_go_is_permanent_and_terminal_closure_at_365():
    f = make_family(cand=("H3-k4", "H3-k6"), feas=())
    f.look(90, {"H3-k4": 0.0001, "H3-k6": 0.9}, ladder_stub(), nw_p={"H3-k4": (0.0, 0.0)})
    f.look(180, {"H3-k6": 0.9}, ladder_stub(), nw_p={})
    f.look(365, {"H3-k6": 0.9}, ladder_stub())
    assert f.status["H3-k4"] == "FINAL GO" and f.status["H3-k6"] == "INCONCLUSIVE"
    with pytest.raises(vd.LookError):
        f.look(400, {}, ladder_stub())
    f.monitor_after_go("H3-k4", risk_breach=True)
    assert f.status["H3-k4"] == "GO REVOKED"


def test_feasibility_not_established_closes_the_slot_and_established_is_tested_only_at_365():
    f = make_family(cand=(), feas=("H1-m1.5", "H1-m2.5"))
    f.look(90, {}, ladder_stub(), nw_p={}, feasibility={"H1-m1.5": True, "H1-m2.5": False})
    assert f.status["H1-m2.5"] == "CLOSED" and f.status["H1-m1.5"] == "PENDING"
    r = f.look(180, {}, ladder_stub(), nw_p={})
    assert r["p"]["H1-m1.5"] == 1.0
    r3 = f.look(365, {"H1-m1.5": 0.001}, ladder_stub())
    assert r3["p"]["H1-m1.5"] == 0.001 and f.status["H1-m1.5"] == "FINAL GO"


def test_looks_must_follow_the_schedule():
    f = make_family()
    with pytest.raises(vd.LookError):
        f.look(180, {}, ladder_stub())


def test_model_invalid_or_kill_closes_at_an_interim_look_but_point_failures_do_not():
    f = make_family(cand=("H3-k4", "H3-k6"), feas=())

    def fn(c, rej):
        if c == "H3-k4":
            return vd.Verdict(c, "MODEL INVALID", "MODEL INVALID", ["MODEL INVALID: H2a gate INVALID"])
        return vd.Verdict(c, "NO-GO", "NO-GO", ["NO-GO: P1/P3 <= 0"])
    f.look(90, {"H3-k4": 0.5, "H3-k6": 0.5}, fn, nw_p={})
    assert f.status["H3-k4"] == "CLOSED" and f.status["H3-k6"] == "PENDING"


def test_feasibility_slot_is_never_tested_before_day_365_even_with_a_tiny_p():
    f = make_family(cand=(), feas=("H1-m1.5",))
    f.look(90, {"H1-m1.5": 1e-9}, ladder_stub(), nw_p={"H1-m1.5": (0.0, 0.0)}, feasibility={"H1-m1.5": True})
    r = f.look(180, {"H1-m1.5": 1e-9}, ladder_stub(), nw_p={"H1-m1.5": (0.0, 0.0)})
    assert r["p"]["H1-m1.5"] == 1.0 and f.status["H1-m1.5"] == "PENDING"
