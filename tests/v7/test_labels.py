"""Leg labels (spec S-5.3 - S-5.6, S-4.4, S-4.6, S-1.5, DEC-43, E10, L3) on synthetic paths with hand nets.

Fixture: 15m bars with h = l = c (Abdi-Ranaldo products are 0, so every half-spread is the 1 bp floor);
the asset's hourly log returns are exactly 2x BTC's (beta = clip(0.7 * 2 + 0.3) = 1.7); 8 h funding records
with explicit intervals; 1m bars with o = c and volume 1. All dates are in 2024 (inside W_DEV, far from
W_HOLD / M6).
"""

import math
from dataclasses import replace

import numpy as np
import pandas as pd
import pytest

import luna.v7.labels as lb
from luna.v7 import guard, windows
from luna.v7.data.instances import Instance
from luna.v7.data.terminal import TerminalEvent
from luna.v7.labels import LabelInputs, LegData, invalid_rate, leg_label
from luna.v7.timeutil import MIN, D, H, to_ns

Q = 15 * MIN
S0 = to_ns("2024-01-30")
T = to_ns("2024-03-10T08:05:00Z")
TE = T + Q
TX = TE + 24 * H  # 2024-03-11 08:20
WIN = (to_ns("2021-01-01"), to_ns("2025-01-01"))
NOW = to_ns("2026-10-14")
F, SLIP, HMIN = 5e-4, 2e-4, 1e-4
BETA = 1.7


def b15(px0, k, start=S0, end=T + 4 * D):
    ts = np.arange(start, end, Q, dtype=np.int64)
    c = px0 * np.exp(k * 0.002 * np.sin(np.arange(len(ts)) / 7.0))
    return pd.DataFrame(dict(open_ts=ts, o=c, h=c, l=c, c=c, vol_base=1.0, known_at=ts + Q))


def b15s(px0, k, a1, a2):
    """15m bars whose Abdi-Ranaldo products are hand-computable. Up to 08:00 of t's day minus 24 h the path
    is as in ``b15`` (flat OHLC, pi = 0, beta 1.7 path); from 08:15 the day before t the close is frozen at
    C and every bar has h = C, l = C * exp(-2 a), so c - eta = a for every bar, pi_k = 4 a_k a_{k+1} and
    the proxy is lam * sqrt(4 a^2) / 2 = 2 a. a = a1 for bars opening <= 08:00 on t's day (exactly the
    pairs of h(t_e)'s window), a = a2 afterwards (every pair of h(t_x)'s and h_fill's windows)."""
    b = b15(px0, k)
    ts = b["open_ts"].to_numpy(np.int64)
    seg = ts >= TE - 24 * H - 5 * MIN
    cfz = float(b.loc[b["open_ts"] == TE - 24 * H - 20 * MIN, "c"].iloc[0])
    a = np.where(ts <= TE - 20 * MIN, a1, a2)
    c = np.where(seg, cfz, b["c"].to_numpy(np.float64))
    lo = np.where(seg, cfz * np.exp(-2 * a), c)
    return b.assign(o=c, h=c, l=lo, c=c)


def m1(px, over=None, start=TE - 3 * H, end=TX + 12 * H, vol=None):
    """1m bars with o = c = px; ``over`` maps ts -> (o, c); ``vol`` maps ts -> volume (default 1)."""
    ts = np.arange(start, end, MIN, dtype=np.int64)
    o = np.full(len(ts), float(px))
    c = o.copy()
    v = np.ones(len(ts))
    for k, (oo, cc) in (over or {}).items():
        i = int((k - start) // MIN)
        o[i], c[i] = oo, cc
    for k, vv in (vol or {}).items():
        v[int((k - start) // MIN)] = vv
    return pd.DataFrame(dict(open_ts=ts, o=o, h=np.maximum(o, c), l=np.minimum(o, c), c=c, vol_base=v,
                             known_at=ts + MIN))


def fund(rate, mark, start=S0, end=T + 4 * D, offset=0):
    ts = np.arange(start + offset, end, 8 * H, dtype=np.int64)
    r = np.array([rate(x) if callable(rate) else rate for x in ts], dtype=float)
    iv = pd.array([8] * len(ts), dtype="Int64")
    return pd.DataFrame(dict(funding_ts=ts, rate=r, interval_h_explicit=iv, known_at=ts + MIN,
                             mark_at_settle=float(mark)))


EMPTY = pd.DataFrame({"open_ts": pd.Series(dtype="int64"), "c": pd.Series(dtype="float64")})
INST = Instance(0, S0, None, None)


def leg(**kw):
    d = dict(bars1m=m1(10.0, {TX: (11.0, 11.0)}), bars15=b15(10.0, 2.0), mark15=EMPTY,
             funding=fund(1e-4, 10.0), insts=[INST], terminal=None)
    d.update(kw)
    return LegData(**d)


def btc(**kw):
    d = dict(bars1m=m1(100.0, {TX: (102.0, 102.0)}), bars15=b15(100.0, 1.0), mark15=EMPTY,
             funding=fund(2e-4, 100.0), insts=[INST], terminal=None)
    d.update(kw)
    return LegData(**d)


M6 = windows.m6()  # read once (git); every label gets the span, so no label runs git


def inputs(lg=None, bt=None, window=WIN):
    return LabelInputs("ABCUSDT", lg or leg(), bt or btc(), window, NOW, M6)


def hand(s, Pe, Px, Be, Bx, fl, fb, ha_e=HMIN, ha_x=HMIN, sl_x=SLIP, hb_x=HMIN, sb_x=SLIP, beta=BETA,
         hb_e=HMIN):
    """S-5.3 written out by hand (fl / fb = sum r_k * M(tau_k) of the leg / BTC)."""
    q, qb = 1 / Pe, beta / Be
    E, X = Pe * (1 + s * (ha_e + SLIP)), Px * (1 - s * (ha_x + sl_x))
    EB, XB = Be * (1 - s * (hb_e + SLIP)), Bx * (1 + s * (hb_x + sb_x))
    c = dict(funding_leg=-s * q * fl, price_resid=s * ((Px / Pe - 1) - beta * (Bx / Be - 1)),
             fee_in=F * q * E, fee_out=F * q * X, ss_in=q * Pe * (ha_e + SLIP), ss_out=q * Px * (ha_x + sl_x),
             hedge_cost=F * qb * EB + F * qb * XB + qb * Be * (hb_e + SLIP) + qb * Bx * (hb_x + sb_x),
             hedge_funding=s * qb * fb)
    return c


def assert_components(r, c):
    assert r.invalid_reason is None
    assert set(r.components) == set(lb.COMPONENTS)
    for k, v in c.items():
        assert r.components[k] == pytest.approx(v, rel=1e-9, abs=1e-15), k


# --- required tests -----------------------------------------------------------------------------------


def test_net_identity():
    r = leg_label(inputs(), T, +1, 24)
    assert r.beta == pytest.approx(BETA, rel=1e-9)
    # long, written out by hand: q = 0.1, q_B = 0.017, 3 settlements (16:00, 00:00, 08:00)
    exp = dict(funding_leg=-3e-4, price_resid=0.066, fee_in=5.0015e-4, fee_out=5.49835e-4, ss_in=3e-4,
               ss_out=3.3e-4, hedge_cost=8.49745e-4 + 8.672601e-4 + 5.1e-4 + 5.202e-4, hedge_funding=1.02e-3)
    assert_components(r, exp)
    c = r.components
    signed = (c["funding_leg"] + c["price_resid"] - c["fee_in"] - c["fee_out"] - c["ss_in"] - c["ss_out"]
              - c["hedge_cost"] + c["hedge_funding"])
    assert r.net == signed
    assert r.net == pytest.approx(0.0622928099, rel=1e-9)
    assert r.y_reg_bps == pytest.approx(r.net * 1e4) and r.y_pos == 1
    assert r.t_x_eff == TX and r.label_known_at == TX + MIN and r.flags == ()
    rs = leg_label(inputs(), T, -1, 24)
    assert_components(rs, hand(-1, 10.0, 11.0, 100.0, 102.0, 3e-3, 3 * 2e-2))
    assert rs.y_pos == 0 and rs.net < 0


def test_funding_window_half_open():
    # records at hh:20: one exactly at t_e (excluded, huge), one exactly at t_x (included)
    def rate(x):
        return 0.5 if x == TE else (3e-4 if x == TX else 1e-4)

    r = leg_label(inputs(leg(funding=fund(rate, 10.0, offset=20 * MIN))), T, +1, 24)
    assert r.components["funding_leg"] == pytest.approx(-0.1 * (1e-3 + 1e-3 + 3e-3), rel=1e-12)
    # a record known after label_known_at is not usable -> missing settlement
    f = fund(1e-4, 10.0)
    f.loc[f["funding_ts"] == TX - 20 * MIN, "known_at"] = TX + 2 * MIN
    assert leg_label(inputs(leg(funding=f)), T, +1, 24).invalid_reason == "funding_missing"
    f.loc[f["funding_ts"] == TX - 20 * MIN, "known_at"] = TX + MIN  # == label_known_at: usable
    assert leg_label(inputs(leg(funding=f)), T, +1, 24).invalid_reason is None


def test_hedge_funding_sign():
    long_ = leg_label(inputs(), T, +1, 24)
    short = leg_label(inputs(), T, -1, 24)
    # BTC rate > 0: the long leg's short hedge receives, the short leg's long hedge pays
    assert long_.components["hedge_funding"] == pytest.approx(1.7 / 100 * 3 * 2e-4 * 100, rel=1e-9)
    assert short.components["hedge_funding"] == pytest.approx(-1.7 / 100 * 3 * 2e-4 * 100, rel=1e-9)
    neg = btc(funding=fund(-2e-4, 100.0))
    assert leg_label(inputs(bt=neg), T, +1, 24).components["hedge_funding"] < 0
    assert leg_label(inputs(bt=neg), T, -1, 24).components["hedge_funding"] > 0


def _halted_leg(fill_open):
    """Leg without volume from t_x - 60 min until the bar opening at ``fill_open`` (o 12, c 12.5)."""
    vol = {x: 0.0 for x in range(TX - 60 * MIN, fill_open, MIN)}
    return leg(bars1m=m1(10.0, {TX: (11.0, 11.0), fill_open: (12.0, 12.5)}, vol=vol))


def test_halted_exit_waits_and_stresses():
    fill = TX + 7 * H + 39 * MIN  # bar 15:59 -> fills 16:00 at its close, at its close time
    bt = btc(bars1m=m1(100.0, {TX: (102.0, 102.0), fill + MIN: (104.0, 104.0)}))
    r = leg_label(inputs(_halted_leg(fill), bt), T, +1, 24)
    assert r.t_x_eff == fill + MIN and r.label_known_at == fill + 2 * MIN
    assert "halted_exit" in r.flags
    # 4 settlements in (t_e, 16:00]: the 16:00 one at t_x_eff is included (known at 16:01 = label_known_at)
    exp = hand(+1, 10.0, 12.5, 100.0, 104.0, 4e-3, 4 * 2e-2, ha_x=25e-4, sl_x=3 * SLIP)
    assert_components(r, exp)
    assert r.components["ss_out"] == pytest.approx(0.1 * 12.5 * 31e-4)
    # one minute earlier: t_x_eff = 15:59, the 16:00 settlement is outside (t_e, t_x_eff]
    bt2 = btc(bars1m=m1(100.0, {TX: (102.0, 102.0), fill: (104.0, 104.0)}))
    r2 = leg_label(inputs(_halted_leg(fill - MIN), bt2), T, +1, 24)
    assert r2.t_x_eff == fill
    assert_components(r2, hand(+1, 10.0, 12.5, 100.0, 104.0, 3e-3, 3 * 2e-2, ha_x=25e-4, sl_x=3 * SLIP))
    # stressed half-spread without a fill-time proxy: 25 bps, reported
    lg = replace(_halted_leg(fill), bars15=b15(10.0, 2.0).query("open_ts < @TE - 5 * @MIN"))
    r4 = leg_label(inputs(lg, bt), T, +1, 24)
    assert {"halted_exit", "exit_proxy_fallback"} <= set(r4.flags)
    assert r4.components == r.components
    # the waiting exit takes no zero-volume bar and no bar before t_x (no retroactive fill)
    assert r.components["price_resid"] != leg_label(inputs(), T, +1, 24).components["price_resid"]


def _ending_leg(last_trade, ev_kind, settle_ts=None, price=None, extra_bars=None):
    inst = Instance(0, S0, last_trade, last_trade + 7 * D)
    insts = [inst] + ([Instance(1, extra_bars, None, None)] if extra_bars is not None else [])
    ev = None if ev_kind is None else TerminalEvent(inst, last_trade, ev_kind, settle_ts, price,
                                                    "official_historical" if ev_kind == "A" else None)
    vol = {x: 0.0 for x in range(last_trade, TX + 12 * H, MIN)} if extra_bars is None else \
        {x: 0.0 for x in range(last_trade, extra_bars, MIN)}
    return leg(bars1m=m1(10.0, {TX: (11.0, 11.0)}, vol=vol), insts=insts, terminal=ev)


def test_terminal_bc_invalid():
    last = TX - 2 * H
    for k in ("B", "C"):
        r = leg_label(inputs(_ending_leg(last, k, last if k == "B" else None)), T, +1, 24)
        assert r.invalid_reason == "terminal_not_reconstructible" and math.isnan(r.net) and r.y_pos is None
    with pytest.raises(ValueError, match="TerminalEvent"):
        leg_label(inputs(_ending_leg(last, None)), T, +1, 24)


def test_terminal_a_settles_without_spread():
    settle = TX - 2 * H - 20 * MIN  # 06:00, cessation 06:15
    bt = btc(bars1m=m1(100.0, {TX: (102.0, 102.0), settle: (101.0, 101.0)}))
    r = leg_label(inputs(_ending_leg(settle + Q, "A", settle, 9.0), bt), T, +1, 24)
    assert r.t_x_eff == settle and r.label_known_at == settle + MIN and "terminal_A" in r.flags
    exp = hand(+1, 10.0, 9.0, 100.0, 101.0, 2e-3, 2 * 2e-2, ha_x=0.0, sl_x=0.0)
    assert_components(r, exp)
    assert r.components["ss_out"] == 0.0
    assert r.components["fee_out"] == pytest.approx(F * 0.1 * 9.0)
    # settlement at/before entry: no exit before entry exists
    r2 = leg_label(inputs(_ending_leg(TE + 10 * MIN, "A", TE, 9.0)), T, +1, 24)
    assert r2.invalid_reason == "terminal_at_or_before_entry"


def test_no_cross_instance_exit_fill():
    # instance 0 is halted at t_x and ceases at t_x + 2 h without an executable 1m bar; instance 1 trades
    # from t_x + 3 h. Its bars must never fill instance 0's exit (E10).
    last, new = TX + 2 * H, TX + 3 * H
    vol = {x: 0.0 for x in range(TX - 60 * MIN, new, MIN)}
    inst0 = Instance(0, S0, last, last + 7 * D)
    for ev, want in ((TerminalEvent(inst0, last, "C", None, None, None), "terminal_not_reconstructible"),
                     (None, ValueError)):
        lg = leg(bars1m=m1(10.0, {TX: (11.0, 11.0), new: (50.0, 50.0)}, vol=vol),
                 insts=[inst0, Instance(1, new, None, None)], terminal=ev)
        if want is ValueError:
            with pytest.raises(ValueError):
                leg_label(inputs(lg), T, +1, 24)
        else:
            assert leg_label(inputs(lg), T, +1, 24).invalid_reason == want
    # class A: settles at the official time / price of instance 0, not at instance 1's bar
    settle = last - 10 * MIN
    ev = TerminalEvent(inst0, last, "A", settle, 9.0, "official_historical")
    lg = leg(bars1m=m1(10.0, {TX: (11.0, 11.0), new: (50.0, 50.0)}, vol=vol),
             insts=[inst0, Instance(1, new, None, None)], terminal=ev)
    r = leg_label(inputs(lg), T, +1, 24)
    assert r.t_x_eff == settle and r.components["price_resid"] == pytest.approx(
        (9.0 / 10.0 - 1) - BETA * 0.0, rel=1e-9)
    # without instance 1's bars the result is identical
    lg2 = replace(lg, bars1m=lg.bars1m[lg.bars1m["open_ts"] < new])
    assert leg_label(inputs(lg2), T, +1, 24) == r


def test_entry_halted_invalid_incl_btc():
    vol = {x: 0.0 for x in range(TE - 60 * MIN, TE, MIN)}
    assert leg_label(inputs(leg(bars1m=m1(10.0, {TX: (11.0, 11.0)}, vol=vol))), T, +1, 24).invalid_reason \
        == "entry_halted"
    assert leg_label(inputs(bt=btc(bars1m=m1(100.0, {TX: (102.0, 102.0)}, vol=vol))), T, -1, 24) \
        .invalid_reason == "btc_entry_halted"
    # one volume bar opening exactly at t_e - 60 min ends the halt
    vol2 = {x: 0.0 for x in range(TE - 59 * MIN, TE, MIN)}
    assert leg_label(inputs(leg(bars1m=m1(10.0, {TX: (11.0, 11.0)}, vol=vol2))), T, +1, 24).invalid_reason \
        is None


def test_exit_proxy_fallback_flagged():
    b = b15(10.0, 2.0)
    lg = leg(bars15=b[b["open_ts"] < TE - 5 * MIN])  # no 15m pair in t_x's 24 h window; entry window intact
    r = leg_label(inputs(lg), T, +1, 24)
    assert "exit_proxy_fallback" in r.flags and "halted_exit" not in r.flags
    assert_components(r, hand(+1, 10.0, 11.0, 100.0, 102.0, 3e-3, 3 * 2e-2, ha_x=25e-4, sl_x=3 * SLIP))
    # the entry proxy missing makes the label invalid
    lg2 = leg(bars15=b[b["open_ts"] < T - 25 * H])
    assert leg_label(inputs(lg2), T, +1, 24).invalid_reason == "entry_proxy_missing"
    bb = b15(100.0, 1.0)
    assert leg_label(inputs(bt=btc(bars15=bb[bb["open_ts"] < T - 25 * H])), T, +1, 24).invalid_reason \
        == "entry_proxy_missing"
    r3 = leg_label(inputs(bt=btc(bars15=bb[bb["open_ts"] < TE - 5 * MIN])), T, +1, 24)
    assert "btc_exit_proxy_fallback" in r3.flags
    assert_components(r3, hand(+1, 10.0, 11.0, 100.0, 102.0, 3e-3, 3 * 2e-2, hb_x=25e-4, sb_x=3 * SLIP))


def test_label_crossing_window_end_absent():
    assert leg_label(inputs(window=(WIN[0], TX)), T, +1, 24) is None
    assert leg_label(inputs(window=(WIN[0], TX - 5 * H)), T, +1, 24) is None
    base = leg_label(inputs(), T, +1, 24)
    assert leg_label(inputs(window=(WIN[0], TX + MIN)), T, +1, 24) == base
    # data at/after the window end never changes a label (S-1.5, L3): spike everything from t_x + 1 min
    lg, bt = leg(), btc()

    def spike(df, col):
        df = df.copy()
        m = df[col] >= TX + MIN
        for c in ("o", "h", "l", "c", "rate", "mark_at_settle"):
            if c in df.columns:
                df.loc[m, c] = df.loc[m, c] * 50
        return df

    lg = replace(lg, bars1m=spike(lg.bars1m, "open_ts"), bars15=spike(lg.bars15, "open_ts"),
                 funding=spike(lg.funding, "funding_ts"))
    bt = replace(bt, bars1m=spike(bt.bars1m, "open_ts"), bars15=spike(bt.bars15, "open_ts"),
                 funding=spike(bt.funding, "funding_ts"))
    assert leg_label(inputs(lg, bt), T, +1, 24) == base
    # a waiting halted exit filling at the window end is invalid; one minute more and it exists
    fill = TX + 7 * H + 39 * MIN
    bth = btc(bars1m=m1(100.0, {TX: (102.0, 102.0), fill + MIN: (104.0, 104.0)}))
    r = leg_label(inputs(_halted_leg(fill), bth, window=(WIN[0], fill + MIN)), T, +1, 24)
    assert r.invalid_reason == "no_executable_exit"
    assert leg_label(inputs(_halted_leg(fill), bth, window=(WIN[0], fill + 2 * MIN)), T, +1, 24) \
        .invalid_reason is None


def test_label_refuses_m6_and_hold_without_auth():
    th = windows.W_HOLD[0] + 10 * D + 5 * MIN
    with pytest.raises(guard.HoldoutBlocked):
        leg_label(inputs(window=windows.W_HOLD), th, +1, 24)
    m6 = windows.m6()[0]
    with pytest.raises(guard.EmbargoBlocked):
        leg_label(inputs(window=(m6, m6 + 180 * D)), m6 + D + 5 * MIN, +1, 24)
    # a label crossing the window end still calls the guard (no data read)
    with pytest.raises(guard.HoldoutBlocked):
        leg_label(inputs(window=windows.W_HOLD), windows.W_HOLD[1] - H, +1, 24)


# --- further cases ------------------------------------------------------------------------------------


def test_guard_called_on_extended_reads(monkeypatch):
    calls = []
    real = guard.check_outcome_window

    def spy(lo, hi, **kw):
        calls.append((lo, hi))
        return real(lo, hi, **kw)

    monkeypatch.setattr(lb.guard, "check_outcome_window", spy)
    leg_label(inputs(), T, +1, 24)
    assert calls == [(T, TX + MIN)]
    calls.clear()
    fill = TX + 7 * H + 39 * MIN
    bt = btc(bars1m=m1(100.0, {TX: (102.0, 102.0), fill + MIN: (104.0, 104.0)}))
    leg_label(inputs(_halted_leg(fill), bt), T, +1, 24)
    assert calls == [(T, TX + MIN), (T, WIN[1])]
    calls.clear()
    bars = m1(10.0, {TX + MIN: (11.0, 11.0)})
    leg_label(inputs(leg(bars1m=bars[bars["open_ts"] != TX])), T, +1, 24)  # exit gap search
    assert calls == [(T, TX + MIN), (T, TX + H + MIN)]


def test_funding_missing_invalid_head_and_marks():
    f = fund(1e-4, 10.0)
    assert leg_label(inputs(leg(funding=f[f["funding_ts"] != TX - 8 * H - 20 * MIN])), T, +1, 24) \
        .invalid_reason == "funding_missing"  # 00:00 missing (gap in the middle)
    assert leg_label(inputs(leg(funding=f[f["funding_ts"] != TX - 20 * MIN])), T, +1, 24) \
        .invalid_reason == "funding_missing"  # 08:00 missing (tail)
    assert leg_label(inputs(leg(funding=f[f["funding_ts"] > TE])), T, +1, 24).invalid_reason \
        == "funding_missing"  # no head record
    fb = fund(2e-4, 100.0)
    assert leg_label(inputs(bt=btc(funding=fb[fb["funding_ts"] != TX - 20 * MIN])), T, +1, 24) \
        .invalid_reason == "funding_missing"
    # an off-schedule record in the window is invalid (S-2.6)
    extra = pd.DataFrame(dict(funding_ts=[TE + 5 * H + 10 * MIN], rate=[1e-4],
                              interval_h_explicit=pd.array([8], dtype="Int64"),
                              known_at=[TE + 5 * H + 11 * MIN], mark_at_settle=[10.0]))
    f2 = pd.concat([f, extra]).sort_values("funding_ts").reset_index(drop=True)
    assert leg_label(inputs(leg(funding=f2)), T, +1, 24).invalid_reason == "funding_invalid"
    f3 = f.copy()
    f3.loc[f3["funding_ts"] == TX - 20 * MIN, "rate"] = np.nan
    assert leg_label(inputs(leg(funding=f3)), T, +1, 24).invalid_reason == "funding_invalid"
    # mark fallback: record mark -> mark15 bar ending at the settlement -> last-price 15m close
    f4 = f.copy()
    f4["mark_at_settle"] = np.nan
    s1 = TX - 20 * MIN
    mk = pd.DataFrame(dict(open_ts=[s1 - Q - 8 * H, s1 - Q], c=[20.0, 30.0]))
    b = b15(10.0, 2.0)
    close = dict(zip(b["open_ts"] + Q, b["c"], strict=True))
    want = 1e-4 * (close[s1 - 16 * H] + 20.0 + 30.0)
    r = leg_label(inputs(leg(funding=f4, mark15=mk)), T, +1, 24)
    assert r.components["funding_leg"] == pytest.approx(-0.1 * want, rel=1e-12)
    f4.loc[f4["funding_ts"] == s1, "mark_at_settle"] = -1.0  # unusable record mark -> fallback
    assert leg_label(inputs(leg(funding=f4, mark15=mk)), T, +1, 24).components["funding_leg"] == \
        pytest.approx(-0.1 * want, rel=1e-12)
    lg = leg(funding=f4, bars15=b[b["open_ts"] != s1 - Q])
    assert leg_label(inputs(lg), T, +1, 24).invalid_reason == "mark_missing"


def test_reference_price_gap_rules():
    bars = m1(10.0, {TX: (11.0, 11.0), TE + MIN: (10.5, 10.5)})
    r = leg_label(inputs(leg(bars1m=bars[bars["open_ts"] != TE])), T, +1, 24)
    assert "entry_gap_price" in r.flags and r.t_x_eff == TX
    assert r.components["price_resid"] == pytest.approx((11.0 / 10.5 - 1) - BETA * 0.02, rel=1e-9)
    gone = bars[(bars["open_ts"] < TE) | (bars["open_ts"] > TE + H)]
    assert leg_label(inputs(leg(bars1m=gone)), T, +1, 24).invalid_reason == "entry_price_missing"
    # exit: NaN open at t_x is unusable -> first usable bar within 60 min, fill time unchanged
    bx = m1(10.0, {TX: (np.nan, 11.0), TX + 2 * MIN: (11.5, 11.5)})
    bx = bx[bx["open_ts"] != TX + MIN]
    r2 = leg_label(inputs(leg(bars1m=bx)), T, +1, 24)
    assert "exit_gap_price" in r2.flags and r2.t_x_eff == TX
    assert r2.components["price_resid"] == pytest.approx((11.5 / 10.0 - 1) - BETA * 0.02, rel=1e-9)
    gone_x = bars[(bars["open_ts"] < TX) | (bars["open_ts"] > TX + H)]
    r3 = leg_label(inputs(leg(bars1m=gone_x)), T, +1, 24)
    assert r3.invalid_reason == "exit_price_missing"


def test_btc_exit_halted_and_beta_missing_and_no_instance():
    fill = TX + 7 * H + 39 * MIN
    vol = {x: 0.0 for x in range(fill + MIN - 60 * MIN, fill + MIN, MIN)}
    bt = btc(bars1m=m1(100.0, {TX: (102.0, 102.0)}, vol=vol))
    assert leg_label(inputs(_halted_leg(fill), bt), T, +1, 24).invalid_reason == "btc_exit_halted"
    bb = b15(100.0, 1.0)
    assert leg_label(inputs(bt=btc(bars15=bb[bb["open_ts"] >= T - 10 * D])), T, +1, 24).invalid_reason \
        == "beta_missing"
    assert leg_label(inputs(leg(insts=[Instance(0, T + H, None, None)])), T, +1, 24).invalid_reason \
        == "no_instance"


def test_invalid_rate_and_arguments():
    ok = leg_label(inputs(), T, +1, 24)
    bad = leg_label(inputs(leg(funding=fund(1e-4, 10.0).iloc[:0])), T, +1, 24)
    assert bad.invalid_reason == "funding_missing" and bad.label_known_at == TX + MIN
    assert invalid_rate([ok, bad, None, ok]) == pytest.approx(1 / 3)
    assert math.isnan(invalid_rate([None]))
    with pytest.raises(ValueError):
        leg_label(inputs(), T, 0, 24)
    with pytest.raises(ValueError):
        leg_label(inputs(), T, +1, 48)
    with pytest.raises(ValueError):
        leg_label(inputs(window=(T + D, WIN[1])), T, +1, 24)


def test_exit_gap_bar_after_cessation_not_used():
    # t_x < cessation < t_x + 60 min and the bar at t_x is missing: the gap rule must not take a bar from
    # after the instance end (dormant, belongs to no instance; E10)
    last = TX + 30 * MIN
    inst0 = Instance(0, S0, last, last + 7 * D)
    bars = m1(10.0, {TX + 40 * MIN: (50.0, 50.0)}, vol={TX + 40 * MIN: 0.0})
    bars = bars[(bars["open_ts"] < TX) | (bars["open_ts"] >= last)]
    lg = leg(bars1m=bars, insts=[inst0], terminal=TerminalEvent(inst0, last, "C", None, None, None))
    assert leg_label(inputs(lg), T, +1, 24).invalid_reason == "exit_price_missing"


def test_half_spreads_at_fill_times():
    # leg: h_a(t_e) = 6 bps, h_a(t_x) = h_fill = 40 bps; BTC: h_B(t_e) = 2 bps, h_B(t_x) = 5 bps
    lg = leg(bars15=b15s(10.0, 2.0, 3e-4, 20e-4))
    bt = btc(bars15=b15s(100.0, 1.0, 1e-4, 2.5e-4))
    r = leg_label(inputs(lg, bt), T, +1, 24)
    assert r.beta == pytest.approx(BETA, rel=1e-9) and r.flags == ()
    # by hand: q = 0.1, q_B = 0.017
    exp = dict(funding_leg=-3e-4, price_resid=0.066,
               fee_in=F * 0.1 * 10 * (1 + 8e-4), fee_out=F * 0.1 * 11 * (1 - 42e-4),
               ss_in=0.1 * 10 * 8e-4, ss_out=0.1 * 11 * 42e-4,
               hedge_cost=F * 0.017 * 100 * (1 - 4e-4) + F * 0.017 * 102 * (1 + 7e-4)
               + 0.017 * 100 * 4e-4 + 0.017 * 102 * 7e-4,
               hedge_funding=1.02e-3)
    assert_components(r, exp)
    rs = leg_label(inputs(lg, bt), T, -1, 24)
    assert_components(rs, hand(-1, 10.0, 11.0, 100.0, 102.0, 3e-3, 3 * 2e-2, ha_e=6e-4, ha_x=40e-4,
                               hb_e=2e-4, hb_x=5e-4))
    # halted exit: max(h_fill = 40 bps, 25 bps) = 40 bps, 3 x slip; BTC exits at t_x_eff at its own 5 bps
    fill = TX + 7 * H + 39 * MIN
    lgh = replace(_halted_leg(fill), bars15=b15s(10.0, 2.0, 3e-4, 20e-4))
    bth = replace(bt, bars1m=m1(100.0, {TX: (102.0, 102.0), fill + MIN: (104.0, 104.0)}))
    rh = leg_label(inputs(lgh, bth), T, +1, 24)
    assert "halted_exit" in rh.flags and rh.t_x_eff == fill + MIN
    assert rh.components["ss_out"] == pytest.approx(0.1 * 12.5 * (40e-4 + 6e-4), rel=1e-9)
    assert rh.components["fee_out"] == pytest.approx(F * 0.1 * 12.5 * (1 - 46e-4), rel=1e-9)
    assert_components(rh, hand(+1, 10.0, 12.5, 100.0, 104.0, 4e-3, 4 * 2e-2, ha_e=6e-4, ha_x=40e-4,
                               sl_x=3 * SLIP, hb_e=2e-4, hb_x=5e-4))
    # below 25 bps the stress floor binds: a2 = 10 bps -> h_fill = 20 bps -> 25 bps
    lgl = replace(_halted_leg(fill), bars15=b15s(10.0, 2.0, 3e-4, 10e-4))
    rl = leg_label(inputs(lgl, bth), T, +1, 24)
    assert rl.components["ss_out"] == pytest.approx(0.1 * 12.5 * (25e-4 + 6e-4), rel=1e-9)


def test_terminal_a_without_settle_time_fails_closed():
    last = TX - 2 * H
    inst = Instance(0, S0, last, last + 7 * D)
    for st, px in ((None, 9.0), (last - 10 * MIN, None), (last - 10 * MIN, float("nan"))):
        ev = TerminalEvent(inst, last, "A", st, px, "official_historical")
        lg = replace(_ending_leg(last, "C"), terminal=ev)
        assert leg_label(inputs(lg), T, +1, 24).invalid_reason == "terminal_not_reconstructible"


def test_label_runs_no_git(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("git / V6 artefacts read during a label computation")

    monkeypatch.setattr(windows, "m6", boom)
    monkeypatch.setattr("luna.v7.spec._git", boom)
    monkeypatch.setattr("luna.v7.spec._git_show", boom)
    assert leg_label(inputs(), T, +1, 24).invalid_reason is None
    fill = TX + 7 * H + 39 * MIN
    bt = btc(bars1m=m1(100.0, {TX: (102.0, 102.0), fill + MIN: (104.0, 104.0)}))
    assert leg_label(inputs(_halted_leg(fill), bt), T, +1, 24).invalid_reason is None
    with pytest.raises(guard.HoldoutBlocked):
        leg_label(inputs(window=windows.W_HOLD), windows.W_HOLD[0] + 10 * D + 5 * MIN, +1, 24)


def test_m6_span_required_and_validated():
    with pytest.raises(TypeError):
        LabelInputs("ABCUSDT", leg(), btc(), WIN, NOW)  # type: ignore[call-arg]
    for bad in (None, (M6[0],), ("x", None), (M6[0], M6[0] - 1)):
        with pytest.raises(ValueError):
            leg_label(LabelInputs("ABCUSDT", leg(), btc(), WIN, NOW, bad), T, +1, 24)  # type: ignore[arg-type]
