"""Event-loop book engine (spec S-1.5, S-4.2, S-5.4, S-6, S-7, S-8.1, S-13.4, K11) on synthetic inputs."""

from dataclasses import replace

import numpy as np
import pandas as pd
import pytest

from luna.v7 import guard, windows
from luna.v7.book import engine
from luna.v7.book.engine import run
from luna.v7.book.execution import INVALID_NO_EXIT, INVALID_TERMINAL
from luna.v7.timeutil import MIN, D, H

from .engine_fixtures import BTC, END, F_T, HMIN, LO, SLIP, base_inputs, gate_row, instrument

T1 = LO + 5 * MIN  # first decision
TE1 = T1 + 15 * MIN
U = END - MIN  # terminal unwind
C = HMIN + SLIP


def plain():
    return run("A0", base_inputs(), (LO, END), 24)


def legs_of(r, sym):
    return r.fills[r.fills["symbol"] == sym].reset_index(drop=True)


def test_plain_run_hand_values():
    r = plain()
    assert r.valid and r.invalid is None
    f = r.fills
    entries = f[f["ts"] == TE1]
    assert list(entries["symbol"]) == ["AAAUSDT", "BBBUSDT", BTC]  # exits, ..., opens; then the hedge
    a, b, h = (entries.iloc[i] for i in range(3))
    assert (a["direction"], a["qty"]) == (1, pytest.approx(0.1 / 100.0))
    assert (b["direction"], b["qty"]) == (-1, pytest.approx(0.1 / 50.0))
    # beta_book = 0.1 * 2 - 0.1 * 1 = 0.1 > 0.05 E -> H_B = -0.1 (S-7.4)
    assert (h["direction"], h["qty"], h["reason"]) == (-1, pytest.approx(0.1 / 1000.0), "hedge")
    assert a["beta"] == 2.0 and b["beta"] == 1.0 and pd.isna(h["beta"])  # beta of the opening decision
    assert a["price"] == pytest.approx(100 * (1 + C)) and b["price"] == pytest.approx(50 * (1 - C))
    # funding: 5 settlements in (00:20 day 1, 23:59 day 2]: 08:00, 16:00, 00:00, 08:00, 16:00
    fl = r.funding_flows
    assert sorted(set(fl["ts"])) == [LO + 8 * H, LO + 16 * H, LO + D, LO + D + 8 * H, LO + D + 16 * H]
    assert np.allclose(fl["cash"], -fl["qty"] * fl["mark"] * fl["rate"], rtol=1e-15, atol=0)
    first = fl[fl["ts"] == LO + 8 * H].set_index("symbol")["cash"]
    assert first["AAAUSDT"] == pytest.approx(0.001 * 100 * 1e-4)  # long receives a negative rate
    assert first["BBBUSDT"] == pytest.approx(0.002 * 50 * 1e-4)  # short receives a positive rate
    assert first[BTC] == pytest.approx(1e-4 * 1000 * 5e-5)
    # S-7.3 hard per-leg cap 0.5 E / K on the marked book (AMB-16 (e)): equity < 1 after the entry costs, so
    # the 08:05 decision cuts each leg to 0.1 E(08:05) (reduce-only); later decisions do not re-increase
    e2 = r.decisions.loc[r.decisions["t"] == LO + 8 * H + 5 * MIN, "equity"].iloc[0]
    red = f[f["reason"] == "cap_leg"].set_index("symbol")
    assert red.loc["AAAUSDT", "qty"] * 100 == pytest.approx(0.1 - 0.1 * e2, rel=1e-9)
    assert red.loc["BBBUSDT", "qty"] * 50 == pytest.approx(0.1 - 0.1 * e2, rel=1e-9)
    assert red["ts"].eq(LO + 8 * H + 20 * MIN).all() and len(f[f["reason"].str.startswith("cap")]) == 2
    # terminal unwind: every leg and the hedge, taker at end - 1 min, nothing at / after the end
    un = f[f["reason"] == "terminal_unwind"]
    assert set(un["symbol"]) == {"AAAUSDT", "BBBUSDT", BTC} and set(un["ts"]) == {U}
    assert f["ts"].max() == U < END
    assert (un["q_after"] == 0).all()
    # flat prices: equity change = funding - fees - spread - slippage exactly (PR = 0)
    assert np.allclose(f["spread_cost"] + f["slip_cost"], f["qty"] * f["ref_mid"] * C, rtol=1e-12, atol=0)
    assert np.allclose(f["fee"], F_T * f["qty"] * f["price"], rtol=1e-12, atol=0)
    final = r.daily["equity_end"].iloc[-1]
    costs = (f["fee"] + f["spread_cost"] + f["slip_cost"]).sum()
    assert final == pytest.approx(1.0 + fl["cash"].sum() - costs, rel=0, abs=1e-15)
    assert final == pytest.approx(1.0 + 5 * 2.5e-5 - 6 * 0.1 * C - 3 * F_T * 0.2, rel=1e-5)


def test_daily_table_and_price_identity():
    r = plain()
    d = r.daily
    assert list(d["day"]) == [LO // D, LO // D + 1]
    assert d["end_ts"].iloc[0] == LO + D and d["end_ts"].iloc[-1] == U
    lhs = d["net"].to_numpy()
    rhs = (d["funding"] - d["fees"] - d["spread_slip"] + d["price"]).to_numpy()
    assert np.allclose(lhs, rhs, rtol=0, atol=1e-15)
    assert d["equity_start"].iloc[1] == d["equity_end"].iloc[0]
    # PR = -sum dq * ref mid (flat at both ends) equals the daily price column (S-8.3)
    f = r.fills
    pr = -(f["direction"] * f["qty"] * f["ref_mid"]).sum()
    assert d["price"].sum() == pytest.approx(pr, abs=1e-15)
    # 15-minute marks every tick in [lo, end - 1 min), BTC mark on every tick
    assert list(r.marks["ts"]) == list(range(LO, U, 15 * MIN))
    assert r.marks["btc_mark"].eq(1000.0).all()
    pos = r.positions[r.positions["ts"] == LO + 30 * MIN].set_index("symbol")
    assert pos.loc["AAAUSDT", "qty"] == pytest.approx(0.001) and pos.loc[BTC, "qty"] == pytest.approx(-1e-4)


def test_terminal_unwind_before_window_end():
    r = plain()
    f = r.fills
    assert (f["ts"] < END).all()
    last = f[f["ts"] == U]
    assert set(last["reason"]) == {"terminal_unwind"} and (last["liquidity"] == "taker").all()
    for _, x in last.iterrows():  # S-5.3 taker cost rules at the unwind minute
        assert x["price"] == pytest.approx(x["ref_mid"] * (1 + x["direction"] * C))
        assert x["fee"] == pytest.approx(F_T * x["qty"] * x["price"])
    assert r.positions["ts"].max() < U
    # no decision whose fill would reach the unwind; the run window ends flat
    assert r.decisions["t"].max() == LO + D + 16 * H + 5 * MIN
    # a shorter window unwinds at its own last minute
    r2 = run("A0", base_inputs(), (LO, LO + D), 24)
    assert set(r2.fills.loc[r2.fills["reason"] == "terminal_unwind", "ts"]) == {LO + D - MIN}
    assert r2.fills["ts"].max() == LO + D - MIN


def test_halted_leg_at_unwind_is_invalid_run():
    dead = ((END - 3 * H, END),)  # AAA stops trading 3 h before the end (same instance continues)
    r = run("A0", base_inputs(aaa=instrument("AAAUSDT", 100.0, -1e-4, dead=dead)), (LO, END), 24)
    assert r.invalid == INVALID_NO_EXIT
    assert "AAAUSDT" in r.invalid_detail
    aaa = legs_of(r, "AAAUSDT")
    assert not (aaa["reason"] == "terminal_unwind").any()  # no synthetic fill
    assert aaa["ts"].max() < END - 3 * H
    # control: trading resumes 30 min before the end -> not halted at the unwind, valid run
    resumed = instrument("AAAUSDT", 100.0, -1e-4, dead=((END - 3 * H, END - 30 * MIN),))
    ok = run("A0", base_inputs(aaa=resumed), (LO, END), 24)
    assert ok.valid


def test_halted_pending_exit_no_synthetic_fill():
    """A decision exit due while halted waits for the first executable bar of the same instance."""
    t2 = LO + D + 5 * MIN  # first decision after h* = 24 h: AAA fails a gate -> decision exit at t2 + 15 min
    halt = (t2 - 50 * MIN, t2 + 75 * MIN)  # halted at t2 + 15 min; resumes at t2 + 75 min
    path = lambda ts: np.where(ts < t2 + 75 * MIN, 100.0, 120.0)  # noqa: E731

    def gates(t, s):
        return gate_row(t, u2=False) if (s == "AAAUSDT" and t >= t2) else gate_row(t)

    inp = base_inputs(aaa=instrument("AAAUSDT", path, -1e-4, dead=(halt,)), gates=gates)
    r = run("A0", inp, (LO, END), 24)
    assert r.valid
    ex = legs_of(r, "AAAUSDT")
    ex = ex[ex["reason"] == "gate"]
    assert len(ex) == 1
    x = ex.iloc[0]
    assert x["ts"] == t2 + 76 * MIN  # the first volume > 0 bar opens at t2 + 75 min, booked at its close
    assert x["ref_mid"] == 120.0 and "halted_exit" in x["flags"]
    assert x["price"] == pytest.approx(120.0 * (1 - 25e-4 - 3 * SLIP))
    # the leg stayed held (and marked) while it waited; funding accrued through the wait
    held = r.positions[(r.positions["symbol"] == "AAAUSDT") & (r.positions["ts"] > t2 + 15 * MIN)]
    assert len(held) and (held["pending_exit"] == "gate").all()


def test_terminal_bc_held_invalidates_run():
    cess = LO + 12 * H
    for k in ("B", "C"):
        aaa = instrument("AAAUSDT", 100.0, -1e-4, last_trade=cess, terminal=k)
        r = run("A0", base_inputs(aaa=aaa), (LO, END), 24)
        assert r.invalid == INVALID_TERMINAL == "INVALID RUN — terminal settlement not reconstructible"
        assert r.invalid_detail.startswith(f"AAAUSDT#0@{cess}")
        assert not (r.fills["ts"] > cess).any()  # the run stops; no fill is invented
    # an A event settles at the official time and price, taker fee, no spread / slippage
    aaa = instrument("AAAUSDT", 100.0, -1e-4, last_trade=cess, terminal="A", settle_price=97.0)
    r = run("A0", base_inputs(aaa=aaa), (LO, END), 24)
    assert r.valid
    s = r.fills[r.fills["liquidity"] == "settlement"].iloc[0]
    got = (s["symbol"], s["ts"], s["price"], s["spread_cost"], s["slip_cost"])
    assert got == ("AAAUSDT", cess, 97.0, 0, 0)
    assert s["fee"] == pytest.approx(F_T * s["qty"] * 97.0, rel=1e-12)
    with pytest.raises(ValueError, match="TerminalEvent"):
        run("A0", base_inputs(aaa=instrument("AAAUSDT", 100.0, -1e-4, last_trade=cess)), (LO, END), 24)


def test_bc_event_not_held_is_harmless():
    cess = LO + 12 * H
    ccc = instrument("CCCUSDT", 20.0, 0.0, last_trade=cess, terminal="B")
    r = run("A0", base_inputs(extra={"CCCUSDT": ccc}, fl30={"CCCUSDT": 0.0}), (LO, END), 24)
    assert r.valid and "CCCUSDT" not in set(r.fills["symbol"])
    # the same contract, held (scored above the hurdle on the long side), invalidates the run
    held = run("A0", base_inputs(extra={"CCCUSDT": ccc}, fl30={"CCCUSDT": -0.004}), (LO, END), 24)
    assert "CCCUSDT" in set(held.fills["symbol"]) and held.invalid == INVALID_TERMINAL


def test_forced_exit_timing():
    # leg stop: AAA falls 20 % from 05:30 -> the 05:45 mark triggers; fill on the first 1m bar >= 05:46
    drop = LO + 5 * H + 30 * MIN
    aaa = instrument("AAAUSDT", lambda ts: np.where(ts < drop, 100.0, 80.0), -1e-4)
    r = run("A0", base_inputs(aaa=aaa), (LO, END), 24)
    st = legs_of(r, "AAAUSDT")
    st = st[st["reason"] == "leg_stop"].iloc[0]
    assert st["ts"] == drop + 15 * MIN + MIN and st["ref_mid"] == 80.0
    # 24 h cooldown from the fill (05:46 day 2): blocked at 00:05 day 2, re-entered at 08:05 day 2
    reopen = legs_of(r, "AAAUSDT")
    reopen = reopen[(reopen["direction"] == 1) & (reopen["ts"] > st["ts"])]
    assert reopen["ts"].min() == LO + D + 8 * H + 20 * MIN
    notes = r.decisions.set_index("t")["notes"]
    assert "cooldown:AAAUSDT" in notes[LO + D + 5 * MIN]
    # ineligibility (U4 failing on two consecutive decisions; AMB-17 (f) / AMB-19 (a)): S-4.2 timing
    t3 = LO + 16 * H + 5 * MIN

    def g4(t, s):
        return gate_row(t, u4=False) if (s == "AAAUSDT" and t >= LO + 8 * H) else gate_row(t)

    r = run("A0", base_inputs(gates=g4), (LO, END), 24)
    x = legs_of(r, "AAAUSDT")
    x = x[x["reason"] == "ineligible"].iloc[0]
    assert x["ts"] == t3 + MIN  # first 1m bar >= trigger (the decision) + 1 min, not t + 15 min
    # U6: exit at the decision's t + 15 min (S-7.3)

    def g6(t, s):
        return gate_row(t, u6=False) if (s == "AAAUSDT" and t >= LO + 8 * H) else gate_row(t)

    r = run("A0", base_inputs(gates=g6), (LO, END), 24)
    x = legs_of(r, "AAAUSDT")
    x = x[x["reason"] == "u6"].iloc[0]
    assert x["ts"] == LO + 8 * H + 20 * MIN


def test_engine_deterministic():
    lim_a, lim_b = 100.0 * (1 - HMIN), 50.0 * (1 + HMIN)
    ts = np.array([TE1 + 2 * MIN, TE1 + 4 * MIN], dtype=np.int64)
    ta = pd.DataFrame(dict(ts=ts, price=[lim_a - 0.01, lim_a - 0.02], qty=[3.0, 4.0]))
    tb = pd.DataFrame(dict(ts=ts, price=[lim_b + 0.01, lim_b], qty=[50.0, 50.0]))

    def inputs():
        return base_inputs(aaa=instrument("AAAUSDT", 100.0, -1e-4, trades=ta),
                           bbb=instrument("BBBUSDT", 50.0, 1e-4, trades=tb))

    for exe in ("E_T", "E_M"):
        r1 = run("A0", inputs(), (LO, END), 24, execution=exe)
        r2 = run("A0", inputs(), (LO, END), 24, execution=exe)
        assert r1.valid and r2.valid
        for name in ("fills", "marks", "positions", "funding_flows", "daily", "decisions"):
            pd.testing.assert_frame_equal(getattr(r1, name), getattr(r2, name))
        assert r1.integrity == r2.integrity
    mk = r1.fills[r1.fills["liquidity"] == "maker"]
    # BBB: the trade AT its limit never fills
    assert sorted(mk["symbol"]) == ["AAAUSDT", "AAAUSDT", "BBBUSDT"]
    assert mk["qty"].sum() * 10_000 == pytest.approx(3.0 + 4.0 + 20.0)
    hedge = r1.fills[r1.fills["symbol"] == BTC]
    assert set(hedge["liquidity"]) == {"taker"}  # hedge trades are always taker (S-6.2)


def test_engine_calls_guard_once_and_refuses_sealed_windows(monkeypatch):
    calls = []
    real = guard.check_outcome_window

    def spy(lo, hi, **kw):
        calls.append((lo, hi))
        return real(lo, hi, **kw)

    monkeypatch.setattr(engine.guard, "check_outcome_window", spy)
    run("A0", base_inputs(), (LO, END), 24)
    assert calls == [(LO, END)]
    lo = windows.W_HOLD[0]
    with pytest.raises(guard.HoldoutBlocked):
        run("A0", base_inputs(), (lo, lo + 2 * D), 24)


def test_engine_argument_checks():
    with pytest.raises(ValueError):
        run("A2", base_inputs(), (LO, END), 24)  # an ML arm needs a model
    with pytest.raises(ValueError):
        run("A0", base_inputs(), (LO, END), 36)
    with pytest.raises(ValueError):
        run("A0", base_inputs(), (LO, END), 24, layer="L-265")
    with pytest.raises(ValueError):
        run("A0", base_inputs(), (LO + MIN, END), 24)
    good = base_inputs()
    leaky = replace(good, view=lambda t: replace(good.view(t), gates={
        s: replace(g, max_known_at=t) for s, g in good.view(t).gates.items()}))
    with pytest.raises(Exception, match="max_known_at"):
        run("A0", leaky, (LO, END), 24)


def test_drawdown_kill_flattens_book_and_cancels_entries():
    """BBB and BTC triple from 08:00: the 08:15 mark kills (<= 85 % of the peak); every leg and the hedge exit
    on the first 1m bar >= 08:16 (reason kill), the DDD entry ordered at 08:05 for 08:20 is cancelled, and
    nothing is entered afterwards (S-7.3, S-4.2; AMB-19 (c))."""
    up = LO + 8 * H
    bbb = instrument("BBBUSDT", lambda ts: np.where(ts < up, 50.0, 150.0), 1e-4)
    btc = instrument(BTC, lambda ts: np.where(ts < up, 1000.0, 3000.0), 5e-5)

    def gates(t, s):
        return gate_row(t, u2=False) if (s == "DDDUSDT" and t < up) else gate_row(t)

    ddd = instrument("DDDUSDT", 10.0, -1e-4)
    inp = base_inputs(bbb=bbb, extra={"DDDUSDT": ddd}, fl30={"DDDUSDT": -0.004}, betas={"DDDUSDT": 3.0},
                      gates=gates)
    inp = replace(inp, btc=btc)
    r = run("A0", inp, (LO, END), 24)
    assert r.valid
    k = r.fills[r.fills["reason"] == "kill"]
    assert list(k["symbol"]) == ["AAAUSDT", "BBBUSDT", BTC] and set(k["ts"]) == {up + 16 * MIN}
    assert (k["q_after"] == 0).all() and r.fills["ts"].max() == up + 16 * MIN
    assert "DDDUSDT" not in set(r.fills["symbol"])
    assert any(x.startswith("entry_cancelled_by_kill:DDDUSDT") for x in r.integrity)
    d = r.decisions.set_index("t")
    # 08:05: AAA halves for neutrality (2 long vs 1 short), DDD enters, hedge grows to -0.15; all cancelled
    assert d.loc[up + 5 * MIN, "orders"] == ("reduce:drift:AAAUSDT", "reduce:cap_leg:BBBUSDT",
                                             "open:entry:DDDUSDT")
    assert d.loc[up + 5 * MIN, "hedge_target"] == pytest.approx(-0.15, rel=1e-3)
    assert not (r.fills["reason"].isin(["drift", "cap_leg", "hedge"]) & (r.fills["ts"] > up)).any()
    later = d[d.index > up + 15 * MIN]
    assert later["killed"].all() and all("entries_blocked:kill" in n for n in later["notes"])


class _FakeModel:
    """Stands in for the fold model: yhat (A2 / EN columns) = -fl30 * 1e4 per side."""

    ml_star = "A2"

    def predict(self, t, cands):
        y = -cands["fl30_r8"].to_numpy() * cands["side"].to_numpy() * 1e4
        return cands.assign(yhat_en=y, yhat_cal_en=y)


def test_ml_arm_through_model():
    r = run("A2", base_inputs(), (LO, END), 24, model=_FakeModel())
    a0 = plain()
    assert r.valid
    pd.testing.assert_frame_equal(r.fills[["ts", "symbol", "direction", "qty"]],
                                  a0.fills[["ts", "symbol", "direction", "qty"]])
    with pytest.raises(ValueError):
        run("A0", base_inputs(), (LO, END), 24, model=_FakeModel())
