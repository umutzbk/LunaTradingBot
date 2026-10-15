"""User clarification round 1 (docs/results/v7-impl/CLARIFICATIONS-2026-10-15-round1.md) at the engine level:
AMB-16 (d) one-sided flatten at the next executable opportunity (S-4.2, S-5.4) and AMB-21 (b) missing BTC
price -> INVALID RUN only when required (S-7.4, S-8.1, S-8.3, S-13.4). Synthetic inputs only."""

from dataclasses import replace

import numpy as np
import pandas as pd
import pytest

from luna.v7 import accounting
from luna.v7.book.engine import run
from luna.v7.book.execution import INVALID_BTC
from luna.v7.timeutil import MIN, D, H

from .engine_fixtures import BTC, END, LO, base_inputs, gate_row, instrument

T1 = LO + 5 * MIN  # first decision
TE1 = T1 + 15 * MIN
RISE = LO + 5 * H + 30 * MIN  # BBB (the only short) rises 20 % -> its leg stop triggers at the 05:45 tick
TRIG = RISE + 15 * MIN


def _bbb_stops():
    return instrument("BBBUSDT", lambda ts: np.where(ts < RISE, 50.0, 60.0), 1e-4)


def _of(r, sym, reason=None):
    f = r.fills[r.fills["symbol"] == sym]
    return f if reason is None else f[f["reason"] == reason]


# ------------------------------------------------------------------------------------ AMB-16 (d) timing


def test_leg_stop_empties_short_side_long_flattened_at_s42_time():
    """The leg stop empties the short side at the 05:45 tick: the long leg AND the BTC hedge (the entire book,
    fix round 1) exit with reason ``one_sided_flatten`` on the S-4.2 forced-exit bar (trigger + 1 min, taker),
    not at the next decision's t + 15 min (08:20); the minimum hold (24 h) does not block it; nothing is
    entered to keep the side."""
    r = run("A0", base_inputs(bbb=_bbb_stops()), (LO, END), 24)
    assert r.valid
    st = _of(r, "BBBUSDT", "leg_stop")
    assert st["ts"].tolist() == [TRIG + MIN]
    fl = _of(r, "AAAUSDT", "one_sided_flatten")
    assert fl[["ts", "liquidity", "q_after"]].values.tolist() == [[TRIG + MIN, "taker", 0.0]]
    assert fl["ts"].iloc[0] < fl["entry_decision_ts"].iloc[0] + 24 * H  # inside the minimum hold
    assert not (r.fills["reason"] == "neutrality").any()  # not left to the 08:05 decision
    # no replacement entry: between the trigger and the next decision fill only the two exits happen; no leg
    # is held afterwards until the next decision
    mid = r.fills[(r.fills["ts"] > TRIG) & (r.fills["ts"] <= LO + 8 * H + 20 * MIN)]
    legs_mid = mid[mid["role"] == "leg"]
    assert sorted(legs_mid["reason"]) == ["leg_stop", "one_sided_flatten"]
    pos = r.positions
    assert pos[(pos["role"] == "leg") & (pos["ts"] > TRIG + MIN) & (pos["ts"] <= LO + 8 * H + 15 * MIN)].empty
    # the S-7.4 hedge is part of the entire book: it exits with the legs, not at the 08:05 decision
    h = _of(r, BTC)
    hm = h[(h["ts"] > TRIG) & (h["ts"] < LO + 16 * H)]
    assert hm[["ts", "reason", "liquidity", "q_after"]].values.tolist() == [
        [TRIG + MIN, "one_sided_flatten", "taker", 0.0]]
    assert "hedge" not in set(r.fills.loc[(r.fills["ts"] > TRIG) & (r.fills["ts"] < LO + 16 * H), "reason"])
    d = r.decisions.set_index("t")
    assert not any(o.startswith("open:") for o in d.loc[LO + 8 * H + 5 * MIN, "orders"])


def test_one_sided_flatten_of_halted_leg_waits_no_synthetic_fill():
    """The remaining long leg is halted at trigger + 1 min: the flatten exit is pending (S-5.4) and fills on
    the first volume > 0 1m bar of the same instance (its close, stressed costs); no fill while halted."""
    dead = (LO + 4 * H, LO + 7 * H)
    aaa = instrument("AAAUSDT", 100.0, -1e-4, dead=(dead,))
    r = run("A0", base_inputs(aaa=aaa, bbb=_bbb_stops()), (LO, END), 24)
    assert r.valid
    fl = _of(r, "AAAUSDT", "one_sided_flatten")
    assert fl["ts"].tolist() == [dead[1] + MIN] and "halted_exit" in fl["flags"].iloc[0]
    assert not ((r.fills["symbol"] == "AAAUSDT") & (r.fills["ts"] > TRIG) & (r.fills["ts"] < dead[1])).any()
    pos = r.positions
    held = pos[(pos["symbol"] == "AAAUSDT") & (pos["ts"] > TRIG) & (pos["ts"] <= dead[1])]
    assert len(held) == (dead[1] - TRIG) // (15 * MIN) and (held["pending_exit"] == "one_sided_flatten").all()


def test_inflight_entry_keeps_its_side_under_e_m():
    """E_M: the long entry fills (maker) at 00:22, the short entry at 00:40 (after the 00:30 tick). A side
    whose entry order is still in flight is not empty: no flatten at 00:30; both legs are held at 00:45."""
    lim_a, lim_b = 100.0 * (1 - 1e-4), 50.0 * (1 + 1e-4)
    ta = pd.DataFrame(dict(ts=np.array([TE1 + 2 * MIN], np.int64), price=[lim_a - 0.01], qty=[100.0]))
    tb = pd.DataFrame(dict(ts=np.array([TE1 + 20 * MIN], np.int64), price=[lim_b + 0.01], qty=[100.0]))
    inp = base_inputs(aaa=instrument("AAAUSDT", 100.0, -1e-4, trades=ta),
                      bbb=instrument("BBBUSDT", 50.0, 1e-4, trades=tb))
    r = run("A0", inp, (LO, END), 24, execution="E_M")
    assert r.valid
    mk = r.fills[r.fills["liquidity"] == "maker"]
    assert mk[["symbol", "ts"]].values.tolist() == [["AAAUSDT", TE1 + 2 * MIN], ["BBBUSDT", TE1 + 20 * MIN]]
    assert not (r.fills["reason"] == "one_sided_flatten").any()
    at = r.positions[(r.positions["ts"] == LO + 45 * MIN) & (r.positions["role"] == "leg")]
    assert sorted(at["symbol"]) == ["AAAUSDT", "BBBUSDT"]


NONE = pd.DataFrame({"ts": pd.Series(dtype="int64"), "price": [], "qty": []})  # E_M: no trade-through ever


def test_empty_side_neutrality_close_is_forced_under_e_m():
    """Fix round 1: at t2 AAA (the only long) fails a gate -> its decision exit rests as an E_M maker order;
    BBB's empty-side neutrality close is a forced exit triggered at t2: taker on the first 1m bar at
    t2 + 1 min (S-4.2), not a maker order at t2 + 15 min."""
    t2 = LO + D + 5 * MIN

    def gates(t, s):
        return gate_row(t, u2=False) if (s == "AAAUSDT" and t >= t2) else gate_row(t)

    inp = base_inputs(aaa=instrument("AAAUSDT", 100.0, -1e-4, trades=NONE),
                      bbb=instrument("BBBUSDT", 50.0, 1e-4, trades=NONE), gates=gates)
    r = run("A0", inp, (LO, END), 24, execution="E_M")
    assert r.valid
    b = _of(r, "BBBUSDT", "neutrality")
    assert b[["ts", "liquidity", "q_after"]].values.tolist() == [[t2 + MIN, "taker", 0.0]]
    assert not (r.fills["liquidity"] == "maker").any()


def test_one_sided_flatten_replaces_resting_maker_exits_under_e_m():
    """E_M: at t2 CCC (long) fails a gate -> its exit rests as a maker order (t2 + 15 .. t2 + 45 min). BBB
    (the only short) stops out at the t2 + 25 min tick: the one-sided flatten replaces CCC's resting maker
    exit by the forced taker exit and flattens AAA, both on the first 1m bar at t2 + 26 min (S-4.2); the hedge
    was already closed by the t2 decision (beta_book 0)."""
    t2 = LO + D + 5 * MIN
    rise = t2 + 10 * MIN

    def gates(t, s):
        return gate_row(t, u2=False) if (s == "CCCUSDT" and t >= t2) else gate_row(t)

    ccc = instrument("CCCUSDT", 20.0, -1e-4, trades=NONE)
    bbb = instrument("BBBUSDT", lambda ts: np.where(ts < rise, 50.0, 60.0), 1e-4, trades=NONE)
    inp = base_inputs(aaa=instrument("AAAUSDT", 100.0, -1e-4, trades=NONE), bbb=bbb, extra={"CCCUSDT": ccc},
                      fl30={"CCCUSDT": -0.004}, gates=gates)
    r = run("A0", inp, (LO, END), 24, execution="E_M")
    assert r.valid
    trig = t2 + 25 * MIN
    assert _of(r, "BBBUSDT", "leg_stop")["ts"].tolist() == [trig + MIN]
    after = r.fills[(r.fills["ts"] > t2) & (r.fills["ts"] < LO + D + 8 * H)]
    got = sorted(map(tuple, after[["symbol", "reason", "ts", "liquidity"]].values.tolist()))
    assert got == [("AAAUSDT", "one_sided_flatten", trig + MIN, "taker"),
                   ("BBBUSDT", "leg_stop", trig + MIN, "taker"),
                   (BTC, "hedge", t2 + 15 * MIN, "taker"),  # the t2 decision: beta_book 0 -> hedge closed
                   ("CCCUSDT", "one_sided_flatten", trig + MIN, "taker")]
    assert not (r.fills["liquidity"] == "maker").any()


def test_class_a_settlement_records_btc_ref_and_is_accountable():
    """Regression (round 1 side fix): an A settlement fill carries B(e) = the BTC reference at the settlement
    time, so the valid run is accountable (the remaining short is then flattened one-sided)."""
    cess = LO + 12 * H
    aaa = instrument("AAAUSDT", 100.0, -1e-4, last_trade=cess, terminal="A", settle_price=97.0)
    r = run("A0", base_inputs(aaa=aaa), (LO, END), 24)
    assert r.valid
    s = r.fills[r.fills["liquidity"] == "settlement"]
    assert s[["symbol", "ts", "btc_ref"]].values.tolist() == [["AAAUSDT", cess, 1000.0]]
    fl = r.fills[r.fills["reason"] == "one_sided_flatten"]
    # the 12:00 terminal check precedes the 12:00 one-sided check: S-4.2 from 12:00
    assert set(fl["symbol"]) == {"BBBUSDT", BTC} and set(fl["ts"]) == {cess + MIN}
    accounting.account(r)


# ---------------------------------------------------------------------------------------- AMB-21 (b)


def _btc(dead=(), drop15=()):
    """BTC at 1000 with 1m bars absent in ``dead`` spans and the 15m last / mark bars opening at ``drop15``
    removed (no BTC mark and no last-price bar at the tick they end at)."""
    b = instrument(BTC, 1000.0, 5e-5, dead=dead)
    keep = ~b.bars15["open_ts"].isin(list(drop15))
    keep_m = ~b.mark15["open_ts"].isin(list(drop15))
    return replace(b, bars15=b.bars15[keep].reset_index(drop=True),
                   mark15=b.mark15[keep_m].reset_index(drop=True))


def test_btc_missing_for_hedge_construction_is_invalid():
    """The S-7.4 hedge trade due at 00:20 has no BTC reference (not halted: bars before 00:20 exist; none in
    [00:20, 01:21)) -> INVALID RUN, never an exception or a stale price."""
    inp = replace(base_inputs(), btc=_btc(dead=((TE1, TE1 + 61 * MIN),)))
    r = run("A0", inp, (LO, END), 24)
    assert r.invalid == INVALID_BTC
    assert f"BTC price required for hedge construction missing at {TE1}" in r.invalid_detail
    assert r.invalid_ts == TE1 and r.fills.empty
    with pytest.raises(ValueError, match="refuses an invalid run"):
        accounting.account(r)


def test_btc_halted_at_hedge_time_follows_s54():
    """BTC halted at 00:20 (no 1m bar in [23:20, 00:20)): no hedge order (S-5.4), the run stays valid; the
    legs' B(e) at 00:20 is P(00:20) under the S-5.2 gap rule; the hedge is placed at the next decision."""
    inp = replace(base_inputs(), btc=_btc(dead=((TE1 - 60 * MIN, TE1 + 30 * MIN),)))
    r = run("A0", inp, (LO, END), 24)
    assert r.valid
    assert "hedge_halted" in r.decisions.set_index("t").loc[T1, "notes"]
    assert _of(r, BTC)["ts"].min() == LO + 8 * H + 20 * MIN
    accounting.account(r)


def test_hedge_held_without_btc_mark_is_invalid():
    """A hedge held at the 02:00 tick with neither a BTC mark bar nor a last-price bar ending there ->
    INVALID RUN (no stale hedge mark)."""
    tick = LO + 2 * H
    r = run("A0", replace(base_inputs(), btc=_btc(drop15=(tick - 15 * MIN,))), (LO, END), 24)
    assert r.invalid == INVALID_BTC and r.invalid_ts == tick
    assert f"BTC mark required for hedge marking missing at {tick}" in r.invalid_detail
    assert r.marks["ts"].max() < tick


def test_hedge_exit_without_btc_price_is_invalid():
    """At t2 (after h* = 24 h) AAA fails a gate: decision exit at t2 + 15 min, BBB's empty-side neutrality
    close is forced (t2 + 1 min) and the hedge target is 0 -> a hedge close due at t2 + 15 min; BTC has no
    reference there (not halted) -> INVALID RUN for the required hedge trade."""
    t2 = LO + D + 5 * MIN
    te = t2 + 15 * MIN

    def gates(t, s):
        return gate_row(t, u2=False) if (s == "AAAUSDT" and t >= t2) else gate_row(t)

    inp = replace(base_inputs(gates=gates), btc=_btc(dead=((te, te + 61 * MIN),)))
    r = run("A0", inp, (LO, END), 24)
    assert r.invalid == INVALID_BTC and r.invalid_ts == te
    assert f"BTC price required for hedge construction missing at {te}" in r.invalid_detail


def _no_hedge(**kw):
    return base_inputs(betas={"AAAUSDT": 1.0, "BBBUSDT": 1.0}, **kw)  # beta_book 0: no hedge (S-7.4)


def test_leg_held_without_btc_b_at_tick_is_invalid():
    """Canonical section 8 accounting: a leg held at the 02:00 tick needs B(e) = the BTC mark there."""
    tick = LO + 2 * H
    r = run("A0", replace(_no_hedge(), btc=_btc(drop15=(tick - 15 * MIN,))), (LO, END), 24)
    assert BTC not in set(r.fills["symbol"])
    assert r.invalid == INVALID_BTC and r.invalid_ts == tick
    assert f"BTC reference B(e) required for canonical accounting missing at {tick}" in r.invalid_detail


def test_leg_fill_without_btc_b_is_invalid():
    """A leg fill at 00:20 needs B(00:20) (mkt_c); BTC has no reference there: INVALID RUN, nothing booked."""
    r = run("A0", replace(_no_hedge(), btc=_btc(dead=((TE1, TE1 + 61 * MIN),))), (LO, END), 24)
    assert r.invalid == INVALID_BTC and r.invalid_ts == TE1 and r.fills.empty
    assert f"BTC reference B(e) required for canonical accounting missing at {TE1}" in r.invalid_detail


def test_btc_missing_while_flat_is_still_valid():
    """No leg, no hedge held and no hedge trade due: BTC data missing at the 00:00 / 00:15 ticks and in the
    1m bars before the first fill invalidates nothing; the BTC mark column is None there."""
    inp = replace(base_inputs(), btc=_btc(dead=((LO - 30 * MIN, LO + 10 * MIN),),
                                          drop15=(LO - 15 * MIN, LO)))
    r = run("A0", inp, (LO, END), 24)
    assert r.valid
    m = r.marks.set_index("ts")["btc_mark"]
    assert pd.isna(m[LO]) and pd.isna(m[LO + 15 * MIN]) and m[LO + 30 * MIN] == 1000.0
    accounting.account(r)
