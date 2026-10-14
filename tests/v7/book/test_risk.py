"""Deterministic risk layer (S-7.3, S-0.6): exact triggers, precedence, state transitions."""

from __future__ import annotations

import inspect
from dataclasses import replace

import pytest

from luna.v7.book import LONG, SHORT
from luna.v7.book.risk import ForcedExit, RiskLayer
from luna.v7.timeutil import MIN, D, H
from tests.v7.book.helpers import T0, gate, leg

MID = T0 // D * D  # 00:00 UTC of the decision day
RL = RiskLayer()


def _decide(state, t, legs, gates, hs=None, marks=None, stale=False):
    hs = {lg.symbol: 5e-4 for lg in legs} if hs is None else hs
    marks = {lg.symbol: lg.avg_entry for lg in legs} if marks is None else marks
    return RL.pre_decision(state, t, legs, gates, hs, marks, stale)


def test_leg_stop_cooldown():
    lo, sh = leg("AAA", LONG, avg=100.0), leg("BBB", SHORT, avg=100.0)
    s = RL.initial_state(1.0, MID)
    # long: 85.01 is a 14.99 % adverse move, 85 is exactly 15 % -> stop; short: 115 is 15 % adverse
    _, a = RL.on_tick(s, MID + 15 * MIN, 1.0, (lo, sh), {"AAA": 85.01, "BBB": 114.99})
    assert a.forced == ()
    _, a = RL.on_tick(s, MID + 15 * MIN, 1.0, (lo, sh), {"AAA": 85.0, "BBB": 115.0})
    assert a.forced == (
        ForcedExit("AAA", LONG, "leg_stop", "AAA:1", True),
        ForcedExit("BBB", SHORT, "leg_stop", "BBB:-1", True),
    )
    # favourable moves never stop
    _, a = RL.on_tick(s, MID + 15 * MIN, 1.0, (lo, sh), {"AAA": 200.0, "BBB": 50.0})
    assert a.forced == ()
    # a missing / non-finite mark never invents a trigger; it is reported
    _, a = RL.on_tick(s, MID + 15 * MIN, 1.0, (lo,), {"AAA": float("nan")})
    assert a.forced == () and a.notes == ("unpriced:AAA",)
    # cooldown: 24 h from the exit fill; blocks re-entry before, allows at exactly fill + 24 h
    fill = MID + 16 * MIN
    s2 = RL.start_cooldown(s, "AAA", fill)
    assert s2.cooldown_until["AAA"] == fill + 24 * H
    g = {"AAA": gate()}
    _, a = _decide(s2, fill + 24 * H - MIN, (), g)
    assert not a.allows_entry("AAA") and a.blocked["AAA"] == "cooldown"
    assert a.allows_entry("ZZZ")
    _, a = _decide(s2, fill + 24 * H, (), g)
    assert a.allows_entry("AAA")
    # a later start never shortens a running cooldown
    assert RL.start_cooldown(s2, "AAA", fill - H).cooldown_until["AAA"] == fill + 24 * H


def test_daily_loss_pause():
    s = RL.initial_state(1.0, MID)
    assert s.day_start_equity == 1.0
    s, a = RL.on_tick(s, MID + 3 * H, 0.981, (), {})
    assert not a.paused
    s, a = RL.on_tick(s, MID + 3 * H + 15 * MIN, 0.98, (), {})  # exactly 98 % -> paused
    assert a.paused and s.paused_until == MID + D
    s, a = RL.on_tick(s, MID + 4 * H, 1.05, (), {})  # recovery the same day does not lift the pause
    assert a.paused
    lo = leg("AAA", LONG)
    s1, a = _decide(s, MID + 8 * H + 5 * MIN, (lo,), {"AAA": gate(), "NEW": gate()})
    assert a.paused and not a.entries_open and not a.allows_entry("NEW")
    # exits still run while paused (U6 here)
    _, a = _decide(s, MID + 8 * H + 5 * MIN, (lo,), {"AAA": gate(u6=False)})
    assert [f.reason for f in a.forced] == ["u6"]
    # next 00:00 tick: new day-start equity, pause lifted (0.97 is not <= 0.98 * 0.97)
    s, a = RL.on_tick(s, MID + D, 0.97, (), {})
    assert not a.paused and s.day_start_equity == 0.97 and s.paused_until is None
    s, a = RL.on_tick(s, MID + D + 15 * MIN, 0.9506, (), {})  # 0.98 * 0.97 = 0.9506
    assert a.paused and s.paused_until == MID + 2 * D
    # ticks must be on the 15-minute grid, increasing, and may not skip a 00:00 mark
    with pytest.raises(ValueError):
        RL.on_tick(s, MID + D + 20 * MIN, 1.0, (), {})
    with pytest.raises(ValueError):
        RL.on_tick(s, MID + D, 1.0, (), {})
    with pytest.raises(ValueError):
        RL.on_tick(s, MID + 3 * D + 15 * MIN, 1.0, (), {})


def test_drawdown_kill_no_reentry():
    s = RL.initial_state(1.0, MID)
    s, a = RL.on_tick(s, MID + 15 * MIN, 1.2, (), {})
    assert s.peak == 1.2 and not a.killed
    s, a = RL.on_tick(s, MID + 30 * MIN, 1.0201, (), {})  # 0.85 * 1.2 = 1.02
    assert not a.killed
    lo, sh = leg("AAA", LONG), leg("BBB", SHORT)
    s, a = RL.on_tick(s, MID + 45 * MIN, 1.02, (lo, sh), {"AAA": 100.0, "BBB": 100.0})
    assert a.killed and s.killed
    got = [(f.symbol, f.reason, f.cooldown) for f in a.forced]
    assert got == [("AAA", "kill", False), ("BBB", "kill", False)]
    # equity recovers: the kill is permanent, every later decision exits everything, no re-entry
    s, a = RL.on_tick(s, MID + 60 * MIN, 2.0, (lo,), {"AAA": 100.0})
    assert a.killed and [f.reason for f in a.forced] == ["kill"]
    _, a = _decide(s, MID + 8 * H + 5 * MIN, (lo,), {"AAA": gate(), "NEW": gate()})
    assert a.killed and not a.entries_open and not a.allows_entry("NEW")
    assert [(f.symbol, f.reason) for f in a.forced] == [("AAA", "kill")]


def test_risk_precedence_and_ml_cannot_override():
    """kill > leg stop > U6 > ineligibility (two consecutive) > daily-loss pause > U8 / staleness."""
    s = RL.initial_state(1.0, MID)
    a_, b_, c_, d_, e_ = (leg(x, LONG) for x in ("AAA", "BBB", "CCC", "DDD", "EEE"))
    legs = (a_, b_, c_, d_, e_)
    gates = {
        "AAA": gate(u6=False, u4=False),  # leg stop + U6 + inelig
        "BBB": gate(u6=False, u7=False),  # U6 + inelig
        "CCC": gate(u7=False),  # inelig only (first failing decision)
        "DDD": gate(u8=False),  # U8 false: never an exit trigger
        "EEE": gate(),
        "NEW": gate(u8=False),
    }
    marks = {"AAA": 80.0, "BBB": 100.0, "CCC": 100.0, "DDD": 100.0, "EEE": 100.0}
    hs = {"AAA": 5e-4, "BBB": 5e-4, "CCC": 5e-4, "DDD": 5e-4, "EEE": 15e-4}  # 15 bps < 20 bps exit threshold
    t1 = MID + 5 * MIN
    s1, a = _decide(s, t1, legs, gates, hs, marks)
    assert [(f.symbol, f.reason) for f in a.forced] == [("AAA", "leg_stop"), ("BBB", "u6")]
    assert s1.inelig_streak == {"AAA": 1, "BBB": 1, "CCC": 1, "DDD": 0, "EEE": 0}
    assert a.blocked["NEW"] == "u8" and a.blocked["DDD"] == "u8"
    # second consecutive failing decision: CCC fails U5 (25 bps > 20 bps) -> ineligibility exit
    t2 = t1 + 8 * H
    gates2 = {k: gate() for k in ("CCC", "DDD", "EEE")}
    s2, a = _decide(s1, t2, (c_, d_, e_), gates2, {"CCC": 25e-4, "DDD": 5e-4, "EEE": None})
    assert [(f.symbol, f.reason) for f in a.forced] == [("CCC", "ineligible")]
    assert s2.inelig_streak == {"CCC": 2, "DDD": 0, "EEE": 1}  # missing h-hat counts as failing (fail closed)
    # a passing decision resets the streak
    s3, a = _decide(s2, t2 + 8 * H, (e_,), {"EEE": gate()}, {"EEE": 5e-4})
    assert a.forced == () and s3.inelig_streak == {"EEE": 0}
    # paused + stale: entries blocked, exits still run, kill dominates every other reason
    sp = replace(s, paused_until=MID + D)
    _, a = _decide(sp, t1, (b_,), {"BBB": gate(u6=False)}, stale=True)
    assert a.paused and a.stale and not a.entries_open and [f.reason for f in a.forced] == ["u6"]
    sk = replace(s, killed=True)
    _, a = _decide(sk, t1, legs, gates, hs, marks)
    assert {f.reason for f in a.forced} == {"kill"} and len(a.forced) == 5
    # a pending exit (halted) is not re-issued
    _, a = _decide(s, t1, (leg("AAA", LONG, pending="u6"),), {"AAA": gate(u6=False)})
    assert a.forced == ()
    # a held leg without a gate row fails closed
    with pytest.raises(ValueError):
        _decide(s, t1, (e_,), {})
    # S-0.6 by type: no RiskLayer method has any score / rank / model input
    for name in ("initial_state", "on_tick", "pre_decision", "start_cooldown"):
        params = set(inspect.signature(getattr(RiskLayer, name)).parameters)
        assert not {p for p in params if any(w in p for w in ("score", "rank", "model", "ml", "yhat", "p_"))}
