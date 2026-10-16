"""Deterministic risk layer (S-7.3, S-0.6): exact triggers, precedence, state transitions, fail-closed use."""

from __future__ import annotations

import inspect
from dataclasses import replace

import pytest

from luna.v7.book import LONG, SHORT
from luna.v7.book.risk import TICK, ForcedExit, RiskLayer, RiskState
from luna.v7.timeutil import MIN, D, H
from tests.v7.book.helpers import T0, gate, leg

MID = T0 // D * D  # 00:00 UTC of the decision day
RL = RiskLayer()


def start(equity: float = 1.0, ts: int = MID) -> RiskState:
    """A run started at ``ts`` with its first 15-minute mark processed."""
    return RL.on_tick(RL.initial_state(equity, ts), ts, equity, (), {})[0]


def advance(state: RiskState, to: int, equity: float | None = None) -> RiskState:
    """Process every 15-minute mark after the last one up to ``to`` (flat book, constant equity)."""
    eq = state.peak if equity is None else equity
    ts = state.last_tick + TICK
    while ts <= to:
        state = RL.on_tick(state, ts, eq, (), {})[0]
        ts += TICK
    return state


def _decide(state, t, legs, gates, hs=None, marks=None, stale=False):
    hs = {lg.symbol: 5e-4 for lg in legs} if hs is None else hs
    marks = {lg.symbol: lg.avg_entry for lg in legs} if marks is None else marks
    return RL.pre_decision(advance(state, t), t, legs, gates, hs, marks, stale)


def test_leg_stop_cooldown():
    lo, sh = leg("AAA", LONG, avg=100.0), leg("BBB", SHORT, avg=100.0)
    s = start()
    ts = MID + 15 * MIN
    # long: 85.01 is a 14.99 % adverse move, 85 is exactly 15 % -> stop; short: 115 is 15 % adverse
    _, a = RL.on_tick(s, ts, 1.0, (lo, sh), {"AAA": 85.01, "BBB": 114.99})
    assert a.forced == ()
    s1, a = RL.on_tick(s, ts, 1.0, (lo, sh), {"AAA": 85.0, "BBB": 115.0})
    assert a.forced == (
        ForcedExit("AAA", LONG, "leg_stop", "AAA:1", True, ts),
        ForcedExit("BBB", SHORT, "leg_stop", "BBB:-1", True, ts),
    )
    assert s1.pending_cooldown == {"AAA": ts, "BBB": ts}  # recorded at the trigger
    # favourable moves never stop
    _, a = RL.on_tick(s, ts, 1.0, (lo, sh), {"AAA": 200.0, "BBB": 50.0})
    assert a.forced == ()
    # a missing / non-finite mark never invents a trigger; it is reported
    _, a = RL.on_tick(s, ts, 1.0, (lo,), {"AAA": float("nan")})
    assert a.forced == () and a.notes == ("unpriced:AAA",)
    # cooldown: 24 h from the exit fill; blocks re-entry before, allows at exactly fill + 24 h
    fill = ts + 2 * MIN
    s2 = RL.start_cooldown(RL.start_cooldown(s1, "AAA", fill), "BBB", fill)
    assert s2.cooldown_until["AAA"] == fill + 24 * H and s2.pending_cooldown == {}
    g = {"AAA": gate()}
    _, a = _decide(s2, fill + 24 * H - MIN, (), g)
    assert not a.allows_entry("AAA") and a.blocked["AAA"] == "cooldown"
    assert a.allows_entry("ZZZ")
    _, a = _decide(s2, fill + 24 * H, (), g)
    assert a.allows_entry("AAA")
    # a later start never shortens a running cooldown
    assert RL.start_cooldown(s2, "AAA", fill - H).cooldown_until["AAA"] == fill + 24 * H
    # a leg-stop exit that left the book without start_cooldown is detected (fail closed)
    s3 = RL.start_cooldown(s1, "BBB", fill)
    with pytest.raises(ValueError, match="start_cooldown"):
        _decide(s3, MID + 8 * H + 5 * MIN, (), {})
    # while the stopped leg is still held (pending fill) the decision runs and does not re-issue it
    pend = leg("AAA", LONG, pending="leg_stop")
    _, a = _decide(s3, MID + 8 * H + 5 * MIN, (pend,), {"AAA": gate()}, marks={"AAA": 50.0})
    assert a.forced == ()


def test_daily_loss_pause():
    s = start()
    assert s.day_start_equity == 1.0
    s = advance(s, MID + 3 * H - TICK, 1.0)
    s, a = RL.on_tick(s, MID + 3 * H, 0.981, (), {})
    assert not a.paused
    s, a = RL.on_tick(s, MID + 3 * H + 15 * MIN, 0.98, (), {})  # exactly 98 % -> paused
    assert a.paused and s.paused_until == MID + D
    s = advance(s, MID + 4 * H - TICK, 1.0)
    s, a = RL.on_tick(s, MID + 4 * H, 1.05, (), {})  # recovery the same day does not lift the pause
    assert a.paused
    lo = leg("AAA", LONG)
    _, a = _decide(s, MID + 8 * H + 5 * MIN, (lo,), {"AAA": gate(), "NEW": gate()})
    assert a.paused and not a.entries_open and not a.allows_entry("NEW")
    # exits still run while paused (U6 here)
    _, a = _decide(s, MID + 8 * H + 5 * MIN, (lo,), {"AAA": gate(u6=False)})
    assert [f.reason for f in a.forced] == ["u6"]
    # next 00:00 tick: new day-start equity, pause lifted (0.97 is not <= 0.98 * 0.97)
    s = advance(s, MID + D - TICK, 1.0)
    s, a = RL.on_tick(s, MID + D, 0.97, (), {})
    assert not a.paused and s.day_start_equity == 0.97 and s.paused_until is None
    s, a = RL.on_tick(s, MID + D + 15 * MIN, 0.9506, (), {})  # 0.98 * 0.97 = 0.9506
    assert a.paused and s.paused_until == MID + 2 * D
    # ticks must be consecutive 15-minute marks: off-grid, repeated or skipped marks raise
    for bad in (MID + D + 20 * MIN, MID + D + 15 * MIN, MID + D + 45 * MIN):
        with pytest.raises(ValueError):
            RL.on_tick(s, bad, 1.0, (), {})
    # a decision needs the mark at or before t processed
    with pytest.raises(ValueError, match="on_tick"):
        RL.pre_decision(s, MID + D + 35 * MIN, (), {}, {}, {}, False)
    with pytest.raises(ValueError, match="on_tick"):
        RL.pre_decision(RL.initial_state(1.0, MID), MID + 5 * MIN, (), {}, {}, {}, False)
    RL.pre_decision(s, MID + D + 29 * MIN, (), {}, {}, {}, False)  # 14 min after the last mark: fine


def test_drawdown_kill_no_reentry():
    s = start()
    s, a = RL.on_tick(s, MID + 15 * MIN, 1.2, (), {})
    assert s.peak == 1.2 and not a.killed
    s, a = RL.on_tick(s, MID + 30 * MIN, 1.0201, (), {})  # 0.85 * 1.2 = 1.02
    assert not a.killed
    lo, sh = leg("AAA", LONG), leg("BBB", SHORT)
    s, a = RL.on_tick(s, MID + 45 * MIN, 1.02, (lo, sh), {"AAA": 100.0, "BBB": 100.0})
    assert a.killed and s.killed
    got = [(f.symbol, f.reason, f.cooldown, f.trigger_ts) for f in a.forced]
    assert got == [("AAA", "kill", False, MID + 45 * MIN), ("BBB", "kill", False, MID + 45 * MIN)]
    # equity recovers: the kill is permanent, every later decision exits everything, no re-entry
    s, a = RL.on_tick(s, MID + 60 * MIN, 2.0, (lo,), {"AAA": 100.0})
    assert a.killed and [f.reason for f in a.forced] == ["kill"]
    _, a = _decide(s, MID + 8 * H + 5 * MIN, (lo,), {"AAA": gate(), "NEW": gate()})
    assert a.killed and not a.entries_open and not a.allows_entry("NEW")
    assert [(f.symbol, f.reason) for f in a.forced] == [("AAA", "kill")]


def test_risk_precedence_and_ml_cannot_override():
    """kill > leg stop > U6 > ineligibility (two consecutive) > daily-loss pause > U8 / staleness."""
    s = start()
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
    got = [(f.symbol, f.reason, f.trigger_ts) for f in a.forced]
    assert got == [("AAA", "leg_stop", t1), ("BBB", "u6", t1)]
    assert s1.inelig_streak == {"AAA": 1, "BBB": 1, "CCC": 1, "DDD": 0, "EEE": 0}
    assert a.blocked["NEW"] == "u8" and a.blocked["DDD"] == "u8"
    s1 = RL.start_cooldown(s1, "AAA", t1 + 16 * MIN)  # the engine's fill hook for the leg stop
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
    assert a.entry_block == "daily_loss"
    sk = replace(s, killed=True)
    _, a = _decide(sk, t1, legs, gates, hs, marks)
    assert {f.reason for f in a.forced} == {"kill"} and len(a.forced) == 5 and a.entry_block == "kill"
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


def test_skip_tick_a3_advances_the_sequence_and_defers_the_day_roll():
    """A-3 (forward replay only): a skipped stale-quarter tick evaluates nothing; a skipped 00:00 UTC tick
    defers the day roll (and the daily-loss reset) to the next marked tick; without the skip a missing 00:00
    mark still fails closed."""
    s = advance(start(), MID + D - TICK)
    s = replace(s, paused_until=MID + D)  # a daily-loss pause running until the next 00:00
    with pytest.raises(ValueError):
        RL.on_tick(s, MID + D + TICK, 1.0, [], {})  # the 00:00 mark is missing (no skip)
    sk = RL.skip_tick(s, MID + D)
    assert sk.last_tick == MID + D and sk.day_roll_deferred and sk.day == s.day and sk.peak == s.peak
    s2, acts = RL.on_tick(sk, MID + D + TICK, 0.9, [], {})
    assert s2.day == (MID + D) // D and s2.day_start_equity == 0.9 and not s2.day_roll_deferred
    assert s2.paused_until is None and not acts.forced
    with pytest.raises(ValueError):
        RL.skip_tick(s2, MID + D + 3 * TICK)  # the sequence check stays
