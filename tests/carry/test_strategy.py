"""The pure decision function: target portfolio, cost-aware switching, hysteresis, neutrality."""

from __future__ import annotations

import dataclasses
import math

import pandas as pd
import pytest

from luna.carry.signals import AssetSignal
from luna.carry.strategy import DecisionInput, HeldLeg, decide
from tests.carry.helpers import T0, carry_config

E = 265.0
N = 66.25  # 0.5 / k x equity, k = 2
HOUR = pd.Timedelta("1h")
COST = 0.0005 + 0.0001 + 0.0002  # taker fee + 1 bp half spread + 2 bps slippage


def sig(asset: str, fl: float, *, eligible: bool = True, cost: float = COST, reasons=()) -> AssetSignal:
    return AssetSignal(
        asset=asset,
        eligible=eligible,
        reasons=tuple(reasons) if not eligible else (),
        fl=fl,
        fl_n=720,
        fl_short=fl,
        current_rel=fl,
        pred_rel=fl,
        rank=None,
        pct=None,
        persistence=1.0,
        mid=100.0,
        mark=100.0,
        spread_full=2e-4,
        half_spread=1e-4,
        top_book_usd=1e6,
        depth_usd=1e6,
        volume_usd=1e8,
        quote_age_s=1.0,
        side_cost=cost,
    )


def sigs(**fls: float) -> dict[str, AssetSignal]:
    out = {a: sig(a, f) for a, f in fls.items()}
    elig = sorted((a for a in out if out[a].eligible), key=lambda a: (-out[a].fl, a))
    return {a: dataclasses.replace(s, rank=elig.index(a) + 1 if a in elig else None) for a, s in out.items()}


def leg(asset: str, side: int, age_h: float = 48.0, notional: float = N) -> HeldLeg:
    return HeldLeg(
        asset=asset, side=side, notional=notional, entry_ts=T0 - age_h * HOUR, position_id=f"pos-{asset}"
    )


def run(signals, legs=(), cfg=None, **kw):
    cfg = cfg or carry_config()
    return decide(DecisionInput(now=T0, equity=E, signals=signals, legs=tuple(legs), **kw), cfg)


def book(decision, legs=()) -> dict[str, int]:
    """Holdings after applying the decision's actions to ``legs``."""
    out = {lg.asset: lg.side for lg in legs}
    for a in decision.actions:
        for o in a.legs:
            if o.intent == "open":
                out[o.asset] = o.side
            elif o.intent == "close":
                out.pop(o.asset, None)
    return out


# --- target portfolio -------------------------------------------------------------------------


def test_flat_book_shorts_highest_and_longs_lowest_funding() -> None:
    s = sigs(A=5e-5, B=4e-5, C=1e-5, D=0.0, E=-1e-5, F=-3e-5)
    d = run(s)
    assert book(d) == {"A": -1, "B": -1, "F": 1, "E": 1}
    assert d.ideal_short == ["A", "B"] and d.ideal_long == ["F", "E"]
    assert [a.decision for a in d.actions] == ["ENTER", "ENTER"]
    assert all(len(a.legs) == 2 for a in d.actions)  # entries are long+short pairs


def test_k2_entry_legs_are_half_over_k_of_equity_each() -> None:
    d = run(sigs(A=5e-5, B=4e-5, C=1e-5, D=0.0, E=-1e-5, F=-3e-5))
    opens = [o for a in d.actions for o in a.legs]
    assert [o.notional for o in opens] == pytest.approx([N] * 4)
    assert sum(o.side * o.notional for o in opens) == pytest.approx(0.0)  # dollar-neutral


def test_top_k_alone_is_not_enough_entry_needs_positive_carry_after_costs() -> None:
    # funding spread 2e-7/h x 168h = 0.34 bps per leg pair, far below fees + spread
    d = run(sigs(A=1.2e-6, B=1.1e-6, C=1.0e-6, D=1.0e-6))
    assert d.actions == []
    rec = [r for r in d.records if r.decision == "HOLD" and r.reason == "entry_carry_below_cost"]
    assert rec and rec[0].value < 0 and rec[0].switch_cost > 0


def test_negative_funding_asset_is_the_long() -> None:
    d = run(sigs(A=1e-5, B=0.5e-5, C=-8e-5, D=-9e-5))
    assert book(d) == {"A": -1, "B": -1, "C": 1, "D": 1}


# --- hysteresis / cost-aware switching ---------------------------------------------------------


HELD = [leg("A", -1), leg("B", -1), leg("E", 1), leg("F", 1)]


def test_small_rank_change_holds() -> None:
    # C overtakes B by 1e-7/h: rank 2 <-> 3, but 0.17 bps over the horizon
    s = sigs(A=5e-5, B=4.00e-5, C=4.01e-5, D=0.0, E=-1e-5, F=-3e-5)
    d = run(s, HELD)
    assert d.actions == []
    holds = {r.asset_out: r.reason for r in d.records if r.decision == "HOLD" and r.asset_out}
    assert holds["B"] == "below_min_improvement"


def test_large_carry_improvement_switches() -> None:
    s = sigs(A=5e-5, B=4e-5, C=3e-4, D=0.0, E=-1e-5, F=-3e-5)
    d = run(s, HELD)
    assert len(d.actions) == 1
    a = d.actions[0]
    assert a.decision == "SWITCH" and a.side == -1 and a.asset_out == "B" and a.asset_in == "C"
    assert [(o.asset, o.intent) for o in a.legs] == [("B", "close"), ("C", "open")]
    assert a.incremental_carry == pytest.approx(N * (3e-4 - 4e-5) * 168)
    assert a.switch_cost == pytest.approx(N * COST + N * COST)
    assert a.value == pytest.approx(a.incremental_carry - 1.5 * a.switch_cost - N * 5e-4)
    assert book(d, HELD) == {"A": -1, "C": -1, "E": 1, "F": 1}


def test_switch_cost_above_advantage_holds() -> None:
    # improvement 10 bps over the horizon (above min 5 bps), cost 1.5 x 16 bps + 5 bps buffer = 29 bps
    delta = 10e-4 / 168
    s = sigs(A=5e-5, B=4e-5, C=4e-5 + delta, D=0.0, E=-1e-5, F=-3e-5)
    d = run(s, HELD)
    assert d.actions == []
    r = next(r for r in d.records if r.asset_out == "B")
    assert r.decision == "HOLD" and r.reason == "cost_exceeds_advantage"
    assert r.incremental_carry == pytest.approx(N * 10e-4)
    # the same advantage with a near-free switch does switch: cost is the binding gate
    cheap = carry_config(switch_cost_multiplier=0.1, safety_buffer_bps=0.0)
    assert [a.decision for a in run(s, HELD, cfg=cheap).actions] == ["SWITCH"]


def test_min_hold_blocks_economic_switch() -> None:
    s = sigs(A=5e-5, B=4e-5, C=3e-4, D=0.0, E=-1e-5, F=-3e-5)
    young = [leg("A", -1), leg("B", -1, age_h=2), leg("E", 1), leg("F", 1)]
    d = run(s, young)
    assert d.actions == []
    assert next(r for r in d.records if r.asset_out == "B").reason == "min_hold"


def test_held_leg_inside_the_rank_buffer_is_kept() -> None:
    s = sigs(A=5e-5, B=4e-5, C=3e-4, D=0.0, E=-1e-5, F=-3e-5)  # B is short rank 3
    d = run(s, HELD, cfg=carry_config(hold_rank_buffer=1))
    assert d.actions == []
    assert next(r for r in d.records if r.asset_out == "B").reason == "in_target_band"


def test_switch_only_when_allowed_by_trigger() -> None:
    s = sigs(A=5e-5, B=4e-5, C=3e-4, D=0.0, E=-1e-5, F=-3e-5)
    assert run(s, HELD, allow_rebalance=False).actions == []


def test_cooldown_asset_is_never_entered() -> None:
    s = sigs(A=5e-5, B=4e-5, C=3e-4, D=0.0, E=-1e-5, F=-3e-5)
    d = run(s, HELD, blocked=frozenset({"C"}))
    assert all(a.asset_in != "C" for a in d.actions)


# --- forced exits and neutrality ---------------------------------------------------------------


def test_forced_exit_then_restore_with_the_best_replacement() -> None:
    s = sigs(A=5e-5, B=4e-5, C=3e-5, D=0.0, E=-1e-5, F=-3e-5)
    d = run(s, HELD, forced={"A": "stop"})
    kinds = [(a.decision, a.reason, a.asset_out, a.asset_in) for a in d.actions]
    assert kinds[0] == ("EXIT", "stop", "A", None)
    assert kinds[1] == ("ENTER", "neutrality_restore", None, "C")
    assert book(d, HELD) == {"B": -1, "C": -1, "E": 1, "F": 1}


def test_forced_exit_without_a_replacement_unwinds_the_other_side() -> None:
    s = sigs(A=5e-5, B=4e-5, E=-1e-5, F=-3e-5)  # every other asset is held
    s["C"] = sig("C", 3e-5, eligible=False, reasons=("wide_spread",))
    d = run(s, HELD, forced={"A": "stop"})
    assert [(a.decision, a.reason) for a in d.actions] == [("EXIT", "stop"), ("EXIT", "neutrality_unwind")]
    # the long that earns least (pays most funding) goes: E (-1e-5) carries -1e-5 vs F +3e-5
    assert d.actions[1].asset_out == "E"
    assert book(d, HELD) == {"B": -1, "F": 1}


def test_opens_not_allowed_only_exits() -> None:
    s = sigs(A=5e-5, B=4e-5, C=3e-4, D=0.0, E=-1e-5, F=-3e-5)
    d = run(s, HELD, forced={"A": "kill_switch"}, opens_allowed=False)
    assert all(o.intent != "open" for a in d.actions for o in a.legs)
    assert [a.decision for a in d.actions] == ["EXIT", "EXIT"]  # forced + neutrality unwind


def test_funding_edge_reversal_exits_the_pair() -> None:
    # the held short now pays funding and the held long now pays more: no eligible replacement
    s = sigs(A=-4e-5, B=4e-5, E=-1e-5, F=5e-5)
    legs = [leg("A", -1), leg("B", -1), leg("E", 1), leg("F", 1)]
    d = run(s, legs)
    assert [a.decision for a in d.actions] == ["EXIT"]
    a = d.actions[0]
    assert a.reason == "funding_edge_reversed" and {o.asset for o in a.legs} == {"A", "F"}
    assert a.value > 0


def test_net_exposure_drift_is_trimmed_on_the_heavier_side() -> None:
    s = sigs(A=5e-5, B=4e-5, E=-1e-5, F=-3e-5)
    legs = [
        leg("A", -1, notional=50.0),
        leg("B", -1, notional=50.0),
        leg("E", 1, notional=90.0),
        leg("F", 1, notional=80.0),
    ]
    d = run(s, legs)  # net +70 > 10% x 265
    assert len(d.actions) == 1
    a = d.actions[0]
    assert a.decision == "EXIT" and a.reason == "net_exposure_trim"
    (o,) = a.legs
    assert o.asset == "E" and o.intent == "reduce" and o.notional == pytest.approx(70.0 - 26.5)


# --- rebalance-to-target mode (V4 weekly baseline semantics) ----------------------------------


def test_rebalance_to_target_ignores_costs_at_its_trigger() -> None:
    cfg = carry_config(decision_mode="rebalance_to_target")
    s = sigs(A=5e-5, B=4.00e-5, C=4.01e-5, D=0.0, E=-1e-5, F=-3e-5)
    d = run(s, HELD, cfg=cfg)
    assert book(d, HELD) == {"A": -1, "C": -1, "E": 1, "F": 1}
    assert run(s, HELD, cfg=cfg, allow_rebalance=False).actions == []


# --- observability / determinism ---------------------------------------------------------------


def test_every_held_leg_gets_a_record_with_economics() -> None:
    s = sigs(A=5e-5, B=4e-5, C=4.01e-5, D=0.0, E=-1e-5, F=-3e-5)
    d = run(s, HELD)
    assert {r.asset_out for r in d.records if r.decision == "HOLD"} >= {"A", "B", "E", "F"}
    r = next(r for r in d.records if r.asset_out == "B")
    for f in (
        "expected_carry",
        "current_carry",
        "incremental_carry",
        "switch_cost",
        "safety_buffer",
        "value",
    ):
        assert math.isfinite(getattr(r, f))
    assert d.book_carry_h == pytest.approx(N * (5e-5 + 4e-5 - (-1e-5) - (-3e-5)))


def test_decide_is_deterministic() -> None:
    s = sigs(A=5e-5, B=5e-5, C=5e-5, D=-5e-5, E=-5e-5, F=-5e-5)  # ties broken by name
    assert run(s) == run(s)
    assert book(run(s)) == {"A": -1, "B": -1, "D": 1, "E": 1}
