"""Book accounting objects (spec S-8.1-S-8.4, K10, P6, P11) on hand-built synthetic BookRuns.

The main scenario (``scenario()``) is computed by hand in the comments; times are hours after LO.
Fills: ts, symbol, instance, direction, qty, ref_mid, beta (entries), btc_ref; every fill costs fee 0.01,
spread 0.02, slippage 0.005 (C = 0.035 per fill).

* AAA#0 long, episode A1 (cluster C1): 1h buy 2 @10 (beta 1.5, B 100); tick 2h mark 11 (B 110); 2h buy 1
  @11.5 (B 112; the tick comes first); 3h sell 3 @12 (B 120).
  PR = 2(11-10) + 2(11.5-11) + 3(12-11.5) = 4.5 = -(20 + 11.5 - 36).
  mkt = 2*10*1.5*0.1 + 2*11*1.5*(112/110-1) + 3*11.5*1.5*(120/112-1) = 3 + 0.6 + 3.6964285714...
* AAA#0 long, episode A2 (C1: starts 48 h after A1 ended): 51h buy 1 @20 (beta 2, B 200); tick 52h mark 18
  (B 190); 53h sell 1 @19 (B 195). PR = -2 + 1 = -1; mkt = 20*2*(-0.05) + 18*2*(5/190).
* AAA#0 long, episode A3 (C2: starts 73 h after A2 ended): 126h buy 1 @30 (beta 1, B 300); 127h sell 1 @33
  (B 330). PR = 3; mkt = 30*1*0.1 = 3.
* AAA#0 short, episode S (C3: other side): 60h sell 1 @10 (beta 1, B 100); 61h buy 1 @9 (B 100). PR = 1,
  mkt = 0.
* BBB#0 short, episode B1 (C4): 1h sell 4 @5 (beta 0.5, B 100); tick 2h mark 4.5 (B 110); 3h buy 4 @4
  (B 120). PR = 2 + 2 = 4; mkt = -4*5*0.5*0.1 - 4*4.5*0.5*(120/110-1) = -1 - 0.8181818...
* BTC hedge: 1h sell 0.01 @100; tick 2h mark 110; 3h buy 0.01 @120. PR_BTC = -0.1 - 0.1 = -0.2.
* Funding: A1 +0.05 (2h), B1 -0.02 (2h), hedge +0.01 (2h), A2 +0.03 (52h).
Days: [0, 24h], ..., [120h, unwind]; every fill / tick / settlement lies inside one day.
"""

from __future__ import annotations

import math
from dataclasses import replace

import numpy as np
import pandas as pd
import pytest

from luna.v7 import accounting as acc
from luna.v7.book import engine
from luna.v7.book.engine import BookRun
from luna.v7.timeutil import MIN, D, H, to_ns
from tests.v7.book import engine_fixtures as fx

LO = to_ns("2024-03-04")
END = LO + 6 * D
U = END - MIN
FEE, SPR, SLP = 0.01, 0.02, 0.005
C = FEE + SPR + SLP
BTC = "BTCUSDT"

MKT_A1 = 3.0 + 0.6 + 3 * 11.5 * 1.5 * (120 / 112 - 1)
MKT_A2 = 20 * 2 * (190 / 200 - 1) + 18 * 2 * (195 / 190 - 1)
MKT_A3 = 30 * 1 * (330 / 300 - 1)
MKT_B1 = -4 * 5 * 0.5 * 0.1 - 4 * 4.5 * 0.5 * (120 / 110 - 1)


def fill(h, sym, inst, d, qty, ref, pid, role, q_after, *, beta=None, btc=None, fee=FEE, spr=SPR, slp=SLP,
         side=None):
    return dict(ts=LO + int(h * H), symbol=sym, instance=inst, direction=d, qty=qty, ref_mid=ref,
                price=ref * (1 + d * (spr + slp) / (qty * ref)), fee=fee, spread_cost=spr, slip_cost=slp,
                liquidity="taker", reason="x", position_id=pid, flags=(), adverse=None,
                beta=np.nan if beta is None else beta, btc_ref=ref if (btc is None and sym == BTC) else btc,
                role=role, side=d if side is None else side, q_after=q_after, entry_decision_ts=LO)


def tick(h, sym, inst, pid, role, qty, mark):
    return dict(ts=LO + int(h * H), symbol=sym, instance=inst, position_id=pid, role=role, qty=qty, mark=mark,
                mark_source="mark", notional=qty * mark, pending_exit=None)


def fund(h, sym, inst, pid, role, cash):
    return dict(ts=LO + int(h * H), symbol=sym, instance=inst, position_id=pid, role=role, qty=1.0, rate=0.0,
                mark=1.0, cash=cash)


def daily_rows(lo, end, n_days, funding, fees, spread_slip, price):
    """Engine-style daily table: day i = [lo + i D, lo + (i+1) D], the last row closes at end - 1 min."""
    rows, eq = [], 1.0
    for i in range(n_days):
        s, e = lo + i * D, (lo + (i + 1) * D if i < n_days - 1 else end - MIN)
        net = funding[i] - fees[i] - spread_slip[i] + price[i]
        rows.append(dict(day=s // D, start_ts=s, end_ts=e, equity_start=eq, equity_end=eq + net, net=net,
                         funding=funding[i], fees=fees[i], spread_slip=spread_slip[i], price=price[i]))
        eq += net
    return pd.DataFrame(rows)


def make_run(fills, ticks, funding, btc_marks, daily, *, lo=LO, end=END, invalid=None):
    marks = pd.DataFrame([dict(ts=LO + int(h * H), equity=1.0, cash=1.0, gross_legs=0.0, hedge_notional=0.0,
                               btc_mark=b) for h, b in btc_marks])
    return BookRun(arm="A0", h_star=24, execution="E_T", layer="L-norm", window=(lo, end),
                   fills=pd.DataFrame(fills), marks=marks, positions=pd.DataFrame(ticks),
                   funding_flows=pd.DataFrame(funding), daily=daily, decisions=pd.DataFrame(), integrity=(),
                   invalid=invalid)


A1, A2, A3, S1, B1, HG = ("AAAUSDT#0@1", "AAAUSDT#0@51", "AAAUSDT#0@126", "AAAUSDT#0@60", "BBBUSDT#0@1",
                          "BTCUSDT#0@hedge1")


def scenario_parts(price_d0=8.3, swap_tick_order=False):
    a, b = "AAAUSDT", "BBBUSDT"
    fills = [
        fill(1, a, 0, 1, 2.0, 10.0, A1, "leg", 2.0, beta=1.5, btc=100.0),
        fill(1, b, 0, -1, 4.0, 5.0, B1, "leg", -4.0, beta=0.5, btc=100.0),
        fill(1, BTC, 0, -1, 0.01, 100.0, HG, "hedge", -0.01),
        fill(2, a, 0, 1, 1.0, 11.5, A1, "leg", 3.0, btc=112.0),
        fill(3, a, 0, -1, 3.0, 12.0, A1, "leg", 0.0, btc=120.0, side=1),
        fill(3, b, 0, 1, 4.0, 4.0, B1, "leg", 0.0, btc=120.0, side=-1),
        fill(3, BTC, 0, 1, 0.01, 120.0, HG, "hedge", 0.0, side=-1),
        fill(51, a, 0, 1, 1.0, 20.0, A2, "leg", 1.0, beta=2.0, btc=200.0),
        fill(53, a, 0, -1, 1.0, 19.0, A2, "leg", 0.0, btc=195.0, side=1),
        fill(60, a, 0, -1, 1.0, 10.0, S1, "leg", -1.0, beta=1.0, btc=100.0),
        fill(61, a, 0, 1, 1.0, 9.0, S1, "leg", 0.0, btc=100.0, side=-1),
        fill(126, a, 0, 1, 1.0, 30.0, A3, "leg", 1.0, beta=1.0, btc=300.0),
        fill(127, a, 0, -1, 1.0, 33.0, A3, "leg", 0.0, btc=330.0, side=1),
    ]
    ticks = [
        tick(2, a, 0, A1, "leg", 2.0, 11.0),
        tick(2, b, 0, B1, "leg", -4.0, 4.5),
        tick(2, BTC, 0, HG, "hedge", -0.01, 110.0),
        tick(52, a, 0, A2, "leg", 1.0, 18.0),
    ]
    if swap_tick_order:  # an A1 tick placed AFTER its same-timestamp fill (the wrong S-8.3 order)
        ticks[0] = tick(2, a, 0, A1, "leg", 3.0, 11.0)
    funding = [fund(2, a, 0, A1, "leg", 0.05), fund(2, b, 0, B1, "leg", -0.02),
               fund(2, BTC, 0, HG, "hedge", 0.01), fund(52, a, 0, A2, "leg", 0.03)]
    btc_marks = [(2, 110.0), (52, 190.0)]
    n = [7, 0, 4, 0, 0, 2]  # fills per day
    daily = daily_rows(LO, END, 6, funding=[0.04, 0, 0.03, 0, 0, 0], fees=[FEE * k for k in n],
                       spread_slip=[(SPR + SLP) * k for k in n], price=[price_d0, 0, 0.0, 0, 0, 3.0])
    return fills, ticks, funding, btc_marks, daily


def scenario(**kw):
    return make_run(*scenario_parts(**kw))


def test_pr_summation_by_parts():
    run = scenario()
    led = acc.account(run)
    eps = {e.position_ids[0]: e for e in led.episodes}
    assert eps[A1].pr == pytest.approx(4.5, abs=1e-12)
    assert eps[A2].pr == pytest.approx(-1.0, abs=1e-12)
    assert eps[A3].pr == pytest.approx(3.0, abs=1e-12)
    assert eps[S1].pr == pytest.approx(1.0, abs=1e-12)
    assert eps[B1].pr == pytest.approx(4.0, abs=1e-12)
    assert led.PR_BTC == pytest.approx(-0.2, abs=1e-12)
    assert led.PR == pytest.approx(11.3, abs=1e-12)
    # event sum == -sum dq * p(f) (summation by parts; q = 0 at both ends)
    f = run.fills
    assert led.PR == pytest.approx(-(f["direction"] * f["qty"] * f["ref_mid"]).sum(), abs=1e-12)
    assert led.PR_fills == pytest.approx(led.PR, abs=1e-12)
    # mkt_c with B = btc_ref at fills, btc_mark at ticks; beta of the opening decision for the whole episode
    assert eps[A1].mkt == pytest.approx(MKT_A1, abs=1e-12)
    assert eps[A2].mkt == pytest.approx(MKT_A2, abs=1e-12)
    assert eps[A3].mkt == pytest.approx(MKT_A3, abs=1e-12)
    assert eps[S1].mkt == 0.0
    assert eps[B1].mkt == pytest.approx(MKT_B1, abs=1e-12)
    assert led.MKT == pytest.approx(MKT_A1 + MKT_A2 + MKT_A3 + MKT_B1 - 0.2, abs=1e-12)
    # every leg fill belongs to exactly one episode
    assert sum(e.n_fills for e in led.episodes) == int((f["role"] == "leg").sum())


def test_tick_before_fill_same_ts():
    led = acc.account(scenario())
    a1 = next(e for e in led.episodes if e.position_ids == (A1,))
    # tick (11, q = 2) THEN the fill at 11.5 (q -> 3) at 2h: the hand mkt
    assert a1.mkt == pytest.approx(MKT_A1, abs=1e-12)
    # the wrong order (fill first, then the tick at the same timestamp) would give another mkt
    wrong = 2 * 10 * 1.5 * (112 / 100 - 1) + 3 * 11.5 * 1.5 * (110 / 112 - 1) + 3 * 11 * 1.5 * (120 / 110 - 1)
    assert abs(wrong - MKT_A1) > 0.1
    # a tick row whose position does not match the position before the same-timestamp fill is refused
    with pytest.raises(ValueError, match="tick"):
        acc.account(scenario(swap_tick_order=True))


def test_cluster_72h():
    run = scenario()
    cl = acc.clusters(run)
    by = {(c.asset, c.side, c.episodes[0].start_ts): c for c in cl}
    c1 = by[("AAAUSDT#0", 1, LO + 1 * H)]
    assert [e.position_ids for e in c1.episodes] == [(A1,), (A2,)]  # A2 starts 48 h after A1 ended
    c2 = by[("AAAUSDT#0", 1, LO + 126 * H)]  # 73 h after A2 ended: a new cluster
    assert [e.position_ids for e in c2.episodes] == [(A3,)]
    c3 = by[("AAAUSDT#0", -1, LO + 60 * H)]  # same asset, other side
    c4 = by[("BBBUSDT#0", -1, LO + 1 * H)]
    assert len(cl) == 4
    assert c1.pr_c == pytest.approx(3.5, abs=1e-12)
    assert c1.mkt_c == pytest.approx(MKT_A1 + MKT_A2, abs=1e-12)
    assert c1.rho_c == pytest.approx(3.5 - MKT_A1 - MKT_A2, abs=1e-12)
    assert c3.rho_c == pytest.approx(1.0, abs=1e-12) and c4.rho_c == pytest.approx(4 - MKT_B1, abs=1e-12)
    assert c2.rho_c == pytest.approx(0.0, abs=1e-12)
    # exactly 72 h joins; 72 h + 1 ns splits
    assert len(acc.clusters(run, gap_h=48)) == 4 and len(acc.clusters(run, gap_h=47)) == 5
    assert len(acc.clusters(run, gap_h=73)) == 3
    # another instance of the same symbol is another asset (E10; AMB-21 (a)): AAA#1 at 5h-6h, 2 h after A1
    fills, ticks, funding, bm, daily = scenario_parts()
    fills += [fill(5, "AAAUSDT", 1, 1, 1.0, 10.0, "AAAUSDT#1@5", "leg", 1.0, beta=1.0, btc=100.0),
              fill(6, "AAAUSDT", 1, -1, 1.0, 10.0, "AAAUSDT#1@5", "leg", 0.0, btc=100.0, side=1)]
    daily.loc[0, ["fees", "spread_slip"]] += [2 * FEE, 2 * (SPR + SLP)]
    cl2 = acc.clusters(make_run(fills, ticks, funding, bm, daily))
    assert len(cl2) == 5 and {c.asset for c in cl2} == {"AAAUSDT#0", "AAAUSDT#1", "BBBUSDT#0"}
    led = acc.account(make_run(fills, ticks, funding, bm, daily))
    assert led.n_clusters == 5 and acc.k10_ok(led)


def test_k10_invariant_holds_and_detects_break():
    led = acc.account(scenario())
    tol = 1e-9 * led.fill_notional
    assert led.fill_notional == pytest.approx(20 + 20 + 1 + 11.5 + 36 + 16 + 1.2 + 20 + 19 + 10 + 9 + 30 + 33)
    assert abs(sum(c.rho_c for c in led.clusters) + led.MKT - led.PR) <= tol
    assert acc.k10_ok(led)
    # a broken attribution
    assert not acc.k10_ok(replace(led, MKT=led.MKT + 10 * tol))
    assert acc.k10_ok(replace(led, MKT=led.MKT + 0.5 * tol))
    # the daily price column disagreeing with PR (book total and one day moved)
    assert not acc.k10_ok(acc.account(scenario(price_d0=8.3 + 1e-6)))
    fills, ticks, funding, bm, daily = scenario_parts()
    daily.loc[0, "price"] -= 1e-3
    daily.loc[2, "price"] += 1e-3  # total unchanged, the day split is wrong
    assert not acc.k10_ok(acc.account(make_run(fills, ticks, funding, bm, daily)))


def test_net_equals_sum_of_assets_plus_hedge():
    led = acc.account(scenario())
    assert led.F_leg == pytest.approx(0.06) and led.F_hedge == pytest.approx(0.01)
    assert led.F_net == pytest.approx(0.07)
    assert (led.F_leg_credits, led.F_leg_debits) == (pytest.approx(0.08), pytest.approx(-0.02))
    assert led.C_exec == pytest.approx(13 * C) and led.C_hedge == pytest.approx(2 * C)
    assert (led.fees, led.spread, led.slippage) == (pytest.approx(13 * FEE), pytest.approx(13 * SPR),
                                                    pytest.approx(13 * SLP))
    assert led.NET == pytest.approx(0.07 - 13 * C + 11.3, abs=1e-12)
    assert led.CAC == pytest.approx(0.07 - 13 * C, abs=1e-12)
    na = acc.per_asset_net(led)
    assert na["AAAUSDT#0"] == pytest.approx(0.08 + 7.5 - 9 * C, abs=1e-12)
    assert na["BBBUSDT#0"] == pytest.approx(-0.02 + 4 - 2 * C, abs=1e-12)
    assert led.H == pytest.approx(0.01 - 0.2 - 2 * C, abs=1e-12)
    assert sum(na.values()) + led.H == pytest.approx(led.NET, abs=1e-12)
    assert led.equity_change == pytest.approx(led.NET, abs=1e-12)


def test_daily_components_sum():
    led = acc.account(scenario())
    d = led.daily
    assert list(d["day"]) == [(LO + i * D) // D for i in range(6)]
    assert np.allclose(d["price"], [8.3, 0, 0, 0, 0, 3.0], atol=1e-12)
    assert np.allclose(d["funding"], [0.04, 0, 0.03, 0, 0, 0], atol=1e-15)
    assert np.allclose(d["fees"], [7 * FEE, 0, 4 * FEE, 0, 0, 2 * FEE], atol=1e-15)
    assert np.allclose(d["spread_slip"], [k * (SPR + SLP) for k in (7, 0, 4, 0, 0, 2)], atol=1e-15)
    assert np.allclose(d["net"], d["funding"] - d["fees"] - d["spread_slip"] + d["price"], rtol=0, atol=1e-15)
    assert d["net"].sum() == pytest.approx(led.NET, abs=1e-12)
    assert d["price"].sum() == pytest.approx(led.PR, abs=1e-12)
    assert d["funding"].sum() == pytest.approx(led.F_net, abs=1e-15)
    assert (d["fees"] + d["spread_slip"]).sum() == pytest.approx(led.C_exec, abs=1e-15)
    assert acc.daily_ok(led)
    # a day boundary at a fill: a fill at 00:00 belongs to the NEW day, a tick / settlement to the closing one
    fills, ticks, funding, bm, daily = scenario_parts()
    fills[7] = fill(48, "AAAUSDT", 0, 1, 1.0, 20.0, A2, "leg", 1.0, beta=2.0, btc=200.0)  # A2 entry at 48h
    led2 = acc.account(make_run(fills, ticks, funding, bm, daily))
    assert led2.daily["fees"].iloc[2] == pytest.approx(4 * FEE)  # the 48h (00:00) fill is day 2's
    assert led2.daily["fees"].iloc[1] == 0.0 and acc.daily_ok(led2)
    funding2 = funding + [fund(48, "AAAUSDT", 0, A2, "leg", 0.5)]
    led3 = acc.account(make_run(fills, ticks, funding2, bm, daily))
    assert led3.daily["funding"].iloc[1] == pytest.approx(0.5)  # a 00:00 settlement closes day 1
    assert not acc.daily_ok(led3)  # the engine's table has no such settlement
    # S1 held across 00:00 of day 3 (72h): the 72h tick (q = -1, 10 -> 9.5) closes day 2, the 72h close fill
    # (9.5 -> 9) is day 3's
    fills, ticks, funding, bm, _ = scenario_parts()
    fills[10] = fill(72, "AAAUSDT", 0, 1, 1.0, 9.0, S1, "leg", 0.0, btc=100.0, side=-1)
    ticks.append(tick(72, "AAAUSDT", 0, S1, "leg", -1.0, 9.5))
    n = [7, 0, 3, 1, 0, 2]
    daily = daily_rows(LO, END, 6, funding=[0.04, 0, 0.03, 0, 0, 0], fees=[FEE * k for k in n],
                       spread_slip=[(SPR + SLP) * k for k in n], price=[8.3, 0, -0.5, 0.5, 0, 3.0])
    led4 = acc.account(make_run(fills, ticks, funding, bm + [(72, 100.0)], daily))
    assert np.allclose(led4.daily["price"], [8.3, 0, -0.5, 0.5, 0, 3.0], atol=1e-12)
    assert acc.k10_ok(led4) and acc.daily_ok(led4)
    # a tick of a position that is already flat is refused
    with pytest.raises(ValueError, match="tick"):
        acc.account(make_run(fills, ticks + [tick(72, "AAAUSDT", 0, A2, "leg", 1.0, 18.0)], funding,
                             bm + [(72, 100.0)], daily))


def test_net_trim_m_rule():
    led = acc.account(scenario())
    assert led.n_clusters == 4
    rho = sorted((c.rho_c for c in led.clusters), reverse=True)
    assert rho[0] == pytest.approx(4 - MKT_B1) and rho[1] == pytest.approx(1.0)
    # m = max(3, ceil(0.05 * 4)) = 3: the two positive clusters (the third is ~0 / negative) and max(MKT, 0)
    exp = led.NET - (4 - MKT_B1) - 1.0 - max(rho[2], 0.0) - max(led.MKT, 0.0)
    assert acc.net_trim(led) == pytest.approx(exp, abs=1e-12)
    assert acc.net_trim(led, m=1) == pytest.approx(led.NET - (4 - MKT_B1) - led.MKT, abs=1e-12)
    assert acc.p11_ok(led) is (led.CAC > 0 and acc.net_trim(led) > 0)
    # 81 clusters with rho_c = 1, 2, ..., 81 (no BTC moves): m = max(3, ceil(4.05)) = 5 -> top 77..81 removed
    fills, n = [], 81
    for i in range(n):
        sym, pid = f"S{i:03d}USDT", f"S{i:03d}USDT#0@1"
        fills += [fill(1, sym, 0, 1, 1.0, 10.0, pid, "leg", 1.0, beta=1.0, btc=100.0, fee=0, spr=0, slp=0),
                  fill(2, sym, 0, -1, 1.0, 11.0 + i, pid, "leg", 0.0, btc=100.0, fee=0, spr=0, slp=0, side=1)]
    pr = float(sum(1 + i for i in range(n)))
    daily = daily_rows(LO, LO + D, 1, [0.0], [0.0], [0.0], [pr])
    big = acc.account(make_run(fills, [], [], [], daily, end=LO + D))
    assert big.n_clusters == 81 and big.MKT == 0.0 and big.NET == pytest.approx(pr)
    assert acc.m_rule(81) == 5 and acc.m_rule(60) == 3 and acc.m_rule(61) == 4 and acc.m_rule(0) == 3
    assert acc.net_trim(big) == pytest.approx(pr - (81 + 80 + 79 + 78 + 77))
    assert not acc.p11_ok(big)  # CAC = 0 is not > 0 (R1)


def test_p6_stress_only_costs_change():
    run = scenario()
    base = acc.account(run)
    st = acc.stressed(run)
    assert (st.fees, st.spread, st.slippage) == (pytest.approx(13 * FEE * 1.5), pytest.approx(13 * SPR * 3.0),
                                                 pytest.approx(13 * SLP * 1.5))
    assert st.C_exec == pytest.approx(13 * (1.5 * FEE + 3 * SPR + 1.5 * SLP))
    for k in ("PR", "F_net", "F_leg", "F_hedge", "MKT", "PR_BTC", "fill_notional"):
        assert getattr(st, k) == getattr(base, k)
    assert st.NET == pytest.approx(base.NET - (st.C_exec - base.C_exec), abs=1e-12)
    # same fills, times, quantities and reference prices
    for col in ("ts", "symbol", "qty", "direction", "ref_mid", "position_id"):
        assert list(st.fills[col]) == list(run.fills[col])
    d0 = 7 * (0.5 * FEE + 2.0 * SPR + 0.5 * SLP)
    assert st.daily["net"].iloc[0] == pytest.approx(base.daily["net"].iloc[0] - d0, abs=1e-12)
    assert acc.mean_daily_net(st) == pytest.approx(st.NET / 6, abs=1e-12)
    assert acc.mean_daily_net(base) == pytest.approx(base.NET / 6, abs=1e-12)
    e0 = 1.0 + np.concatenate([[0.0], np.cumsum(st.daily["net"].to_numpy())[:-1]])  # stressed day-start E
    assert acc.mean_daily_return(st) == pytest.approx(np.mean(st.daily["net"].to_numpy() / e0), abs=1e-12)
    assert acc.period_net(st, [LO, LO + 3 * D, END]) == pytest.approx(
        [st.daily["net"].iloc[:3].sum(), st.daily["net"].iloc[3:].sum()], abs=1e-12)
    assert acc.k10_ok(st) and acc.daily_ok(st)
    assert st.stress == (1.5, 1.5, 3.0) and base.stress is None
    # multipliers: spread x3, slippage x1.5 (swapping them changes the result)
    sw = acc.stressed(run, fee=1.5, slip=3.0, spread=1.5)
    assert sw.C_exec != pytest.approx(st.C_exec)


def test_invalid_run_refused_and_inputs_checked():
    run = scenario()
    with pytest.raises(ValueError, match="INVALID"):
        acc.account(replace(run, invalid="INVALID RUN — terminal settlement not reconstructible"))
    with pytest.raises(ValueError, match="INVALID"):
        acc.stressed(replace(run, invalid="INVALID RUN — x"))
    fills, ticks, funding, bm, daily = scenario_parts()
    fills[3]["btc_ref"] = None  # B missing inside an episode: mkt_c cannot be computed (no invented price)
    with pytest.raises(ValueError, match="BTC"):
        acc.account(make_run(fills, ticks, funding, bm, daily))
    fills, ticks, funding, bm, daily = scenario_parts()
    fills[0]["beta"] = np.nan  # an episode without beta-hat of its opening decision
    with pytest.raises(ValueError, match="beta"):
        acc.account(make_run(fills, ticks, funding, bm, daily))
    fills, ticks, funding, bm, daily = scenario_parts()
    fills[4]["q_after"] = 0.5  # not flat at the end / inconsistent with dq
    with pytest.raises(ValueError):
        acc.account(make_run(fills, ticks, funding, bm, daily))
    fills, ticks, funding, bm, daily = scenario_parts()
    fills[1]["ref_mid"] = math.nan
    with pytest.raises(ValueError):
        acc.account(make_run(fills, ticks, funding, bm, daily))


def test_engine_run_accounts_and_k10():
    r = engine.run("A0", fx.base_inputs(), (fx.LO, fx.END), 24)
    led = acc.account(r)
    assert acc.k10_ok(led) and acc.daily_ok(led)
    assert led.NET == pytest.approx(r.daily["equity_end"].iloc[-1] - 1.0, abs=1e-12)
    assert led.n_clusters == 2 and led.PR == pytest.approx(0.0, abs=1e-12)
