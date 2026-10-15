"""Task 22b: round-2 small decisions, code bugs and must-fix minors (docs/results/v7-impl/
CLARIFICATIONS-2026-10-15-round2.md sections C / D; DECISION-DOCKET DK-17, DK-39, DK-53, DK-106, DK-108,
DK-110 to DK-114; red-team r1 m1-m3, m8-m10 and the deferred-minor triage). Synthetic inputs only."""

from __future__ import annotations

import dataclasses
import math
from dataclasses import replace
from types import MappingProxyType

import numpy as np
import pandas as pd
import pytest

from luna.v7 import accounting, runrecord
from luna.v7 import selection as sel
from luna.v7 import verdict as V
from luna.v7.book.engine import BookRun, run
from luna.v7.constants import FROZEN
from luna.v7.data.instances import Instance, current_instance
from luna.v7.features import _funding_feats
from luna.v7.timeutil import MIN, D, H, ns_array, to_ns
from tests.v7 import leak_fixtures as lf
from tests.v7.book.engine_fixtures import LO as BLO
from tests.v7.book.engine_fixtures import base_inputs
from tests.v7.run_fixtures import funding_run

# ------------------------------------------------------------- DK-17: F2 / F4 pairs break at a dropped record


def _fund(r8):
    t0 = to_ns("2024-03-01")
    ts = t0 + np.arange(len(r8), dtype=np.int64) * 8 * H
    return pd.DataFrame(dict(funding_ts=ts, r8=np.asarray(r8, dtype=np.float64))), int(ts[-1]) + 2 * MIN


def test_f2_f4_pairs_break_at_an_invalid_record():
    seq = [1.0, -2.0, 3.0, np.nan, -1.0, 2.0, -3.0, 0.5]
    r8 = np.array(seq) * 1e-4
    fund, t = _fund(r8)
    v = _funding_feats(fund, t)
    x = r8[np.isfinite(r8)]
    pairs = [(r8[i], r8[i + 1]) for i in range(len(r8) - 1) if np.isfinite(r8[i]) and np.isfinite(r8[i + 1])]
    assert len(pairs) == len(r8) - 3  # the NaN breaks two pairs and forms none
    a, b = np.array(pairs).T
    assert v["f2_autocorr1_30d"] == pytest.approx(np.corrcoef(a, b)[0, 1])
    assert v["f4_sign_reversals_30d"] == pytest.approx(np.sum(a * b < 0) / len(x))  # 5 / 7
    # discriminating: the bridged (old) pairs (3, -1) would add one reversal and change the autocorrelation
    assert v["f4_sign_reversals_30d"] != pytest.approx(np.sum(x[1:] * x[:-1] < 0) / len(x))
    assert v["f2_autocorr1_30d"] != pytest.approx(np.corrcoef(x[:-1], x[1:])[0, 1])
    # an invalid record between two valid ones gives no pair: only (1, -2) here, so F2 has no fit, F4 = 1 / 2
    fund2, t2 = _fund(np.array([1.0, -2.0, np.nan, 3.0]) * 1e-4)
    v2 = _funding_feats(fund2, t2)
    assert math.isnan(v2["f2_autocorr1_30d"])
    assert v2["f4_sign_reversals_30d"] == pytest.approx(1 / 3)
    fund3, t3 = _fund(np.array([1.0, np.nan, -2.0]) * 1e-4)
    assert _funding_feats(fund3, t3)["f4_sign_reversals_30d"] == 0.0  # no pair at all, windows unchanged
    # the windows are unchanged: F1 means still use every valid record
    assert v["fl30"] == pytest.approx(x.mean())


# ---------------------------------------------------- DK-108: the instance switch at the first bar's known_at


def test_instance_becomes_current_at_first_bar_known_at():
    f0 = to_ns("2024-01-01")
    old = Instance(0, f0, f0 + 10 * D, f0 + 17 * D)
    new = Instance(1, f0 + 20 * D, None, None)
    insts = [old, new]
    # known_at = first_bar_ts + 15 min must be <= t - 1 min: current from first_bar_ts + 16 min on
    assert current_instance(insts, new.first_bar_ts) is None
    assert current_instance(insts, new.first_bar_ts + 16 * MIN - 1) is None
    assert current_instance(insts, new.first_bar_ts + 16 * MIN).idx == 1
    assert current_instance([old], f0 + 16 * MIN - 1) is None
    assert current_instance([old], f0 + 16 * MIN).idx == 0
    assert current_instance(insts, old.end_known_at - 1).idx == 0
    assert current_instance(insts, old.end_known_at) is None


# ------------------------------------------------------------- DK-106 / m1 and DK-110 / m2: ns unit guards


def test_ns_array_window_checks_integers():
    with pytest.raises(ValueError, match="window"):
        ns_array([1_700_000_000_000], "known_at")  # ms by mistake
    for bad in ([0], [5], [np.iinfo(np.int64).min], [to_ns("2100-01-01")], [to_ns("2016-12-31")]):
        with pytest.raises(ValueError, match="known_at"):
            ns_array(np.array(bad, dtype=np.int64), "known_at")
    with pytest.raises(ValueError, match="window"):
        ns_array(np.array([1_700_000_000_000], dtype=np.uint64), "x")
    with pytest.raises(ValueError, match="window"):
        ns_array(pd.Series([1_700_000_000_000], dtype="Int64"), "x")
    ok = np.array([to_ns("2017-01-01"), to_ns("2099-12-31")], dtype=np.int64)
    assert ns_array(ok, "x").tolist() == ok.tolist()


def test_to_ns_refuses_floats():
    for bad in (1.6e12, 1.6e18, float("nan"), np.float64(1.7e18)):
        with pytest.raises(ValueError, match="float"):
            to_ns(bad)
    assert to_ns(np.int64(1_600_000_000_000_000_000)) == 1_600_000_000_000_000_000


# -------------------------------------------------------------------------- DK-111 / m3: frozen constants


def _walk(obj, path="FROZEN"):
    if dataclasses.is_dataclass(obj):
        assert obj.__dataclass_params__.frozen, path
        for f in dataclasses.fields(obj):
            yield from _walk(getattr(obj, f.name), f"{path}.{f.name}")
    elif isinstance(obj, MappingProxyType):
        for k, v in obj.items():
            yield from _walk(v, f"{path}[{k!r}]")
    elif isinstance(obj, tuple):
        for i, v in enumerate(obj):
            yield from _walk(v, f"{path}[{i}]")
    else:
        yield path, obj


def test_frozen_constants_are_immutable():
    leaves = dict(_walk(FROZEN))
    assert leaves and all(isinstance(v, int | float | str | bool) for v in leaves.values()), \
        {k: type(v) for k, v in leaves.items() if not isinstance(v, int | float | str | bool)}
    for m, key in ((FROZEN.accept.p6, "spread"), (FROZEN.accept.p4, "quarters"), (FROZEN.accept.p5, "top1"),
                   (FROZEN.forward.p4, "clusters"), (FROZEN.search.lgb_fixed, "max_trees")):
        before = m[key]
        with pytest.raises(TypeError):
            m[key] = 1.0
        with pytest.raises(TypeError):
            m["new"] = 1.0
        with pytest.raises((TypeError, AttributeError)):
            m.pop(key)  # type: ignore[attr-defined]
        assert m[key] == before
    with pytest.raises(dataclasses.FrozenInstanceError):
        FROZEN.accept.p6 = {}  # type: ignore[misc]
    # callers that copy, unpack or iterate still work
    assert dict(FROZEN.accept.p6) == dict(fee=1.5, slip=1.5, spread=3.0)
    assert {**FROZEN.forward.p4} == dict(active_days=30, closed_legs=50, clusters=10)
    assert sorted(FROZEN.search.lgb_fixed) == sorted(dict(FROZEN.search.lgb_fixed).keys())


# ------------------------------------------------------------- DK-112 / m8: accounting errors -> INVALID RUN


def _unaccountable(r: BookRun) -> BookRun:
    return replace(r, daily=r.daily.iloc[:0])  # account() raises ValueError ("no daily table")


def _sel_runs():
    return {(a, h): funding_run(np.random.default_rng(i).normal(0.0, 0.004, 30), arm=a, h=h)
            for i, (a, h) in enumerate(sel.REQUIRED)}


def test_selection_accounting_error_is_invalid_k10():
    runs = _sel_runs()
    with pytest.raises(ValueError):
        accounting.account(_unaccountable(runs[("A3", 72)]))
    runs[("A3", 72)] = _unaccountable(runs[("A3", 72)])
    out = sel.choose(runs, B=100)  # never raised
    assert isinstance(out, sel.Invalid) and out.reason == sel.INVALID_K10
    assert len(out.runs) == 1 and out.runs[0].startswith("A3@72h: ") and "daily table" in out.runs[0]
    # a reference (secondary) run is never accounted here: it voids nothing
    ref = _unaccountable(funding_run([0.0] * 30, arm="TS"))
    assert isinstance(sel.choose({**_sel_runs(), ("TS", 24): ref}, B=100), sel.Choice)


def _hold_inputs(**kw):
    n = 120
    edges = tuple(int(x) for x in np.linspace(funding_run([0.0]).window[0], funding_run([0.0]).window[0]
                                              + n * D, 5).astype(np.int64))
    rng = np.random.default_rng(0)
    dates = np.repeat(np.arange(40) * D + edges[0], 10)
    y = rng.normal(0, 1, len(dates))
    base = dict(
        ml_T=funding_run(rng.normal(0.002, 0.002, n), arm="A2", h=72),
        a0_T=funding_run(rng.normal(0.0, 0.002, n), arm="A0", h=72),
        ml_M=funding_run(rng.normal(0.002, 0.002, n), arm="A2", h=72, execution="E_M"),
        a0_M=funding_run(rng.normal(0.0, 0.002, n), arm="A0", h=72, execution="E_M"),
        quarter_edges=edges, ic=(y + rng.normal(0, 1, len(y)), y, dates), calib=(y, y, dates),
        dev_mean_daily_net=0.001, dev_kill=(), integrity=(), k8=V.K8Rates(0.0, 0.0), B=200,
        seed=7,
    )
    pre = lf.passed_report("A2", 72)  # the holdout-phase report of exactly these runs (verdict: pure, M-1)
    base.update(kw)
    base.update(leakage=pre, holdout_leakage=lf.holdout_report(
        pre, [base[k] for k in ("ml_T", "a0_T", "ml_M", "a0_M")]))
    return V.HoldoutInputs(**base)


def _swap(x, **runs):
    """``replace(x, **runs)`` with the holdout-phase report rebuilt over the new runs (it must cover exactly
    the four runs given, Task 24 M-1)."""
    y = replace(x, **runs)
    return replace(y, holdout_leakage=lf.holdout_report(y.leakage, [y.ml_T, y.a0_T, y.ml_M, y.a0_M]))


def test_verdict_accounting_error_is_invalid_k10():
    good = _hold_inputs()
    assert V.holdout(good).cls != V.INVALID
    for field_ in ("ml_T", "a0_T"):
        v = V.holdout(_swap(good, **{field_: _unaccountable(getattr(good, field_))}))  # never raised
        assert v.cls == V.INVALID
        name = "ML* E_T" if field_ == "ml_T" else "A0 E_T"
        assert len(v.reasons) == 1 and v.reasons[0].startswith(f"{name}: {V.INTEGRITY_K10} (")
        assert "daily table" in v.reasons[0]
    # a secondary (E_M) run voids only its own comparison
    v = V.holdout(_swap(good, ml_M=_unaccountable(good.ml_M)))
    assert v.cls != V.INVALID and v.p1_em is None and v.details["p1_em_unavailable"].startswith("ML* E_M")


# ----------------------------------------------------------- DK-113 / m9: schema frames, holdout never raises


def test_empty_engine_frames_carry_the_schema_columns():
    r = run("A0", base_inputs(), (BLO + 15 * MIN, BLO + 75 * MIN), 24)  # no decision in the window
    assert len(r.decisions) == 0 and len(r.fills) == 0 and len(r.positions) == 0 and len(r.funding_flows) == 0
    assert {"t", "equity", "stale", "killed", "paused", "orders", "notes", "post"} <= set(r.decisions.columns)
    assert {"ts", "symbol", "instance", "direction", "qty", "ref_mid", "price", "fee", "reason",
            "position_id", "role", "side", "q_after", "entry_decision_ts", "btc_ref"} <= set(r.fills.columns)
    assert {"ts", "symbol", "instance", "position_id", "role", "qty", "mark",
            "notional"} <= set(r.positions.columns)
    assert {"ts", "position_id", "role", "qty", "rate", "mark", "cash"} <= set(r.funding_flows.columns)
    assert V.drawdown_killed(r) is False  # no raise on an empty decision log
    accounting.account(r)


def test_holdout_returns_a_class_for_a_malformed_decision_log():
    good = _hold_inputs()
    bad = replace(good.ml_T, decisions=pd.DataFrame(dict(t=[good.ml_T.window[0]])))
    v = V.holdout(_swap(good, ml_T=bad))
    assert v.cls == V.INVALID and any("killed" in s for s in v.reasons)


# ------------------------------------------------------------------- DK-114 / m10: the DT4 research flag


def _rec(runs, **kw):
    base = dict(commit="a" * 40, dirty=False, config={"h": 24}, data_manifests={"bars15": "b" * 64},
                seeds=runrecord.default_seeds(), uv_lock_sha256="c" * 64,
                libraries={"python": "3.12"}, convergence=(), runs=runs)
    return runrecord.run_record(**{**base, **kw})


def test_research_runs_always_record_as_research():
    plain = funding_run([0.0] * 3)
    research = replace(plain, research=True)
    assert plain.research is False
    assert _rec([plain])["research"] is False
    assert _rec([plain], dirty=True)["code_dirty"] is True  # a non-research record may be dirty (recorded)
    rec = _rec([research], research=False)  # the caller's flag cannot unset it
    assert rec["research"] is True and rec["runs"][0]["research"] is True
    with pytest.raises(ValueError, match="clean working tree"):
        _rec([plain, research], dirty=True)
    with pytest.raises(ValueError, match="clean working tree"):
        _rec([plain], dirty=True, research=True)


# ------------------------------------------------------------------- DK-53: non-neutral post-decision books


def test_non_neutral_decisions_are_counted():
    r = funding_run([0.0] * 3)
    assert r.non_neutral_decisions == 0  # no notes column
    t0 = r.window[0]  # real ns decision times (the holdout L1 re-check reads them)
    d = pd.DataFrame(dict(t=[t0, t0 + H, t0 + 2 * H], killed=[False] * 3,
                          notes=[("risk_gross", "non_neutral_post"), (), ("cap_gross_entry",)]))
    assert replace(r, decisions=d).non_neutral_decisions == 1
    e = run("A0", base_inputs(), (BLO, BLO + 2 * D), 24)
    assert e.non_neutral_decisions == sum("non_neutral_post" in n for n in e.decisions["notes"])
    v = V.holdout(_hold_inputs(ml_T=replace(_hold_inputs().ml_T, decisions=d)))
    assert v.details["non_neutral_decisions"] == 1


# ------------------------------------------- round 1 (review): accounting input validation -> ValueError only


def _with_fills():
    return funding_run([0.001, 0.0005, -0.0002], costs=[0.0001, 0.0001, 0.0])  # hedge round-trip fills


@pytest.mark.parametrize("col", ["start_ts", "equity_end", "price", "equity_start", "funding", "net"])
def test_missing_daily_column_is_a_value_error_and_invalid_k10(col):
    r = _with_fills()
    accounting.account(r)
    bad = replace(r, daily=r.daily.drop(columns=col))
    with pytest.raises(ValueError, match=col):
        accounting.account(bad)
    runs = {**_sel_runs(), ("A0", 24): replace(bad, arm="A0", h_star=24)}
    out = sel.choose(runs, B=100)
    assert isinstance(out, sel.Invalid) and out.reason == sel.INVALID_K10 and col in out.runs[0]


@pytest.mark.parametrize("col", ["btc_mark", "ts"])
def test_missing_marks_column_is_a_value_error(col):
    r = _with_fills()
    with pytest.raises(ValueError, match=col):
        accounting.account(replace(r, marks=r.marks.drop(columns=col)))
    g = _hold_inputs()
    v = V.holdout(_swap(g, a0_T=replace(g.a0_T, marks=g.a0_T.marks.drop(columns=col))))
    assert v.cls == V.INVALID and v.reasons[0].startswith(f"A0 E_T: {V.INTEGRITY_K10} (")


@pytest.mark.parametrize("value", [None, float("nan"), 1.5, "x", True])
def test_bad_fill_instance_is_a_value_error(value):
    r = _with_fills()
    f = r.fills.astype({"instance": object})
    f.loc[0, "instance"] = value
    with pytest.raises(ValueError, match="instance"):
        accounting.account(replace(r, fills=f))


# --------------------------------------------------------- round 1 (review): remaining ns guards


def test_to_ns_window_checks_integers():
    for bad in (1_700_000_000_000_000, -1_700_000_000_000_000_000, to_ns("2100-01-01") + 0, 2**62):
        with pytest.raises(ValueError):
            to_ns(int(bad))
    assert to_ns(1_700_000_000_000_000_000) == 1_700_000_000_000_000_000


def test_ns_array_refuses_narrow_floats():
    t = float(to_ns("2024-06-01"))
    with pytest.raises(ValueError, match="float"):  # float32 ULP at 1.7e18 is ~1.4e11 ns (minutes)
        ns_array(np.array([t], dtype=np.float32), "known_at")
    with pytest.raises(ValueError, match="float"):  # float16 cannot even hold the magnitude
        ns_array(np.array([1.0], dtype=np.float16), "known_at")
    assert ns_array(np.array([t], dtype=np.float64), "known_at").tolist() == [int(t)]


def test_instances_and_intervals_validate_timestamps():
    from luna.v7.data.instances import build_instances
    from luna.v7.data.intervals import annotate

    bars = pd.DataFrame(dict(open_ts=np.array([1_700_000_000_000, 1_700_000_900_000]), vol_base=[1.0, 1.0]))
    with pytest.raises(ValueError, match="open_ts"):
        build_instances(bars, to_ns("2024-01-01"))  # ms values: refused, never read as 1970 ns
    with pytest.raises(ValueError, match="open_ts"):
        build_instances(bars.assign(open_ts=[float(to_ns("2024-01-01")), np.nan]), to_ns("2024-01-02"))
    f = pd.DataFrame(dict(funding_ts=np.array([1_700_000_000_000]), rate=[1e-4],
                          interval_h_explicit=pd.array([8], dtype="Int64")))
    with pytest.raises(ValueError, match="funding_ts"):
        annotate(f, [Instance(0, to_ns("2023-01-01"), None, None)])


def test_deploy_layer_runs_are_research_runs():
    from luna.v7.deploy import run_layer
    from tests.v7.test_deploy import filters, spec

    r = run_layer(spec(), 265, filters(btc_min_notional=5.0))
    assert r.research is True and r.layer == "L-265"
