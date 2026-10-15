"""User decision round 2 (docs/results/v7-impl/CLARIFICATIONS-2026-10-15-round2.md) at the engine level:
DK-10 (data-gap fills booked at the substitute bar), override 2 / DK-69 (a required non-halted exit without a
bar within 60 min -> INVALID RUN), override 3 / DK-70 (no stale leg mark -> INVALID RUN), DK-42 (the A1 veto
is entry-only), DK-77 (``maker_exit`` only while a maker limit rests), DK-78 (a flatten / the kill cancels an
in-flight hedge open). Synthetic inputs only."""

from dataclasses import replace

import numpy as np
import pandas as pd
import pytest

from luna.v7 import accounting
from luna.v7.book.engine import run
from luna.v7.book.execution import INVALID_EXIT_GAP, INVALID_MARK
from luna.v7.timeutil import MIN, D, H

from .engine_fixtures import BTC, END, LO, S0, base_inputs, gate_row, instrument

T1 = LO + 5 * MIN  # first decision
TE1 = T1 + 15 * MIN
T2 = LO + D + 5 * MIN  # first decision after h* = 24 h
NONE = pd.DataFrame({"ts": pd.Series(dtype="int64"), "price": [], "qty": []})  # E_M: no trade-through ever


def _of(r, sym, reason=None):
    f = r.fills[r.fills["symbol"] == sym]
    return f if reason is None else f[f["reason"] == reason]


def _drop15(ins, *opens):
    """Remove the 15m last-price AND mark bars opening at ``opens`` (no mark at the tick they end at)."""
    return replace(ins, bars15=ins.bars15[~ins.bars15["open_ts"].isin(opens)].reset_index(drop=True),
                   mark15=ins.mark15[~ins.mark15["open_ts"].isin(opens)].reset_index(drop=True))


def _aaa_gate_fails_at_t2(t, s):
    return gate_row(t, u2=False) if (s == "AAAUSDT" and t >= T2) else gate_row(t)


# --------------------------------------------------------------------------------------------- DK-10


def test_gap_fill_booked_at_substitute_bar_with_its_funding_and_b():
    """AAA has no 1m bar in [00:20, 01:05) (not halted): its entry is booked at 01:05 at that bar's open
    (DK-10), so the hourly 01:00 settlement is not AAA's; B(e) is BTC's reference at 01:05, not at 00:20."""
    gap = (TE1, LO + H + 5 * MIN)
    ts = np.arange(S0, END, H, dtype=np.int64)
    hourly = pd.DataFrame(dict(funding_ts=ts, rate=-1e-4, interval_h_explicit=pd.array([1] * len(ts),
                               dtype="Int64"), known_at=ts + MIN, mark_at_settle=100.0))
    aaa = replace(instrument("AAAUSDT", lambda t: np.where(t < gap[1], 100.0, 101.0), -1e-4, dead=(gap,)),
                  funding=hourly)
    btc = instrument(BTC, lambda t: np.where(t < LO + H, 1000.0, 1010.0), 5e-5)
    r = run("A0", replace(base_inputs(aaa=aaa), btc=btc), (LO, END), 24)
    assert r.valid
    e = _of(r, "AAAUSDT", "entry")
    assert e[["ts", "ref_mid", "btc_ref"]].values.tolist() == [[gap[1], 101.0, 1010.0]]
    assert "gap_price" in e["flags"].iloc[0]
    fa = r.funding_flows[r.funding_flows["symbol"] == "AAAUSDT"]
    assert fa["ts"].min() == LO + 2 * H  # 01:00 precedes the booked entry; 02:00 is the first settlement
    assert _of(r, "BBBUSDT", "entry")["ts"].tolist() == [TE1]  # BBB had P(00:20): booked at 00:20
    accounting.account(r)


# ------------------------------------------------------------------------------------ DK-69 override


def test_required_decision_exit_without_bar_is_invalid():
    """AAA's gate exit is due at t2 + 15 min; AAA traded in the hour before (not halted) but has no 1m bar in
    the following 61 min -> INVALID RUN naming symbol, instance and time (no S-5.4 stressed wait)."""
    te = T2 + 15 * MIN
    aaa = instrument("AAAUSDT", 100.0, -1e-4, dead=((te, te + 61 * MIN),))
    r = run("A0", base_inputs(aaa=aaa, gates=_aaa_gate_fails_at_t2), (LO, END), 24)
    assert r.invalid == INVALID_EXIT_GAP and r.invalid_ts == te
    assert r.invalid_detail.startswith(f"AAAUSDT#0@{te}: gate exit due at {te}")
    assert _of(r, "AAAUSDT", "gate").empty and not (r.fills["ts"] > te).any()
    # control: the bar at te + 60 min exists -> the exit fills there, at its own time (DK-10)
    ok = run("A0", base_inputs(aaa=instrument("AAAUSDT", 100.0, -1e-4, dead=((te, te + 60 * MIN),)),
                               gates=_aaa_gate_fails_at_t2), (LO, END), 24)
    assert ok.valid and _of(ok, "AAAUSDT", "gate")["ts"].tolist() == [te + 60 * MIN]


def test_required_forced_flatten_without_bar_is_invalid():
    """BBB (the only short) stops out at the 05:45 tick; the one-sided flatten of AAA is due on the first 1m
    bar >= 05:46 (S-4.2), but AAA has none within 60 min (not halted) -> INVALID RUN."""
    rise = LO + 5 * H + 30 * MIN
    trig = rise + 15 * MIN
    bbb = instrument("BBBUSDT", lambda ts: np.where(ts < rise, 50.0, 60.0), 1e-4)
    aaa = instrument("AAAUSDT", 100.0, -1e-4, dead=((trig + MIN, trig + 62 * MIN),))
    r = run("A0", base_inputs(aaa=aaa, bbb=bbb), (LO, END), 24)
    assert r.invalid == INVALID_EXIT_GAP and r.invalid_ts == trig + MIN
    assert r.invalid_detail.startswith(f"AAAUSDT#0@{trig + MIN}: one_sided_flatten exit due")


# ------------------------------------------------------------------------------------ DK-70 override


def test_leg_without_mark_at_tick_is_invalid():
    """AAA is held and not halted at the 02:00 tick, with neither a mark-price nor a last-price 15m bar
    ending there -> INVALID RUN (no stale carry of the previous price)."""
    tick = LO + 2 * H
    aaa = _drop15(instrument("AAAUSDT", 100.0, -1e-4), tick - 15 * MIN)
    r = run("A0", base_inputs(aaa=aaa), (LO, END), 24)
    assert r.invalid == INVALID_MARK and r.invalid_ts == tick
    assert r.invalid_detail.startswith(f"AAAUSDT#0@{tick}: no mark-price or last-price 15-minute close")
    assert r.marks["ts"].max() < tick and not (r.positions["mark_source"] == "stale").any()
    # the mark-price bar alone missing: the last-price close of the same period is used (S-4.6 chain)
    m_only = instrument("AAAUSDT", 100.0, -1e-4)
    m_only = replace(m_only, mark15=m_only.mark15[m_only.mark15["open_ts"] != tick - 15 * MIN])
    ok = run("A0", base_inputs(aaa=m_only), (LO, END), 24)
    assert ok.valid
    src = ok.positions.set_index(["ts", "symbol"])["mark_source"]
    assert src[(tick, "AAAUSDT")] == "last" and src[(tick - 15 * MIN, "AAAUSDT")] == "mark"


def test_halted_leg_without_mark_keeps_current_code_amb29():
    """AMB-29 (normative amendment required, stopped sub-case): AAA HALTED at the 02:00 tick (no 1m bar in
    [01:00, 02:00)) and without any 15m close there: no frozen rule supplies the mark; the current code (the
    previous price event carried, ``mark_source`` = ``stale``) is kept, clearly marked."""
    tick = LO + 2 * H
    aaa = _drop15(instrument("AAAUSDT", 100.0, -1e-4, dead=((tick - 90 * MIN, tick + 30 * MIN),)),
                  tick - 15 * MIN)
    r = run("A0", base_inputs(aaa=aaa), (LO, END), 24)
    assert r.valid
    src = r.positions.set_index(["ts", "symbol"])["mark_source"]
    assert src[(tick, "AAAUSDT")] == "stale"


# --------------------------------------------------------------------------------------------- DK-42


class _A1Model:
    """A1's classifier (ML* = A2: ``p_logit_en_cal``): P-hat 0.9 before day 2, 0.1 from day 2 on."""

    ml_star = "A2"

    def predict(self, t, cands):
        return cands.assign(p_logit_en_cal=0.9 if t < LO + D else 0.1)


def test_a1_veto_never_exits_a_held_leg():
    """S-7.2 A1 = A0's entries minus the vetoed: a held A1 leg whose P-hat drops below 0.50 while s_rule > 0
    is kept after h* (no hurdle exit); A1's book equals A0's."""
    a1 = run("A1", base_inputs(), (LO, END), 24, model=_A1Model())
    a0 = run("A0", base_inputs(), (LO, END), 24)
    assert a1.valid and a0.valid
    assert not (a1.fills["reason"] == "hurdle").any()
    cols = ["ts", "symbol", "direction", "qty", "reason"]
    pd.testing.assert_frame_equal(a1.fills[cols], a0.fills[cols])


# --------------------------------------------------------------------------------------------- DK-77


def test_maker_remainder_in_halted_wait_is_not_reexited():
    """E_M: AAA's gate exit posts a maker limit at t2 + 15 min (P exists: zero-volume bars) that never fills;
    its remainder is due at t2 + 45 min while AAA is HALTED -> the S-5.4 wait. From then on no maker order
    rests: the 20 % drop seen at the 01:15 tick does NOT replace the wait by a leg stop (no cooldown); the
    exit fills at the first volume > 0 bar with its own reason ``gate``."""
    resume = T2 + 120 * MIN
    drop = T2 + 50 * MIN
    aaa = instrument("AAAUSDT", lambda ts: np.where(ts < drop, 100.0, 80.0), -1e-4, trades=NONE)
    b1 = aaa.bars1m.copy()
    b1.loc[(b1["open_ts"] >= T2 - 15 * MIN) & (b1["open_ts"] < resume), "vol_base"] = 0.0
    aaa = replace(aaa, bars1m=b1)
    inp = base_inputs(aaa=aaa, bbb=instrument("BBBUSDT", 50.0, 1e-4, trades=NONE),
                      gates=_aaa_gate_fails_at_t2)
    r = run("A0", inp, (LO, END), 24, execution="E_M")
    assert r.valid
    ex = _of(r, "AAAUSDT")
    ex = ex[ex["direction"] == -1]
    assert ex[["ts", "reason", "liquidity"]].values.tolist() == [[resume + MIN, "gate", "taker"]]
    assert "halted_exit" in ex["flags"].iloc[0]
    assert not (r.fills["reason"] == "leg_stop").any()


# --------------------------------------------------------------------------------------------- DK-78


def _hedge_needed_from_8h(inp):
    """beta AAA = BBB = 1 (no hedge) until 08:00; from the 08:05 decision beta AAA = 2 -> a hedge open is
    ordered at 08:05 for 08:20."""
    base = inp.view

    def view(t):
        v = base(t)
        return replace(v, betas={**v.betas, "AAAUSDT": 2.0}) if t >= LO + 8 * H else v

    return replace(inp, view=view)


def test_one_sided_flatten_cancels_inflight_hedge_open():
    """BBB (the only short) stops out at the 08:15 tick, between the 08:05 decision and its 08:20 hedge
    open: the one-sided flatten closes AAA and cancels the hedge open (never opened after the flatten)."""
    rise = LO + 7 * H + 50 * MIN
    bbb = instrument("BBBUSDT", lambda ts: np.where(ts < rise, 50.0, 60.0), 1e-4)
    inp = _hedge_needed_from_8h(base_inputs(bbb=bbb, betas={"AAAUSDT": 1.0, "BBBUSDT": 1.0}))
    r = run("A0", inp, (LO, END), 24)
    assert r.valid
    target = r.decisions.set_index("t").loc[LO + 8 * H + 5 * MIN, "hedge_target"]
    assert target == pytest.approx(-0.1, rel=1e-3)  # a hedge open was ordered at 08:05 for 08:20
    assert _of(r, "AAAUSDT", "one_sided_flatten")["ts"].tolist() == [LO + 8 * H + 16 * MIN]
    assert (_of(r, BTC)["ts"] > LO + 16 * H).all()  # no hedge before the next decision's (if any)
    assert any(x.startswith(f"hedge_open_cancelled_by_one_sided_flatten:{BTC}") for x in r.integrity)


def test_kill_cancels_inflight_hedge_open():
    """BBB triples before the 08:15 tick: the kill exits every leg and cancels the hedge open ordered at 08:05
    for 08:20 (exit everything, no re-entry)."""
    rise = LO + 7 * H + 50 * MIN
    bbb = instrument("BBBUSDT", lambda ts: np.where(ts < rise, 50.0, 150.0), 1e-4)
    inp = _hedge_needed_from_8h(base_inputs(bbb=bbb, betas={"AAAUSDT": 1.0, "BBBUSDT": 1.0}))
    r = run("A0", inp, (LO, END), 24)
    assert r.valid
    assert set(r.fills.loc[r.fills["reason"] == "kill", "symbol"]) == {"AAAUSDT", "BBBUSDT"}
    assert _of(r, BTC).empty
    assert any(x.startswith(f"hedge_open_cancelled_by_kill:{BTC}") for x in r.integrity)


# ---------------------------------------------- controller ruling R42 (fix round 1): required reduces

T8 = LO + 8 * H + 5 * MIN  # the 08:05 decision
TE8 = T8 + 15 * MIN
_L, _S = ["CCCUSDT", "DDDUSDT", "EEEUSDT", "FFFUSDT"], ["GGGUSDT", "HHHUSDT", "IIIUSDT", "JJJUSDT"]


def _ten_legs(ccc_dead=()):
    """5 + 5 legs at 0.1 E each from 00:20: the entry costs leave the marked gross above 1.0 E, so the 08:05
    decision reduces every kept leg proportionally (``risk_gross``, a taker risk order due at 08:20)."""
    extra = {s: instrument(s, 20.0, 0.0, dead=ccc_dead if s == "CCCUSDT" else ()) for s in _L + _S}
    return base_inputs(extra=extra, fl30={**{s: -0.003 for s in _L}, **{s: 0.003 for s in _S}})


def test_risk_gross_reduce_without_bar_is_invalid():
    """R42: a required ``risk_gross`` reduce of a NON-HALTED leg with no 1m bar in [08:20, 09:20] -> INVALID
    RUN (never skipped)."""
    ok = run("A0", _ten_legs(), (LO, END), 24)
    assert ok.valid and "reduce:risk_gross:CCCUSDT" in ok.decisions.set_index("t").loc[T8, "orders"]
    r = run("A0", _ten_legs(ccc_dead=((TE8, TE8 + 61 * MIN),)), (LO, END), 24)
    assert r.invalid == INVALID_EXIT_GAP and r.invalid_ts == TE8
    assert r.invalid_detail.startswith(f"CCCUSDT#0@{TE8}: risk_gross exit due at {TE8}")


def test_halted_reduce_is_still_skipped():
    """S-5.4 "no resizes while halted": CCC traded before 07:15 (not halted at the 08:05 decision) but is
    HALTED at 08:20 -> its ``risk_gross`` reduce is skipped (``unfilled_reduce``), the run stays valid."""
    r = run("A0", _ten_legs(ccc_dead=((LO + 7 * H + 15 * MIN, TE8 + 20 * MIN),)), (LO, END), 24)
    assert r.valid
    assert "unfilled_reduce:CCCUSDT" in r.decisions.set_index("t").loc[T8, "notes"]
    at_te = lambda sym: _of(r, sym, "risk_gross")["ts"].eq(TE8).sum()  # noqa: E731
    assert at_te("CCCUSDT") == 0 and at_te("DDDUSDT") == 1  # CCC not resized while halted, DDD resized


def test_em_drift_reduce_without_bar_is_invalid():
    """R42 under E_M: AAA rose 30 % (marked notional 0.13 > 1.25 x 0.1): its drift reduce is a maker-first
    order at 08:20 (``maker_at``); no 1m bar within 60 min (not halted) -> INVALID RUN."""
    up = LO + 7 * H
    path = lambda ts: np.where(ts < up, 100.0, 130.0)  # noqa: E731
    bbb = instrument("BBBUSDT", 50.0, 1e-4, trades=NONE)
    ok = run("A0", base_inputs(aaa=instrument("AAAUSDT", path, -1e-4, trades=NONE), bbb=bbb), (LO, END), 24,
             execution="E_M")
    assert ok.valid and "reduce:drift:AAAUSDT" in ok.decisions.set_index("t").loc[T8, "orders"]
    aaa = instrument("AAAUSDT", path, -1e-4, trades=NONE, dead=((TE8, TE8 + 61 * MIN),))
    r = run("A0", base_inputs(aaa=aaa, bbb=bbb), (LO, END), 24, execution="E_M")
    assert r.invalid == INVALID_EXIT_GAP and r.invalid_ts == TE8
    assert r.invalid_detail.startswith(f"AAAUSDT#0@{TE8}: drift exit due at {TE8}")


def test_em_remainder_without_bar_invalidates_at_its_due_time_unless_replaced():
    """Fix round 1 (item 4): the E_M remainder of AAA's gate exit (posted t2 + 15 min) is due at t2 + 45 min,
    where AAA has no bar for 61 min (not halted). The INVALID is an event at that due time, not at the
    decision: fills in between are booked and ``invalid_ts`` is the due time. If a forced exit replaces the
    resting order first (leg stop at the t2 + 25 min tick), the remainder never exists and the run stays
    valid."""
    rem = T2 + 45 * MIN
    dead = ((rem, rem + 61 * MIN),)
    flat = instrument("AAAUSDT", 100.0, -1e-4, trades=NONE, dead=dead)
    inp = base_inputs(aaa=flat, bbb=instrument("BBBUSDT", 50.0, 1e-4, trades=NONE),
                      gates=_aaa_gate_fails_at_t2)
    r = run("A0", inp, (LO, END), 24, execution="E_M")
    assert r.invalid == INVALID_EXIT_GAP and r.invalid_ts == rem
    assert r.invalid_detail.startswith(f"AAAUSDT#0@{rem}: gate exit due at {rem}")
    assert _of(r, BTC, "hedge")["ts"].max() == T2 + 15 * MIN  # the t2 hedge close (t2 + 15 min) was booked
    # replaced before the remainder: AAA drops 20 % -> leg stop at t2 + 25 min, forced taker at t2 + 26 min
    drop = instrument("AAAUSDT", lambda ts: np.where(ts < T2 + 10 * MIN, 100.0, 80.0), -1e-4, trades=NONE,
                      dead=dead)
    ok = run("A0", replace(inp, instruments={**inp.instruments, "AAAUSDT": drop}), (LO, END), 24,
             execution="E_M")
    assert ok.valid
    ex = _of(ok, "AAAUSDT")
    assert ex.loc[ex["direction"] == -1, ["ts", "reason"]].values.tolist() == [[T2 + 26 * MIN, "leg_stop"]]


def test_ceased_not_halted_leg_without_mark_is_invalid():
    """Fix round 1 (item 3): AAA's instance ceases at 12:00 with a class-A settlement at 12:30 (official time
    after the last trade). At the 12:15 tick no bar of the instance can end there and AAA is not yet halted
    (it traded in [11:15, 12:00)): DEC-43 supplies no mark, so override 3 applies literally -> INVALID RUN
    (no stale exemption for a ceased, non-halted leg; only HALTED legs are the stopped AMB-29 (a)
    sub-case)."""
    cess = LO + 12 * H
    aaa = instrument("AAAUSDT", 100.0, -1e-4, last_trade=cess, terminal="A", settle_price=97.0)
    ev = aaa.terminals[0]
    aaa = replace(aaa, terminals={0: replace(ev, settle_ts=cess + 30 * MIN)})
    r = run("A0", base_inputs(aaa=aaa), (LO, END), 24)
    assert r.invalid == INVALID_MARK and r.invalid_ts == cess + 15 * MIN
    assert r.invalid_detail.startswith(f"AAAUSDT#0@{cess + 15 * MIN}: no mark-price")
    assert (r.fills["liquidity"] != "settlement").all()
