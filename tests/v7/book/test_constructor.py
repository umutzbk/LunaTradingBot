"""Deterministic constructor (S-7.1, S-0.6): exact orders on synthetic books."""

from __future__ import annotations

from dataclasses import replace

import pytest

from luna.v7.book import LONG, SHORT, Leg, Order
from luna.v7.book.constructor import Targets, targets
from luna.v7.book.risk import RiskLayer
from luna.v7.timeutil import MIN, D
from tests.v7.book.helpers import BIG_QV, E_REF, T0, E, gate, leg, scores

RL = RiskLayer()


def run(
    held: tuple[Leg, ...] = (),
    rows: list[tuple[str, int, float, bool]] | None = None,
    gates: dict | None = None,
    *,
    halted: dict | None = None,
    betas: dict | None = None,
    qv: dict | None = None,
    equity: float = E,
    h_star: float = 24.0,
    state=None,
    stale: bool = False,
    no_gate: tuple[str, ...] = (),
) -> Targets:
    rows = rows or []
    syms = sorted({r[0] for r in rows} | {lg.symbol for lg in held})
    g = {s: gate() for s in syms if s not in no_gate} | (gates or {})
    st = state if state is not None else RL.initial_state(equity, T0 // D * D)
    _, risk = RL.pre_decision(
        st, T0, held, g, {s: 5e-4 for s in syms}, {lg.symbol: lg.avg_entry for lg in held}, stale
    )
    return targets(
        t=T0,
        equity=equity,
        held=held,
        scores=scores(rows),
        gates=g,
        risk=risk,
        halted={s: False for s in syms} | (halted or {}),
        betas={s: 1.0 for s in syms} | (betas or {}),
        quote_vol_24h={s: BIG_QV for s in syms} | (qv or {}),
        h_star_h=h_star,
        usd_per_unit=E_REF,
    )


def opens(tg: Targets) -> dict[tuple[str, int], float]:
    return {(o.symbol, o.side): round(o.notional, 12) for o in tg.orders if o.kind == "open"}


def closes(tg: Targets) -> dict[tuple[str, int], str]:
    return {(o.symbol, o.side): o.reason for o in tg.orders if o.kind == "close"}


def side_rows(prefix: str, side: int, n: int, fail: tuple[int, ...] = ()) -> list:
    """n candidates ranked 1..n (rank_key n..1); ranks in ``fail`` do not pass the hurdle."""
    return [(f"{prefix}{i}", side, float(n - i + 1), i not in fail) for i in range(1, n + 1)]


def test_top_k_and_hurdle():
    rows = side_rows("L", LONG, 7, fail=(2,)) + side_rows("S", SHORT, 7, fail=(5,))
    tg = run(rows=list(reversed(rows)))  # input order is irrelevant: the ranking is re-derived
    # top K = 5 by score; a hurdle-failing slot is NOT backfilled by rank 6 (A1 literal reading, AMB-16)
    assert opens(tg) == {
        **{(f"L{i}", LONG): 0.1 for i in (1, 3, 4, 5)},
        **{(f"S{i}", SHORT): 0.1 for i in (1, 2, 3, 4)},
    }
    assert "hurdle:L2" in tg.notes and "hurdle:S5" in tg.notes
    # ties are broken by symbol
    tie = [("BBB", LONG, 1.5, True), ("AAA", LONG, 1.5, True), ("CCC", LONG, 1.5, True)]
    tie += [("CCC", SHORT, 9.0, False), ("DDD", SHORT, 9.0, False)] + side_rows("S", SHORT, 3)
    tie += side_rows("X", LONG, 4)  # X1..X3 rank above the tied symbols, X4 below them
    tg = run(rows=tie)
    assert sorted(s for s, sd in opens(tg) if sd == LONG) == ["AAA", "BBB", "X1", "X2", "X3"]
    # held legs keep their slots: entrants only fill free slots, in rank order, and only if top K
    held = tuple(leg(f"H{i}", LONG) for i in range(1, 4)) + tuple(leg(f"S{i}", SHORT) for i in range(1, 6))
    rows = [("N1", LONG, 10.0, True), ("H1", LONG, 9.0, True), ("N2", LONG, 8.0, True)]
    rows += [("H2", LONG, 7.0, True), ("N3", LONG, 6.0, True), ("H3", LONG, 5.0, True)]
    rows += [("N4", LONG, 4.0, True)]
    rows += side_rows("S", SHORT, 5)
    tg = run(held, rows)
    # ranks: N1 1, H1 2, N2 3, H2 4, N3 5, H3 6 (kept, <= K + 2), N4 7 (not top K)
    assert opens(tg) == {("N1", LONG): 0.1, ("N2", LONG): 0.1}
    assert "no_slot:N3" in tg.notes and closes(tg) == {}
    assert tg.post == {
        **{s: 0.1 for s in ("N1", "N2", "H1", "H2", "H3")},
        **{f"S{i}": -0.1 for i in range(1, 6)},
    }


def test_no_same_decision_flip():
    held = (leg("XXX", LONG),)  # past min hold, not ranked on the long side -> decision exit
    rows = [("XXX", SHORT, 10.0, True), ("YYY", SHORT, 9.0, True), ("YYY", LONG, 9.0, True)]
    rows += [("ZZZ", LONG, 1.0, True), ("WWW", SHORT, 1.0, True)]
    tg = run(held, rows)
    assert closes(tg) == {("XXX", LONG): "rank"}
    # XXX may not enter short while it is held long; YYY qualifies on both sides -> neither side enters
    assert opens(tg) == {("ZZZ", LONG): 0.1, ("WWW", SHORT): 0.1}
    assert "flip:XXX" in tg.notes and "both_sides:YYY" in tg.notes


def test_equal_weight_and_neutrality():
    # 5 longs vs 3 shorts: shorts 0.1 each (per-leg cap 0.5 E / K), longs scaled to the short gross
    tg = run(rows=side_rows("L", LONG, 5) + side_rows("S", SHORT, 3))
    o = opens(tg)
    assert {k: v for k, v in o.items() if k[1] == SHORT} == {(f"S{i}", SHORT): 0.1 for i in (1, 2, 3)}
    assert {k: v for k, v in o.items() if k[1] == LONG} == {(f"L{i}", LONG): 0.06 for i in range(1, 6)}
    # liquidity cap 0.5 % of 24 h quote volume at E_ref: qv 80,000 USD -> 400 USD -> 0.04 E
    tg = run(rows=side_rows("L", LONG, 5) + side_rows("S", SHORT, 5), qv={"L1": 80_000.0})
    o = opens(tg)
    assert o[("L1", LONG)] == 0.04 and all(o[(f"L{i}", LONG)] == 0.1 for i in range(2, 6))
    assert all(o[(f"S{i}", SHORT)] == round(0.1 * 0.44 / 0.5, 12) for i in range(1, 6))
    long_g = sum(v for k, v in o.items() if k[1] == LONG)
    short_g = sum(v for k, v in o.items() if k[1] == SHORT)
    assert abs(long_g - short_g) <= 1e-12 and long_g + short_g <= 1.0
    # inside the 5 % tolerance nothing is scaled: L = 0.498, S = 0.5
    tg = run(rows=side_rows("L", LONG, 5) + side_rows("S", SHORT, 5), qv={"L1": 196_000.0})
    o = opens(tg)
    assert o[("L1", LONG)] == 0.098 and o[("S1", SHORT)] == 0.1
    # equity scales the targets; no target ever exceeds 0.5 E / K; gross <= 1.0 E
    tg = run(rows=side_rows("L", LONG, 5) + side_rows("S", SHORT, 5), equity=2.0)
    assert set(opens(tg).values()) == {0.2}
    assert sum(abs(v) for v in tg.post.values()) == pytest.approx(2.0, abs=1e-12)
    # a missing liquidity input or beta blocks the entry (fail closed), slot not backfilled
    rows = side_rows("L", LONG, 6) + side_rows("S", SHORT, 6)
    tg = run(rows=rows, qv={"L1": float("nan")}, betas={"S2": None})
    o = opens(tg)
    assert ("L1", LONG) not in o and ("L6", LONG) not in o and ("S2", SHORT) not in o
    assert "liquidity_missing:L1" in tg.notes and "beta_missing:S2" in tg.notes
    assert len([k for k in o if k[1] == LONG]) == 4 and set(o.values()) == {0.1}


def test_empty_side_keeps_book_flat():
    # long candidates only: no order at all, the book stays flat
    tg = run(rows=side_rows("L", LONG, 5))
    assert tg.orders == () and tg.post == {}
    assert all(f"neutrality:L{i}" in tg.notes for i in range(1, 6))
    # held one-sided legs inside the minimum hold are reduced to flat (conservative, AMB-16)
    held = (leg("AAA", LONG, age_h=8), leg("BBB", LONG, age_h=8), leg("CCC", LONG, age_h=8))
    tg = run(held, side_rows("L", LONG, 5) + [("AAA", LONG, 0.5, True)], halted={"CCC": True})
    assert closes(tg) == {(s, LONG): "neutrality" for s in ("AAA", "BBB", "CCC")}
    assert [o.halted for o in tg.orders] == [False, False, True] and opens(tg) == {}
    assert tg.post == {"CCC": 0.1}  # a halted exit is pending: the leg is still held
    # the short side empties through forced exits: the long side follows to flat
    held = (leg("AAA", LONG), leg("SSS", SHORT))
    tg = run(held, [("AAA", LONG, 1.0, True), ("SSS", SHORT, 1.0, True)], gates={"SSS": gate(u6=False)})
    assert closes(tg) == {("SSS", SHORT): "u6", ("AAA", LONG): "neutrality"}


def test_min_hold_blocks_non_risk_exit():
    shorts = tuple(leg(f"S{i}", SHORT) for i in range(1, 4))
    srows = side_rows("S", SHORT, 3)
    rows = srows + [("BBB", LONG, 9.0, False), ("DDD", LONG, 8.0, True)]
    gates = {"AAA": gate(u2=False), "DDD": gate(u8=False), "CCC": gate(u6=False)}

    def book(age):
        return shorts + (leg("AAA", LONG, age_h=age), leg("BBB", LONG, age_h=age), leg("DDD", LONG, age_h=72))

    # before h* = 24 h: gate failure (U2), rank outside K + 2 and a failed hurdle do not exit
    tg = run(book(23.9), rows, gates)
    assert tg.orders == ()
    # a risk exit (U6) fires inside the minimum hold
    tg = run(book(23.9) + (leg("CCC", LONG, age_h=1),), rows + [("CCC", LONG, 99.0, True)], gates)
    o = [x for x in tg.orders if x.symbol == "CCC"]
    assert len(o) == 1 and o[0].kind == "close" and o[0].reason == "u6" and o[0].forced
    # at exactly h*: the decision exits fire; U8 false is never an exit trigger
    tg = run(book(24.0), rows, gates)
    assert closes(tg) == {("AAA", LONG): "gate", ("BBB", LONG): "hurdle"}
    assert not any(o.forced for o in tg.orders)
    # rank outside K + 2 = 7 on its side exits; rank 7 stays
    many = side_rows("L", LONG, 8)
    held = tuple(leg(f"L{i}", LONG) for i in (1, 7, 8)) + shorts
    tg = run(held, many + srows)
    assert closes(tg) == {("L8", LONG): "rank"}


def test_resize_drift_rule():
    shorts = tuple(leg(f"S{i}", SHORT) for i in range(1, 6))
    longs = (
        leg("L1", LONG, 0.126),  # +26 % > 25 % -> reduce to the target
        leg("L2", LONG, 0.125),  # +25 % is not > 25 %
        leg("L3", LONG, 0.07),  # -30 %: increases are never made (reduce-only)
        leg("L4", LONG, 0.13),  # halted: no order of any kind
        leg("L5", LONG, 0.1),
    )
    rows = side_rows("L", LONG, 5) + side_rows("S", SHORT, 5)
    tg = run(longs + shorts, rows, halted={"L4": True})
    assert [(o.symbol, o.kind, round(o.notional, 12), o.target, o.reason) for o in tg.orders] == [
        ("L1", "reduce", 0.026, 0.1, "drift")
    ]
    assert "no_increase:L3" in tg.notes and "halted_resize:L4" in tg.notes
    assert tg.post["L1"] == 0.1 and tg.post["L3"] == 0.07 and tg.post["L4"] == 0.13
    # equity 0.4 -> per-leg 0.04; 2 longs vs 1 short -> longs scaled to 0.02 each
    tg = run((leg("L1", LONG, 0.1), leg("L2", LONG, 0.1001), leg("S1", SHORT, 0.1)),
             [("L1", LONG, 2.0, True), ("L2", LONG, 1.0, True), ("S1", SHORT, 1.0, True)], equity=0.4)
    assert {(o.symbol, o.kind, round(o.target, 12)) for o in tg.orders} == {
        ("L1", "reduce", 0.02), ("L2", "reduce", 0.02), ("S1", "reduce", 0.04)
    }


def test_risk_precedence_and_ml_cannot_override():
    """S-0.6: the score orders / removes only; it cannot add a gate-failing candidate, change a size or
    cancel a forced exit."""
    held = (leg("AAA", LONG, age_h=1), leg("SSS", SHORT), leg("TTT", SHORT))
    rows = [("AAA", LONG, 1e9, True), ("GGG", LONG, 1e8, True), ("NOG", LONG, 1e7, True)]
    rows += [("L1", LONG, 3.0, True), ("SSS", SHORT, 2.0, True), ("TTT", SHORT, 1.0, True)]
    gates = {"AAA": gate(u6=False), "GGG": gate(u3=False)}
    tg = run(held, rows, gates, no_gate=("NOG",))
    assert closes(tg) == {("AAA", LONG): "u6"}  # forced exit despite the top score inside min hold
    assert opens(tg) == {("L1", LONG): 0.1}  # GGG fails a gate, NOG has no gate row (fail closed)
    assert "gate:GGG" in tg.notes and "gate:NOG" in tg.notes
    # sizes do not depend on score values (only on the order): rescaled scores give identical orders
    rows2 = [(s, sd, k * 1000.0 + 5.0, p) for s, sd, k, p in rows]
    assert run(held, rows2, gates, no_gate=("NOG",)).orders == tg.orders
    # killed: everything exits with reason kill, no entry whatever the scores
    fresh = RL.initial_state(E, T0 // D * D)
    tg = run(held, rows, gates, state=replace(fresh, killed=True))
    assert set(closes(tg).values()) == {"kill"} and len(closes(tg)) == 3 and opens(tg) == {}
    assert all(o.forced for o in tg.orders)
    # daily-loss pause / staleness: entries blocked, held legs untouched
    held2 = held + (leg("BBB", LONG),)
    rows2 = rows + [("BBB", LONG, 2.5, True)]
    cases = (({"state": replace(fresh, paused_until=T0 + D)}, "daily_loss"), ({"stale": True}, "stale"))
    for kw, why in cases:
        tg = run(held2, rows2, {"AAA": gate()}, **kw)
        assert tg.orders == ()
        assert tg.notes[0] == f"entries_blocked:{why}"
    tg = run(held2, rows2, {"AAA": gate()}, state=replace(fresh, paused_until=T0 + D), stale=True)
    assert tg.notes[0] == "entries_blocked:daily_loss"  # precedence: daily-loss pause > staleness
    # an order is never emitted for a halted entrant; a halted forced exit is flagged, not dropped
    tg = run(held, rows, gates, halted={"AAA": True, "L1": True}, no_gate=("NOG",))
    assert opens(tg) == {} and [(o.symbol, o.halted) for o in tg.orders if o.side == LONG] == [("AAA", True)]
    assert "halted:L1" in tg.notes
    # risk actions are bound to their decision time
    _, other = RL.pre_decision(RL.initial_state(E, T0 // D * D), T0 + 8 * 60 * MIN, (), {}, {}, {}, False)
    with pytest.raises(ValueError):
        targets(t=T0, equity=E, held=(), scores=scores([]), gates={}, risk=other, halted={}, betas={},
                quote_vol_24h={}, h_star_h=24.0, usd_per_unit=E_REF)
    assert isinstance(tg.orders[0], Order)
