"""Equity layers L-265 / L-10k and the deployment gate (spec §14, S-14.1, DEC-21, DEC-34, EC-6) on synthetic
inputs."""

from dataclasses import replace

import pandas as pd
import pytest

from luna.v7 import deploy
from luna.v7.book import engine
from luna.v7.deploy import Filters, RunSpec, d_gates, round_order, run_layer
from luna.v7.timeutil import MIN, D

from .book.engine_fixtures import BTC, END, LO, base_inputs
from .run_fixtures import funding_run

T1 = LO + 5 * MIN
TE1 = T1 + 15 * MIN
KA = LO - D  # known before the window


def pit(step, min_qty, min_notional, known_at=KA):
    return Filters(step, min_qty, min_notional, "pit_historical", known_at)


def filters(btc_min_notional=5.0, prov="pit_historical", drop=()):
    def mk(s, q, n):
        return Filters(s, q, n, prov, None if prov == "current_approximation" else KA)

    f = {"AAAUSDT": mk(0.01, 0.01, 5.0), "BBBUSDT": mk(0.01, 0.01, 5.0),
         BTC: mk(0.001, 0.001, btc_min_notional)}
    return {k: v for k, v in f.items() if k not in drop}


def spec():
    return RunSpec("A0", base_inputs(), (LO, END), 24)


def test_rounding_fail_closed():
    f = pit(0.001, 0.002, 5.0)
    assert round_order(0.0259, 1000.0, f) == 0.025  # towards zero to the step
    assert round_order(0.0251, 1000.0, f) == 0.025
    assert round_order(0.003, 5000.0, f) == 0.003  # an exact multiple survives float noise (0.003 / 0.001)
    assert round_order(0.0029999999, 5000.0, f) == pytest.approx(0.002)
    assert round_order(0.0019, 1e6, f) is None  # rounds to 0.001 < min_qty
    assert round_order(0.004, 1250.0, f) == 0.004 and round_order(0.004, 1249.0, f) is None  # 4.996 < 5 USD
    assert round_order(0.0009, 1e9, f) is None  # rounds to 0
    for bad in ((float("nan"), 1.0), (1.0, float("nan")), (1.0, 0.0), (-1.0, 1.0), (1.0, float("inf"))):
        assert round_order(*bad, f) is None
    with pytest.raises(ValueError):
        Filters(0.0, 0.0, 0.0, "pit_historical", KA)
    with pytest.raises(ValueError):
        Filters(0.1, 0.0, 0.0, "pit_historical")  # point-in-time filters need known_at
    with pytest.raises(ValueError):
        Filters(0.1, 0.0, 0.0, "guess", KA)  # type: ignore[arg-type]


def test_pit_filters_apply_only_when_known():
    s = deploy._Sizer({"X": [pit(0.1, 0.1, 0.0, known_at=LO), pit(1.0, 1.0, 0.0, known_at=LO + D)]})
    assert s.size("X", 2.55, 10.0, LO) is None and s.missing == {"X"}  # known_at > t - 60 s
    assert s.size("X", 2.55, 10.0, LO + MIN) == pytest.approx(2.5)
    assert s.size("X", 2.55, 10.0, LO + D + MIN) == 2.0  # the later snapshot replaces it
    assert s.size("Y", 1.0, 1.0, LO + D) is None and s.missing == {"X", "Y"}
    with pytest.raises(ValueError):  # current and point-in-time filters never mixed (EC-6)
        deploy._Sizer({"X": [pit(0.1, 0.1, 0.0), Filters(0.1, 0.1, 0.0, "current_approximation")]})


def test_hedge_infeasible_blocks_entries():
    ok = run_layer(spec(), 265, filters(btc_min_notional=5.0))
    assert ok.valid and ok.layer == "L-265" and ok.equity0 == 265.0
    e = ok.fills[ok.fills["ts"] == TE1].set_index("symbol")
    # 26.5 USD per leg: AAA 0.265 -> 0.26, BBB 0.53; beta_book = 26 * 2 - 26.5 = 25.5 -> BTC 0.0255 -> 0.025
    assert e.loc["AAAUSDT", "qty"] == 0.26 and e.loc["BBBUSDT", "qty"] == 0.53
    assert e.loc[BTC, "qty"] == 0.025 and e.loc[BTC, "direction"] == -1
    assert ok.decisions.set_index("t").loc[T1, "constructible"]
    # BTC min notional 100 USD: the 25 USD hedge is infeasible -> the beta rule forbids the entries
    bad = run_layer(spec(), 265, filters(btc_min_notional=100.0))
    assert bad.valid and len(bad.fills) == 0
    d = bad.decisions.set_index("t")
    assert not d.loc[T1, "constructible"] and "hedge:entries_forbidden" in d.loc[T1, "infeasible"]
    assert d.loc[T1, "n_post"] == 0
    assert "forbidden_open:AAAUSDT" in d.loc[T1, "notes"]
    # BTC filters missing: same (fail closed), flagged incomplete (DEC-34)
    nob = run_layer(spec(), 265, filters(drop=(BTC,)))
    assert len(nob.fills) == 0 and nob.filters_missing == (BTC,)


def test_missing_symbol_excluded_and_flagged():
    r = run_layer(spec(), 265, filters(drop=("BBBUSDT",)))
    assert r.valid and r.filters_missing == ("BBBUSDT",)
    # AMB-16 (d) / AMB-28 (user decision 2026-10-15): with BBB infeasible the book would be one-sided, so
    # the AAA entry is forbidden too (round 1 changed this from "AAA and the hedge enter at 00:20")
    assert len(r.fills) == 0
    d = r.decisions.set_index("t")
    assert "one_sided:entries_forbidden" in d.loc[T1, "infeasible"]
    assert "forbidden_open:AAAUSDT" in d.loc[T1, "notes"]
    assert d.loc[T1, "n_post"] == 0
    assert deploy.label(r) == deploy.INCOMPLETE and not deploy.exact_evidence(r)
    assert "leg:BBBUSDT" in r.decisions.set_index("t").loc[T1, "infeasible"]


def _lnorm(n_post):
    t = [LO + i * 8 * 3600 * 10**9 + 5 * MIN for i in range(len(n_post))]
    return replace(funding_run([1e-4] * 3), decisions=pd.DataFrame(dict(t=t, killed=False, n_post=n_post)))


def _l265(nets, cons, prov=("pit_historical",), missing=()):
    t = [LO + i * 8 * 3600 * 10**9 + 5 * MIN for i in range(len(cons))]
    r = funding_run(nets)
    return replace(r, layer="L-265", equity0=265.0, window=funding_run([1e-4] * 3).window,
                   decisions=pd.DataFrame(dict(t=t, killed=False, constructible=cons)),
                   filter_provenance=prov, filters_missing=missing)


def test_d1_share():
    ln = _lnorm([2] * 9 + [0] * 3 + [2])  # 10 decisions with a non-empty L-norm book
    cons = [True] * 9 + [False] * 3 + [False]  # the 3 empty-book decisions do not count
    g = d_gates(ln, _l265([1.0] * 3, cons))
    assert (g["d1_constructible"], g["d1_base"], g["d1_share"]) == (9, 10, 0.9) and g["D1"]  # >= 90 %
    cons2 = [True] * 8 + [False] + [True] * 3 + [False]
    g2 = d_gates(ln, _l265([1.0] * 3, cons2))
    assert g2["d1_share"] == 0.8 and not g2["D1"]
    # a missing L-265 decision is not constructible
    short = _l265([1.0] * 3, cons[:9])
    assert d_gates(ln, short)["d1_share"] == 0.9
    assert d_gates(ln, _l265([1.0] * 3, [True] * 8))["d1_share"] == 0.8
    # no non-empty L-norm decision: D1 cannot be established
    g0 = d_gates(_lnorm([0] * 13), _l265([1.0] * 3, cons))
    assert g0["d1_share"] is None and not g0["D1"]


def test_d3_infra_cost():
    cons = [True] * 13
    ln = _lnorm([1] * 13)
    hi = d_gates(ln, _l265([0.40] * 3, cons))  # 146 USD/yr > 144
    assert hi["D2"] and hi["D3"] and hi["annual_net_usd"] == pytest.approx(146.0, rel=1e-6)
    lo = d_gates(ln, _l265([0.39] * 3, cons))  # 142.35 USD/yr: positive net, below the VPS cost
    assert lo["D2"] and not lo["D3"]
    neg = d_gates(ln, _l265([-0.01] * 3, cons))
    assert not neg["D2"] and not neg["D3"]
    assert d_gates(ln, _l265([0.39] * 3, cons), infra_usd_year=100.0)["D3"]
    inv = d_gates(ln, replace(_l265([0.40] * 3, cons), invalid="INVALID RUN — x"))
    assert not (inv["D1"] or inv["D2"] or inv["D3"]) and inv["why"].startswith("L-265 run invalid")
    with pytest.raises(ValueError):  # the D gate is at 265 USD only
        d_gates(ln, replace(_l265([0.4] * 3, cons), equity0=10_000.0))


def test_label_approximate_when_current_filters():
    cons, ln = [True] * 13, _lnorm([1] * 13)
    g = d_gates(ln, _l265([0.4] * 3, cons, prov=("current_approximation", "pit_historical")))
    assert g["label"] == "approximate (current filters)" and not g["exact"]
    assert d_gates(ln, _l265([0.4] * 3, cons, prov=("pit_historical",)))["label"] == deploy.PIT_HIST
    fw = d_gates(ln, _l265([0.4] * 3, cons, prov=("forward_pit",)))
    assert fw["label"] == deploy.FORWARD and fw["exact"]
    inc = d_gates(ln, _l265([0.4] * 3, cons, prov=("forward_pit",), missing=("XUSDT",)))
    assert inc["label"] == deploy.INCOMPLETE and not inc["exact"] and inc["incomplete"]
    both = d_gates(ln, _l265([0.4] * 3, cons, prov=("current_approximation",), missing=("XUSDT",)))
    assert both["label"] == deploy.APPROX and both["incomplete"]
    # the provenance is carried from the supplied filters by run_layer
    r = run_layer(spec(), 265, filters(prov="current_approximation"))
    assert r.filter_provenance == ("current_approximation",) and deploy.label(r) == deploy.APPROX


def test_lnorm_path_rejects_filters():
    f = filters()
    with pytest.raises(TypeError):
        engine.run("A0", base_inputs(), (LO, END), 24, filters=f)  # type: ignore[call-arg]
    with pytest.raises(ValueError):
        engine.run("A0", base_inputs(), (LO, END), 24, layer="L-265")
    with pytest.raises(ValueError):
        engine._run("A0", base_inputs(), (LO, END), 24, None, "E_T", "L-norm", lots=deploy._Sizer(f))
    with pytest.raises(ValueError):
        run_layer(spec(), 1.0, f)  # E = 1 is L-norm, never a filtered layer
    with pytest.raises(ValueError):
        run_layer(replace(spec(), execution="E_M"), 265, f)
    # EC-6: current filters never become point-in-time metadata
    with pytest.raises(deploy.CurrentFiltersNotHistorical):
        deploy.assert_not_backdated({"r1": [{"AAAUSDT": filters(prov="current_approximation")["AAAUSDT"]}]})
    deploy.assert_not_backdated({"r1": [filters()["AAAUSDT"]], "x": "current_approximation"})
    # the plain L-norm run records the non-empty book for D1 and has no filter fields
    r = engine.run("A0", base_inputs(), (LO, END), 24)
    assert r.decisions.set_index("t").loc[T1, "n_post"] == 2 and "constructible" not in r.decisions
    assert r.filter_provenance == () and r.equity0 == 1.0


def test_d1_scaled_report_and_empty_base():
    ln = engine.run("A0", base_inputs(), (LO, END), 24)
    l265 = run_layer(spec(), 265, filters(btc_min_notional=100.0))
    # path-independent report: the L-norm book scaled to 265 USD (hedge 26.5 USD -> 0.026 BTC = 26 USD)
    assert deploy.d1_share_scaled(ln, filters(btc_min_notional=5.0))[0] == 1.0
    assert deploy.d1_share_scaled(ln, filters(btc_min_notional=26.5))[0] == 0.0  # 0.026 BTC = 26 USD < 26.5
    assert deploy.d1_share_scaled(ln, filters(drop=("AAAUSDT",)))[0] == 0.0  # missing filter
    g = d_gates(ln, l265, filters_by_symbol=filters(btc_min_notional=5.0))
    assert g["d1_share"] == 0.0 and g["d1_share_scaled"] == 1.0 and not g["D1"]  # the gate stays path-based
    assert "d1_share_scaled" not in d_gates(ln, l265)
    # an empty L-norm decision table: no base, D1 fails (consistent, fail closed)
    empty = replace(_lnorm([1] * 13), decisions=pd.DataFrame())
    ge = d_gates(empty, _l265([0.4] * 3, [True] * 13))
    assert ge["d1_share"] is None and not ge["D1"] and ge["D2"]


def test_l10k_layer_and_d_gates_on_real_runs():
    ln = engine.run("A0", base_inputs(), (LO, END), 24)
    big = run_layer(spec(), 10_000, filters())
    assert big.layer == "L-10k" and big.valid
    e = big.fills[big.fills["ts"] == TE1].set_index("symbol")
    assert e.loc["AAAUSDT", "qty"] == 10.0 and e.loc["BBBUSDT", "qty"] == 20.0  # 1,000 USD per leg
    with pytest.raises(ValueError):
        d_gates(ln, big)  # D1-D3 are at 265 USD only
    g = d_gates(ln, run_layer(spec(), 265, filters(btc_min_notional=100.0)))
    assert g["d1_share"] == 0.0 and not g["D1"] and g["label"] == deploy.PIT_HIST
