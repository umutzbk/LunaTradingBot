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
from luna.v7.timeutil import MIN, H

from .engine_fixtures import BTC, END, LO, base_inputs, instrument

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
    """The leg stop empties the short side at the 05:45 tick: the long leg exits with reason
    ``one_sided_flatten`` on the S-4.2 forced-exit bar (trigger + 1 min, taker), not at the next decision's
    t + 15 min (08:20); the minimum hold (24 h) does not block it; nothing is entered to keep the side."""
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
    # the hedge is a book-level S-7.4 object: it is closed at the next decision (target 0), taker
    h = _of(r, BTC)
    hm = h[(h["ts"] > TRIG) & (h["ts"] < LO + 16 * H)]
    assert hm[["ts", "reason"]].values.tolist() == [[LO + 8 * H + 20 * MIN, "hedge"]]
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
    """After the one-sided flatten, the 08:05 decision closes the hedge at 08:20; BTC has no reference there
    (not halted) and no leg is held -> INVALID RUN for the required hedge trade."""
    te = LO + 8 * H + 20 * MIN
    inp = replace(base_inputs(bbb=_bbb_stops()), btc=_btc(dead=((te, te + 61 * MIN),)))
    r = run("A0", inp, (LO, END), 24)
    assert r.invalid == INVALID_BTC
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
