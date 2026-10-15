"""S-13.2 P1–P11, S-13.4 precedence, S-13.3 secondary family, K table (luna.v7.verdict)."""

from __future__ import annotations

import itertools
import math
from dataclasses import replace

import numpy as np
import pandas as pd
import pytest

from luna.v7 import accounting as acc
from luna.v7 import stats
from luna.v7 import verdict as V
from luna.v7.accounting import Cluster, Episode
from luna.v7.book.execution import (
    INVALID_BTC,
    INVALID_EXIT_GAP,
    INVALID_MARK,
    INVALID_NO_EXIT,
    INVALID_TERMINAL,
)
from luna.v7.timeutil import MIN, D
from luna.v7.universe import LeakageError
from tests.v7 import leak_fixtures as lf
from tests.v7.run_fixtures import LO, funding_run

N = 120  # holdout days in the synthetic runs (4 quarters of 30 days)
EDGES = [LO + k * 30 * D for k in range(5)]
ALLP = {p: True for p in V.P_IDS}


# --- precedence ------------------------------------------------------------------------------------------
def test_verdict_precedence():
    c = V.classify
    base = dict(invalid=False, leakage=False, dev_kill=False, criteria=ALLP, p1_em=True)
    assert c(**base) == V.ADVANCE
    # each class alone
    assert c(**{**base, "invalid": True, "criteria": {}}) == V.INVALID
    assert c(**{**base, "leakage": True}) == V.LEAKAGE
    assert c(**{**base, "dev_kill": True}) == V.NO_EDGE
    assert c(**{**base, "criteria": {**ALLP, "P1": False}, "p1_em": False}) == V.NO_EDGE
    assert c(**{**base, "criteria": {**ALLP, "P1": False}, "p1_em": True}) == V.MAKER
    assert c(**{**base, "criteria": {**ALLP, "P4": False}}) == V.INCONCLUSIVE
    for p in V.P_IDS:
        if p not in ("P1", "P4"):
            assert c(**{**base, "criteria": {**ALLP, p: False}}) == V.NOGO
    # the most severe applicable class wins, for every combination
    order = [V.INVALID, V.LEAKAGE, V.NO_EDGE, V.MAKER, V.INCONCLUSIVE, V.NOGO, V.ADVANCE]
    assert list(V.CLASSES) == order
    for inv, leak, kill, p1, p1m, p4, p9 in itertools.product([False, True], repeat=7):
        crit = {**ALLP, "P1": p1, "P4": p4, "P9": p9}
        got = c(invalid=inv, leakage=leak, dev_kill=kill, criteria={} if inv else crit, p1_em=p1m)
        if inv:
            want = V.INVALID
        elif leak:
            want = V.LEAKAGE
        elif kill or (not p1 and not p1m):
            want = V.NO_EDGE
        elif not p1:
            want = V.MAKER
        elif not p4:
            want = V.INCONCLUSIVE
        elif not p9:
            want = V.NOGO
        else:
            want = V.ADVANCE
        assert got == want
    # a positive P&L alone never advances: P1 missing from the criteria is refused
    with pytest.raises(ValueError):
        c(**{**base, "criteria": {p: True for p in V.P_IDS if p != "P1"}})


def test_k_table():
    assert list(V.K_TABLE) == [f"K{i}" for i in range(1, 12)]
    assert V.K_TABLE["K10"][1] == "INVALID RUN"
    assert V.K_TABLE["K6"][1] == "NO-GO: maker-dependent"
    assert "historical terminal execution not reconstructible" in V.K_TABLE["K11"][1]
    assert "ML* only (EC-11)" in V.K_TABLE["K11"][0].replace("\n", " ")


# --- P helpers --------------------------------------------------------------------------------------------
def _marks(gross_on_counts, frac=0.10, e=2.0):
    rows = []
    for d, n_on in enumerate(gross_on_counts):
        for q in range(96):
            ts = LO + d * D + q * 15 * MIN
            g = frac * e if q < n_on else frac * e * (1 - 1e-9)
            rows.append(dict(ts=ts, equity=e, cash=e, gross_legs=g, hedge_notional=0.0, btc_mark=100.0))
    return pd.DataFrame(rows)


def test_p4_active_day_definition():
    # day 0: 48 quarter-hours at exactly 10 % of E (counts); day 1: 47; day 2: 96; day 3: 0
    assert V.active_days(_marks([48, 47, 96, 0])) == 2
    assert V.active_days(_marks([48], frac=0.0999)) == 0
    assert V.active_days(pd.DataFrame()) == 0
    # a quarter-hour belongs to the UTC day of its tick (00:00 starts the new day)
    m = _marks([96, 0])
    m.loc[m.ts == LO + D, "gross_legs"] = 1.0  # 00:00 of day 1 -> day 1 gets 1, not day 0
    assert V.active_days(m) == 1
    bad = _marks([48]).assign(equity=np.nan)
    with pytest.raises(ValueError):
        V.active_days(bad)
    # thresholds: >= 60 active days, >= 100 closed legs, >= 20 clusters
    led = acc.account(funding_run([0.0] * 2))
    eps = [Episode("S", 0, 1, 0, 1, 1.0, (f"p{i}a", f"p{i}b"), 0, 0, 0, 0, 0, 0, 2) for i in range(50)]
    cl = tuple(Cluster(f"S{i}#0", 1, (eps[i],)) for i in range(20))
    led2 = replace(led, episodes=tuple(eps), clusters=cl)
    assert V.closed_legs(led2) == 100 and led2.n_clusters == 20
    run = funding_run([0.0] * 2, marks=_marks([48] * 60))
    ok, d = V.p4(run, led2)
    assert ok and d == dict(active_days=60, closed_legs=100, clusters=20)
    assert not V.p4(funding_run([0.0] * 2, marks=_marks([48] * 59)), led2)[0]
    assert not V.p4(run, replace(led2, episodes=tuple(eps[:49])))[0]
    assert not V.p4(run, replace(led2, clusters=cl[:19]))[0]
    open_ep = replace(eps[0], end_ts=None)
    assert V.closed_legs(replace(led2, episodes=(open_ep, *eps[1:]))) == 98


def _cl(sym, inst, net):
    ep = Episode(sym, inst, 1, 0, 1, 1.0, (f"{sym}#{inst}",), 0.0, 0.0, net, 0.0, 0.0, 0.0, 2)
    return Cluster(f"{sym}#{inst}", 1, (ep,))


def _p5_ledger(qnets, clusters):
    nets = []
    for q in qnets:
        nets += [q] + [0.0] * 29
    return replace(acc.account(funding_run(nets)), clusters=tuple(clusters))


def test_p5_concentration():
    cl = [_cl("A", 0, 0.25), _cl("B", 0, 0.25), _cl("C", 0, 0.0625), _cl("D", 0, 0.0625)]
    led = _p5_ledger([0.375, 0.25, 0.25, 0.125], cl)
    assert led.NET == 1.0
    ok, d = V.p5(led, EDGES)
    assert ok and d["quarters"] == [0.375, 0.25, 0.25, 0.125] and d["top1"] == 0.25 and d["top3"] == 0.5625
    # one quarter > 60 % of NET
    assert not V.p5(_p5_ledger([0.625, 0.125, 0.125, 0.125], cl), EDGES)[0]
    assert V.p5(_p5_ledger([0.5, 0.25, 0.125, 0.125], cl), EDGES)[0]
    # net > 0 in only 1 of 4 quarters
    assert not V.p5(_p5_ledger([0.5, 0.0, 0.0, -0.25], cl), EDGES)[0]
    # top-1 N_a > 0.30 NET
    assert not V.p5(_p5_ledger([0.375, 0.25, 0.25, 0.125], [_cl("A", 0, 0.3125), *cl[1:]]), EDGES)[0]
    # top-1 alone: 0.3125 > 0.30 NET while top-3 = 0.5625 <= 0.60 NET
    alone = [_cl("A", 0, 0.3125), _cl("B", 0, 0.125), _cl("C", 0, 0.125)]
    ok1, d1 = V.p5(_p5_ledger([0.375, 0.25, 0.25, 0.125], alone), EDGES)
    assert not ok1 and d1["top3"] == 0.5625
    # top-3 > 0.60 NET (0.25 + 0.25 + 0.125)
    assert not V.p5(_p5_ledger([0.375, 0.25, 0.25, 0.125], [*cl[:2], _cl("C", 0, 0.125)]), EDGES)[0]
    # N_a is per symbol: two instances of A pool into one N_a = 0.3125 > 0.30
    assert not V.p5(_p5_ledger([0.375, 0.25, 0.25, 0.125], [*cl, _cl("A", 1, 0.0625)]), EDGES)[0]
    with pytest.raises(ValueError):
        V.p5(led, EDGES[:4])
    with pytest.raises(ValueError):
        V.p5(led, [e + D for e in EDGES])


def test_p8_slope_band():
    # date 1: one row (x 0, y 0); date 2: three rows (x 1, 2, 3; y 1, 3, 2). Weights 1/2, 1/6 x3.
    x, y, d = [0.0, 1.0, 2.0, 3.0], [0.0, 1.0, 3.0, 2.0], [1, 2, 2, 2]
    w = np.array([0.5, 1 / 6, 1 / 6, 1 / 6])
    xa, ya = np.array(x), np.array(y)
    mx, my = w @ xa, w @ ya
    want = (w @ ((xa - mx) * (ya - my))) / (w @ (xa - mx) ** 2)
    assert V.p8_slope(x, y, d) == pytest.approx(want)
    assert V.p8_slope(x, y, d) != pytest.approx(np.polyfit(xa, ya, 1)[0])  # not the row-weighted OLS
    assert V.p8_ok(0.5) and V.p8_ok(1.5) and V.p8_ok(1.0)
    assert not V.p8_ok(0.4999) and not V.p8_ok(1.5001) and not V.p8_ok(float("nan"))
    assert math.isnan(V.p8_slope([1.0, 1.0], [0.0, 1.0], [1, 2]))
    with pytest.raises(ValueError):
        V.p8_slope([1.0], [1.0, 2.0], [1, 2])


def test_p11_m_rule():
    def led_with(n_clusters, n_big, net):
        cl = [_cl(f"S{i}", 0, 0.0) for i in range(n_clusters)]
        for i in range(n_big):  # rho_c = PR_c - mkt_c = 0.1
            ep = replace(cl[i].episodes[0], pr=0.1)
            cl[i] = Cluster(cl[i].asset, 1, (ep,))
        return replace(acc.account(funding_run([net])), clusters=tuple(cl), MKT=0.0)

    assert [acc.m_rule(n) for n in (0, 60, 61, 81)] == [3, 3, 4, 5]
    # 60 clusters: m = 3 trims 0.3 -> 0.35 - 0.3 > 0; 61 clusters: m = 4 trims 0.4 -> < 0
    assert acc.p11_ok(led_with(60, 4, 0.35))
    assert not acc.p11_ok(led_with(61, 4, 0.35))
    # max(MKT, 0) is trimmed too; a negative MKT is not added back
    assert not acc.p11_ok(replace(led_with(60, 4, 0.35), MKT=0.06))
    assert acc.p11_ok(replace(led_with(60, 4, 0.35), MKT=-5.0))
    # R1: CAC > 0 required
    led = led_with(60, 0, 0.35)
    assert not acc.p11_ok(replace(led, fees_leg=led.F_net))


def test_p3_drawdown_and_kill():
    marks = pd.DataFrame(dict(ts=[LO + k * 15 * MIN for k in range(4)], equity=[1.0, 1.2, 0.9, 1.1],
                              btc_mark=100.0))
    run = funding_run([0.0], marks=marks)
    led = acc.account(run)
    assert V.max_drawdown(run, led) == pytest.approx(0.25)
    assert not V.drawdown_killed(run)
    killed = funding_run([0.0], decisions=pd.DataFrame(dict(t=[LO, LO + D // 2], killed=[False, True])))
    assert V.drawdown_killed(killed)
    kf = replace(run, fills=pd.DataFrame(dict(reason=["decision", "kill"])))
    assert V.drawdown_killed(kf)


def test_p8_classifier_ece():
    p = np.linspace(0.05, 0.95, 10)
    y = np.array([0, 0, 1, 0, 1, 0, 1, 1, 1, 1], float)
    d = np.arange(10)  # one row per date: weight 0.1 each, one row per bin
    assert V.ece(p, y, d) == pytest.approx(0.1 * np.abs(y - p).sum())
    # 20 rows, 2 per bin, all on one date (weights 1/20): |sum w (y - p)| per bin
    p2 = np.repeat(p, 2)
    y2 = np.tile([0.0, 1.0], 10)
    assert V.ece(p2, y2, np.zeros(20)) == pytest.approx(sum(abs(0.05 * (1 - 2 * q)) for q in p))
    with pytest.raises(ValueError):
        V.ece(p[:5], y[:5], d[:5])
    with pytest.raises(ValueError):
        V.ece(p, y * 2, d)


# --- holdout end to end ---------------------------------------------------------------------------------
NO_K8 = V.K8Rates(0.0, 0.0)


def _nets(mu, seed, n=N):
    return np.random.default_rng(seed).normal(mu, 0.002, n)


def _alt(mu, amp, n=N):
    """Deterministic mu ± amp alternating (mean mu, std amp)."""
    return [mu + (amp if i % 2 == 0 else -amp) for i in range(n)]


def _ic_data(seed=0, n_dates=40, signal=1.0):
    rng = np.random.default_rng(seed)
    dates = np.repeat(np.arange(n_dates) * D + LO, 10)
    y = rng.normal(0, 1, len(dates))
    return (signal * y + rng.normal(0, 1, len(dates)), y, dates)


def _leak_late(t):
    """A holdout decision record with an input known at t (> t - 1 min): an L1 finding."""
    return lf.clean_records(int(t) + MIN)


def _leak(runs, records=lf.clean_records) -> dict:
    """The passed pre-opening LeakageReport (ML* = A2 at 72 h) and a holdout-phase report of ``runs``."""
    pre = lf.passed_report("A2", 72)
    return dict(leakage=pre, holdout_leakage=lf.holdout_report(pre, runs, records))


def _inputs(ml_mu=0.002, ml_m_mu=None, a0_mu=0.0, leak_records=lf.clean_records, **kw):
    ml_m_mu = ml_mu if ml_m_mu is None else ml_m_mu
    _, y, dts = _ic_data(1)
    base = dict(
        ml_T=funding_run(_nets(ml_mu, 1), arm="A2", h=72),
        a0_T=funding_run(_nets(a0_mu, 2), arm="A0", h=72),
        ml_M=funding_run(_nets(ml_m_mu, 3), arm="A2", h=72, execution="E_M"),
        a0_M=funding_run(_nets(a0_mu, 4), arm="A0", h=72, execution="E_M"),
        quarter_edges=EDGES, ic=_ic_data(), calib=(y, y, dts),  # slope exactly 1
        dev_mean_daily_net=0.001, dev_kill=(), integrity=(), k8=NO_K8, B=2000, seed=7,
    )
    base.update(kw)
    if "leakage" not in kw:
        base.update(_leak([base[k] for k in ("ml_T", "a0_T", "ml_M", "a0_M")], leak_records))
    return V.HoldoutInputs(**base)


def _ml(nets, **kw):
    return funding_run(nets, arm="A2", h=72, **kw)


def test_holdout_inconclusive_when_only_p4_fails():
    v = V.holdout(_inputs())
    assert v.criteria["P1"] and not v.criteria["P4"]
    assert all(v.criteria[p] for p in V.P_IDS if p != "P4"), v.criteria
    assert v.cls == V.INCONCLUSIVE and v.applicable == (V.INCONCLUSIVE,)
    led = acc.account(_inputs().ml_T)
    r, x = stats.paired(led, acc.account(_inputs().a0_T))
    assert v.details["p1"] == stats.cbb_test(r, x, (14, 28), 2000, 7)
    assert v.details["p8_slope"] == pytest.approx(1.0)
    assert v.details["sharpe"] == stats.sharpe(r)
    assert v.details["mean_daily_net"] == acc.mean_daily_net(led)
    # each required / secondary run's engine integrity notes are copied into the details
    inp = _inputs()
    inp = replace(inp, ml_T=replace(inp.ml_T, integrity=("funding_missing:X",)),
                  a0_M=replace(inp.a0_M, integrity=("mark_missing:Y",)))
    v = V.holdout(inp)
    assert v.details["run_integrity"] == {"ML* E_T": ("funding_missing:X",), "A0 E_T": (),
                                          "ML* E_M": (), "A0 E_M": ("mark_missing:Y",)}


def _fails(v):
    return {p for p in V.P_IDS if not v.criteria[p]}


def _flip(p):
    """With every other criterion holding, this criterion alone turns ADVANCE into NO-GO."""
    return V.classify(invalid=False, leakage=False, dev_kill=False, criteria={**ALLP, p: False}, p1_em=True)


def test_holdout_negative_wiring_p2_p3_p5_p9_p11():
    # P2: C_exec / F_net = 0.62 > 0.60 (fees on hedge round trips), everything else as before
    v = V.holdout(_inputs(ml_T=_ml(_alt(0.002, 0.0005), costs=[0.00124] * N)))
    assert _fails(v) == {"P2", "P4"} and v.details["C_exec_over_F_net"] == pytest.approx(0.62, rel=1e-6)
    assert _flip("P2") == V.NOGO
    v = V.holdout(_inputs(ml_T=_ml(_alt(0.002, 0.0005), costs=[0.00118] * N)))  # ratio 0.59 holds
    assert _fails(v) == {"P4"}
    # P3: a 20 % drawdown of the 15-minute marked equity; and a drawdown kill alone
    base = _inputs()
    m = base.ml_T.marks.copy()
    m.loc[100, "equity"] = 0.8
    v = V.holdout(replace(base, ml_T=replace(base.ml_T, marks=m)))
    assert _fails(v) == {"P3", "P4"} and v.details["mdd"] == pytest.approx(0.2)
    killed = pd.DataFrame(dict(t=[LO, LO + D], killed=[False, True]))
    v = V.holdout(replace(base, ml_T=replace(base.ml_T, decisions=killed)))
    assert _fails(v) == {"P3", "P4"} and v.details["killed"]
    # DK-113 / m9: a decision log without `killed` is an integrity failure (a class), never an exception
    v = V.holdout(replace(base, ml_T=replace(base.ml_T, decisions=pd.DataFrame(dict(t=[LO])))))
    assert v.cls == V.INVALID
    assert v.reasons == ("ML* E_T: integrity: decisions table without the 'killed' column",)
    assert _flip("P3") == V.NOGO
    # P5: all net in the first quarter
    v = V.holdout(_inputs(ml_T=_ml(_alt(0.004, 0.0005, 30) + _alt(0.0007, 0.0005, 90))))
    q = v.details["p5"]["quarters"]
    assert q[0] > 0.6 * v.details["p5"]["NET"] and all(x > 0 for x in q)
    assert _fails(v) == {"P5", "P4"}
    assert _flip("P5") == V.NOGO
    # P9: Sharpe < 1 (the mean is then too small for P1 as well)
    v = V.holdout(_inputs(ml_T=_ml(_alt(0.0001, 0.002))))
    assert "P9" in _fails(v) and v.details["sharpe"].value < 1.0
    assert v.details["sharpe"].value == pytest.approx(stats.sharpe(stats.ledger_returns(
        acc.account(_ml(_alt(0.0001, 0.002))))).value)
    assert _flip("P9") == V.NOGO
    # P11: CAC <= 0 (costs = funding; with no clusters NET_trim = CAC, so P2 fails with it)
    v = V.holdout(_inputs(ml_T=_ml(_alt(0.002, 0.0005), costs=_alt(0.002, 0.0005))))
    assert {"P11", "P2"} <= _fails(v)
    assert _flip("P11") == V.NOGO


def test_holdout_p1_execution_classes():
    v = V.holdout(_inputs(ml_mu=0.0, ml_m_mu=0.002))
    assert not v.criteria["P1"] and v.p1_em and v.cls == V.MAKER
    v = V.holdout(_inputs(ml_mu=0.0, ml_m_mu=0.0))
    assert not v.criteria["P1"] and v.p1_em is False and v.cls == V.NO_EDGE
    # P1 needs BOTH legs of the IUT: ML* strongly positive but not better than A0
    v = V.holdout(_inputs(ml_mu=0.002, a0_mu=0.002))
    assert v.details["p1"].p_abs < 0.05 and v.details["p1"].p_diff >= 0.05 and not v.criteria["P1"]
    v = V.holdout(_inputs(leak_records=_leak_late))
    assert v.cls == V.LEAKAGE and v.reasons[0].startswith("leakage: L1: decision ")
    assert "max_known_at" in v.reasons[0]
    v = V.holdout(_inputs(dev_mean_daily_net=0.0))
    assert not v.criteria["P10"] and v.cls == V.INCONCLUSIVE  # P4 still fails -> INCONCLUSIVE outranks NO-GO
    v = V.holdout(_inputs(ic=_ic_data(signal=0.0)))
    assert not v.criteria["P7"]
    _, y, dts = _ic_data(1)
    v = V.holdout(_inputs(calib=(y, 3.0 * y, dts)))
    assert not v.criteria["P8"] and v.details["p8_slope"] == pytest.approx(3.0)
    # P6 on the stressed ledger's mean daily net
    inp = _inputs(ml_mu=-0.001)
    v = V.holdout(inp)
    assert not v.criteria["P6"]
    assert v.details["stressed_mean_daily_net"] == acc.mean_daily_net(acc.stressed(inp.ml_T))


def _bad_funding_role(run):
    f = run.funding_flows.copy()
    f.loc[0, "role"] = "bogus"  # account() raises on it
    return replace(run, funding_flows=f)


def test_em_runs_are_secondary():
    good = _inputs()
    for field_ in ("ml_M", "a0_M"):
        for spoil in (lambda r: replace(r, invalid=INVALID_TERMINAL), _bad_funding_role,
                      lambda r: replace(r, invalid=INVALID_BTC, invalid_detail="BTCUSDT#0@1: BTC price "
                                        "required for hedge construction missing at 1"),  # AMB-21 (b)
                      lambda r: replace(r, invalid=INVALID_EXIT_GAP, invalid_detail="AAAUSDT#0@2: gate exit "
                                        "due at 2: no 1-minute bar within 60 min"),  # round-2 DK-69
                      lambda r: replace(r, invalid=INVALID_MARK,  # round-2 DK-70
                                        invalid_detail="AAAUSDT#0@3: no mark")):
            # P1(E_T) holds: the class is unaffected, P1(E_M) unavailable, no flag
            v = V.holdout(replace(good, **{field_: spoil(getattr(good, field_))}))
            assert v.cls == V.INCONCLUSIVE and v.p1_em is None and not v.maker_split_undecidable
            assert v.details["p1_em_unavailable"]
            if "hedge construction" in str(spoil(getattr(good, field_)).invalid_detail):
                assert "hedge construction missing at 1" in v.details["p1_em_unavailable"]  # detail reported
            sp = spoil(getattr(good, field_))
            if getattr(sp, "invalid", None) in (INVALID_EXIT_GAP, INVALID_MARK):  # reason + symbol#inst@ts
                assert sp.invalid in v.details["p1_em_unavailable"]
                assert sp.invalid_detail.split(":")[0] in v.details["p1_em_unavailable"]
            # P1(E_T) fails, E_M unavailable: NO-GO with the explicit flag (never MAKER / NO_EDGE / ADVANCE);
            # it outranks the INCONCLUSIVE of the failing P4 (both candidate classes would)
            weak = _inputs(ml_mu=0.0, ml_m_mu=0.002)
            v = V.holdout(replace(weak, **{field_: spoil(getattr(weak, field_))}))
            assert v.cls == V.NOGO and v.maker_split_undecidable and v.p1_em is None
            assert any(s.startswith("maker_split_undecidable") for s in v.reasons)
    # an E_M run on other days is not comparable: unavailable as well
    v = V.holdout(replace(good, a0_M=funding_run(_nets(0.0, 4), arm="A0", h=72, execution="E_M", lo=LO + D)))
    assert v.p1_em is None and v.cls == V.INCONCLUSIVE
    # classify: P1 fails and P1(E_M) unavailable -> NO-GO, never ADVANCE
    for p4 in (True, False):
        crit = {**ALLP, "P1": False, "P4": p4}
        assert V.classify(invalid=False, leakage=False, dev_kill=False, criteria=crit, p1_em=None) == V.NOGO
    assert V.classify(invalid=False, leakage=True, dev_kill=False, criteria={**ALLP, "P1": False},
                      p1_em=None) == V.LEAKAGE
    assert V.classify(invalid=False, leakage=False, dev_kill=True, criteria={**ALLP, "P1": False},
                      p1_em=None) == V.NO_EDGE


def test_holdout_invalid_required_arm():
    good = _inputs()
    leaky = _inputs(leak_records=_leak_late).holdout_leakage
    for field_ in ("ml_T", "a0_T"):
        # INVALID_BTC: AMB-21 (b); INVALID_EXIT_GAP / INVALID_MARK: round-2 overrides 2 / 3 (DK-69, DK-70)
        for why in (INVALID_TERMINAL, INVALID_NO_EXIT, INVALID_BTC, INVALID_EXIT_GAP, INVALID_MARK):
            run = replace(getattr(good, field_), invalid=why)
            v = V.holdout(replace(good, **{field_: run}, holdout_leakage=leaky))
            assert v.cls == V.INVALID and any(why in s for s in v.reasons)
            assert v.applicable[:2] == (V.INVALID, V.LEAKAGE) and v.criteria == {}
            assert set(v.details["run_integrity"]) == {"ML* E_T", "A0 E_T", "ML* E_M", "A0 E_M"}
    # EC-11: A0 alone invalid on W_HOLD is INVALID RUN (no forward-style relaxation)
    v = V.holdout(replace(good, a0_T=replace(good.a0_T, invalid=INVALID_TERMINAL)))
    assert v.cls == V.INVALID and v.reasons[0] == f"A0 E_T: {INVALID_TERMINAL}"
    # integrity findings and K10 / daily-identity failures
    v = V.holdout(replace(good, integrity=("guard: refused endpoint",)))
    assert v.cls == V.INVALID and v.reasons == ("integrity: guard: refused endpoint",)
    d = good.ml_T.daily.copy()
    d.loc[3, "price"] = 1e-6
    v = V.holdout(replace(good, ml_T=replace(good.ml_T, daily=d)))
    assert v.cls == V.INVALID and v.reasons == (f"ML* E_T: {V.INTEGRITY_K10}",)
    # roles are checked
    with pytest.raises(ValueError):
        V.holdout(replace(good, a0_T=good.ml_T))
    with pytest.raises(ValueError):
        V.holdout(replace(good, ml_M=good.ml_T))
    with pytest.raises(ValueError):
        V.holdout(replace(good, a0_T=funding_run(_nets(0.0, 2), arm="A0", h=24)))


def test_k8_and_required_findings_inputs():
    good = _inputs()
    assert V.holdout(replace(good, k8=V.K8Rates(0.005, 0.005))).cls == V.INCONCLUSIVE  # 0.5 % is allowed
    v = V.holdout(replace(good, k8=V.K8Rates(0.0051, 0.0)))
    assert v.cls == V.INVALID and v.reasons[0].startswith("K8: invalid labels")
    v = V.holdout(replace(good, k8=V.K8Rates(0.0, 0.006)))
    assert v.cls == V.INVALID and v.reasons[0].startswith("K8: missing funding")
    for bad in (V.K8Rates(float("nan"), 0.0), V.K8Rates(-0.1, 0.0), V.K8Rates(0.0, 1.5), None):
        with pytest.raises(ValueError):
            V.holdout(replace(good, k8=bad))
    # findings are required (no defaults); None / a bare string is refused, an empty tuple means "checked"
    with pytest.raises(TypeError):
        V.HoldoutInputs(good.ml_T, good.a0_T, good.ml_M, good.a0_M, EDGES, good.ic, good.calib, 0.001)
    for name in ("leakage", "holdout_leakage", "integrity", "dev_kill"):
        for bad in (None, "L1"):
            with pytest.raises(ValueError):
                V.holdout(replace(good, **{name: bad}))
    # an S-10.7 kill never opens the holdout
    with pytest.raises(ValueError, match="never opened"):
        V.holdout(replace(good, dev_kill=("CAC <= 0",)))


def test_holdout_misaligned_days_refused():
    good = _inputs()
    with pytest.raises(ValueError, match="aligned"):
        V.holdout(replace(good, a0_T=funding_run(_nets(0.0, 2), arm="A0", h=72, lo=LO + D)))


def test_secondary_family_holm_and_a1_claim():
    strong = funding_run(_nets(0.003, 11), arm="A1", h=72)
    a0 = funding_run(_nets(0.0, 12), arm="A0", h=72)
    weak = [funding_run(_nets(0.0, 20 + i), arm="A2", h=72) for i in range(12)]
    pairs = {name: (weak[2 * i], weak[2 * i + 1]) for i, name in enumerate(V.SECONDARY)}
    pairs["A1 vs A0"] = (strong, a0)
    lo_bin, hi_bin = [1.0, 1.0] + [0.0] * 8, [1.0] * 8 + [0.0] * 2  # each 10-row bin calibrated
    good_cal = (np.repeat([0.2, 0.8], 50), np.array(lo_bin * 5 + hi_bin * 5), np.arange(100))
    bad_cal = (np.full(100, 0.9), np.zeros(100), np.arange(100))
    sec, a1 = V.secondary_family(pairs, good_cal, 2000, 7)
    p_a1 = sec["A1 vs A0"]["p"]
    assert p_a1 == pytest.approx(1 / 2001)
    raw = {n: sec[n]["p"] for n in V.SECONDARY}
    assert {n: sec[n]["p_holm"] for n in V.SECONDARY} == stats.holm(raw)
    assert sec["A1 vs A0"]["p_holm"] == pytest.approx(6 / 2001)
    assert sec["P8-classifier"]["ece"] == pytest.approx(0.0) and a1
    # P8-classifier fails -> no A1 claim; missing -> no claim
    assert not V.secondary_family(pairs, bad_cal, 2000, 7)[1]
    assert not V.secondary_family(pairs, None, 2000, 7)[1]
    # an invalid / non-accountable / misaligned secondary arm voids only its own comparison; it stays in the
    # Holm family (m = 6); an exception while accounting it never aborts the family
    p2 = dict(pairs)
    p2["ML* Binance vs Kraken"] = (weak[0], replace(weak[1], invalid=INVALID_TERMINAL))
    p2["ML* E_M vs E_T"] = None
    p2["non-selected ML family vs ML*"] = (_bad_funding_role(weak[2]), weak[3])
    shifted = funding_run(_nets(0.0, 5), arm="A0", h=24, lo=LO + D)
    p2["ML* at non-selected horizon 1 vs A0"] = (weak[4], shifted)
    sec2, a1b = V.secondary_family(p2, good_cal, 2000, 7)
    for name in ("ML* Binance vs Kraken", "ML* E_M vs E_T", "non-selected ML family vs ML*",
                 "ML* at non-selected horizon 1 vs A0"):
        assert not sec2[name]["available"] and sec2[name]["p_holm"] is None and sec2[name]["why"]
    assert sec2["non-selected ML family vs ML*"]["why"].startswith("not accountable: ValueError")
    assert sec2["ML* at non-selected horizon 1 vs A0"]["why"].startswith("not comparable")
    assert sec2["A1 vs A0"]["p_holm"] == pytest.approx(6 / 2001) and a1b
    # A1 pair invalid -> unavailable, no claim
    p3 = {**pairs, "A1 vs A0": (replace(strong, invalid=INVALID_NO_EXIT), a0)}
    sec3, a1c = V.secondary_family(p3, good_cal, 2000, 7)
    assert not sec3["A1 vs A0"]["available"] and not a1c
    # exactly the family of 6
    with pytest.raises(ValueError):
        V.secondary_family({k: v for k, v in pairs.items() if k != "A1 vs A0"}, good_cal, 2000, 7)
    # the secondary family never changes the class
    base = _inputs()
    v1 = V.holdout(base)
    v2 = V.holdout(replace(base, secondary=p2, a1_calib=good_cal))
    assert v1.cls == v2.cls and v1.criteria == v2.criteria and v2.a1_better is True


# --- leakage reports (SC-02, AMB-33) ------------------------------------------------------------------------
def test_holdout_requires_a_passed_pre_opening_report():
    good = _inputs()
    assert V.holdout(good).cls != V.LEAKAGE and good.leakage.passed
    # a free tuple / a forged or altered report is refused
    for bad in ((), ("L1",), replace(good.leakage, commit="c" * 40)):
        with pytest.raises(ValueError, match="run_leakage_checks"):
            V.holdout(replace(good, leakage=bad))
    # a real report with checks not executed is not passed: the holdout is never opened
    skipped = lf.run_checks("A2", 72, samples={}, fold_models={}, final_model=None)
    assert lf.leakage.verify_report(skipped) and not skipped.passed
    with pytest.raises(ValueError, match="did not pass"):
        V.holdout(replace(good, leakage=skipped))
    # a report for another ML*
    a3 = dict(ml_T=replace(good.ml_T, arm="A3"), ml_M=replace(good.ml_M, arm="A3"))
    with pytest.raises(ValueError, match=r"ML\*"):
        V.holdout(replace(good, **a3))
    # the holdout-phase report must be issued by run_holdout_checks and unaltered
    for bad in ((), replace(good.holdout_leakage, findings=())
                if good.holdout_leakage.findings else replace(good.holdout_leakage, runs=())):
        with pytest.raises(ValueError, match="run_holdout_checks"):
            V.holdout(replace(good, holdout_leakage=bad))


def test_holdout_phase_findings_give_no_go_leakage():
    def raising(t):
        raise LeakageError(f"decision {t}: an input known at {t} > t - 1 min")

    for rec in (_leak_late, raising):
        x = _inputs(leak_records=rec)
        assert x.holdout_leakage.findings and x.leakage.passed
        v = V.holdout(x)
        assert v.cls == V.LEAKAGE and V.LEAKAGE in v.applicable
        n = len(x.holdout_leakage.findings)
        assert all(r.startswith("leakage: L1: decision ") for r in v.reasons[:n])
