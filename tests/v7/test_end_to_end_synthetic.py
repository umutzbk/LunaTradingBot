"""End-to-end synthetic determinism (Tasks 19 / 19b): the whole V7 chain on a seeded synthetic universe,
twice, wired through the research runner (``luna.v7.runner`` / ``runner_select``).

ingestion normalisation (klines.normalise / union, funding.normalise_* / merge) -> instances (DEC-35, one
relisted symbol) -> interval annotation (EC-9, one 8 h -> 4 h regime transition) ->
``runner.DecisionProvider`` (gates U1-U8, h-hat, beta-hat, qv24, staleness, §9 features) ->
``runner.label_panel`` (leg labels of a sample of decisions) -> ``runner.fit_fold_models`` (tiny
monkeypatched search space; one compressed synthetic fold) -> ``runner.FoldScoreModel`` -> executed books of
A0 and A2 under E_T and E_M (engine, L-norm) -> §8 accounting -> K10 / daily identity -> S-10.7 kill
triggers and the S-13.1 paired bootstrap -> verdict stage (``runner_select.holdout_inputs`` with a real
synthetic opening per pass (``auth_fixtures.authorize``: frozen repo, injected home, pre-opening record of a
DevSelection with these books issued through the documented test hook and the real passed LeakageReport of
the ``leak_fixtures`` fold models, whose final model then scores the holdout panel, I-2) on a W_DEV window,
``verdict.holdout``; if the S-10.7 kill fired, ``verdict.classify`` with it). Both passes must give
byte-identical outputs (sha256 of every key frame, the decision provenance logs and the Verdict). Everything
is dated 2022 - early 2023 on the real calendar, inside W_DEV: the guards (labels, engine) run unmodified
against the canonical M6 / W_HOLD spans.

Runtime is kept modest by sampling decisions (every ``SAMPLE_EVERY``-th sample day) and a short book window;
nothing is skipped.
"""

from __future__ import annotations

import hashlib
import time

import numpy as np
import pandas as pd
import pytest

from luna.v7 import accounting, guard, models, runner, selection, stats, universe, verdict, windows
from luna.v7.constants import FROZEN
from luna.v7.runner_select import holdout_inputs, holdout_verdict
from luna.v7.runrecord import run_output_sha256
from luna.v7.runstore import RunStore, encode_run, run_file_sha256
from luna.v7.timeutil import D
from tests.v7 import auth_fixtures as af
from tests.v7 import fixtures_v7 as fx
from tests.v7 import leak_fixtures as lf

SEED = 7
SAMPLE_EVERY = 7  # every 7th day of the 4-day sample grid -> 28-day spacing, 11 sample decisions
B_SMALL = 200  # bootstrap draws (the frozen B = 10,000 is exercised in test_stats)


def _sha(obj: object) -> str:
    """sha256 of a frame's exact values (numeric columns by their bytes; object columns via CSV) or a repr."""
    if not isinstance(obj, pd.DataFrame):
        return hashlib.sha256(repr(obj).encode()).hexdigest()
    h = hashlib.sha256(repr([(str(c), str(t)) for c, t in obj.dtypes.items()]).encode())
    try:
        h.update(pd.util.hash_pandas_object(obj, index=False).to_numpy().tobytes())
    except TypeError:  # unhashable cells (tuples in the decision log)
        h.update(obj.to_csv(index=False, lineterminator="\n").encode())
    return h.hexdigest()


def run_pipeline(seed: int, base, monkeypatch) -> tuple[dict[str, str], dict]:
    """One full pass (``base``: a fresh directory for this pass's synthetic opening). Returns (sha256 per
    output, facts for the non-triviality assertions)."""
    u = fx.build_universe(seed)
    prov = runner.DecisionProvider(u, with_features=True)
    panel = runner.label_panel(u, prov, fx.sample_times()[::SAMPLE_EVERY], fx.H_STAR, windows.W_DEV, fx.NOW)
    samples = panel.samples
    [fm] = runner.fit_fold_models(samples, [fx.FOLD], fx.H_STAR)
    model = runner.FoldScoreModel([fm], "A2", fx.H_STAR)
    runs = {"A0": runner.run_book("A0", u, prov, fx.BOOK, fx.H_STAR, fx.NOW),
            "A2": runner.run_book("A2", u, prov, fx.BOOK, fx.H_STAR, fx.NOW, model=model)}
    em = {"A0": runner.run_book("A0", u, prov, fx.BOOK, fx.H_STAR, fx.NOW, execution="E_M"),
          "A2": runner.run_book("A2", u, prov, fx.BOOK, fx.H_STAR, fx.NOW, model=model, execution="E_M")}
    leds = {k: accounting.account(r) for k, r in runs.items()}
    edges = list(range(fx.BOOK[0], fx.BOOK[1] + 1, D))  # daily "folds" / "quarters" of the short book window
    kill = selection.dev_kill(leds["A2"], accounting.period_net(leds["A2"], [edges[0], edges[-1]]))
    r_ml, r_a0 = stats.paired(leds["A2"], leds["A0"])
    cbb = stats.cbb_test(r_ml, r_ml - r_a0, ells=(2, 3), B=B_SMALL)
    logs = {"A0": runner.decision_log(runs["A0"], prov), "A2": runner.decision_log(runs["A2"], prov, model)}
    # verdict stage (synthetic): the "holdout" is the W_DEV book window, its labels and the fold model; the
    # S-10.7 result passed is empty so the P1-P11 machinery runs; the real kill is applied through classify
    hold = runner.label_panel(u, prov, fx.decisions_in(fx.BOOK), fx.H_STAR, fx.BOOK, fx.NOW)
    dev = af.dev_selection("A2", fx.H_STAR, window=fx.BOOK, em_runs={"ML*": em["A2"], "A0": em["A0"]},
                           runs={("A2", fx.H_STAR): runs["A2"], ("A0", fx.H_STAR): runs["A0"]})
    opened = af.authorize(base, monkeypatch, dev=dev)
    # Task 26a: the four books persist canonically (parquet + JSON, no pickle) and round-trip exactly; the
    # opening reads the W_DEV ML* run back through the verified pin (RunStore.load_pinned)
    store = RunStore(base / "runs")
    for r in (*runs.values(), *em.values()):
        sha = store.put(r)
        back = store.load(sha, file_sha256=run_file_sha256(r))
        assert run_output_sha256(back) == sha and encode_run(back) == encode_run(r)
    x = holdout_inputs(auth=opened.auth, ml_T=runs["A2"], a0_T=runs["A0"],
                       ml_M=em["A2"], a0_M=em["A0"],
                       quarter_edges=edges, panel=hold, final_model=lf.final(fx.H_STAR, "en"),
                       dev_run_store=store, dev_kill=(), leakage_report=opened.auth.leakage_report,
                       records=prov.record, integrity=(), B=B_SMALL)
    v = holdout_verdict(x, auth=opened.auth)  # writes the verdict marker
    v_kill = None
    if kill:
        v_kill = verdict.classify(invalid=v.cls == verdict.INVALID, leakage=False, dev_kill=True,
                                  criteria=v.criteria, p1_em=v.p1_em)

    out: dict[str, str] = {}
    for s in fx.SYMBOLS:
        out[f"bars15/{s}"] = _sha(u.inputs[s].bars15)
        out[f"bars1m/{s}"] = _sha(u.inputs[s].bars1m)
        out[f"funding_annot/{s}"] = _sha(u.funding_annot[s])
        out[f"instances/{s}"] = _sha(u.inputs[s].insts)
    out["samples"] = _sha(samples)
    out["invalid_labels"] = _sha(panel.invalid_by_reason)
    out["fold/selection"] = _sha({k: (s_.cfg, s_.best_iter, s_.ic) for k, s_ in fm.selection.items()})
    out["fold/test_pred"] = _sha(fm.test_pred)
    out["fold/convergence"] = _sha(runner.fold_convergence([fm]))
    out["fold/model_hash"] = _sha(model.hashes)
    for k, r in {**runs, **{f"{k}_EM": r for k, r in em.items()}}.items():
        for part in ("fills", "marks", "positions", "funding_flows", "daily", "decisions"):
            out[f"{k}/{part}"] = _sha(getattr(r, part))
        out[f"{k}/integrity"] = _sha((r.invalid, r.integrity))
    for k, led in leds.items():
        out[f"{k}/ledger"] = _sha((led.NET, led.F_net, led.C_exec, led.PR, led.MKT, led.CAC, led.n_clusters,
                                    accounting.k10_ok(led), accounting.daily_ok(led)))
    for k, lg in logs.items():
        out[f"{k}/decision_log"] = _sha(lg)
    out["kill"] = _sha(kill)
    out["cbb"] = _sha((cbb.mean_r, cbb.mean_x, cbb.p_abs_by_ell, cbb.p_diff_by_ell, cbb.p_P))
    out["holdout_panel"] = _sha((hold.samples, hold.invalid_by_reason, x.k8))
    out["holdout_leakage"] = _sha((x.holdout_leakage.sha256, x.holdout_leakage.findings))
    out["verdict"] = _sha((v.cls, v.reasons, v.applicable, v.criteria, v.p1_em, v.maker_split_undecidable,
                           v.a1_better, v_kill))
    facts = dict(u=u, samples=samples, panel=panel, fm=fm, runs=runs, em=em, leds=leds, kill=kill, cbb=cbb,
                 logs=logs, verdict=v, verdict_kill=v_kill, hold=hold, holdout_leakage=x.holdout_leakage)
    return out, facts


@pytest.fixture
def tiny_space(monkeypatch):
    monkeypatch.setattr(models, "search_space", lambda h: list(fx.TINY_SPACE))


def test_end_to_end_synthetic(tiny_space, tmp_path, monkeypatch):
    t0 = time.perf_counter()
    universe._PF_CACHE.clear()  # independent passes: no memo carried between them
    a, facts = run_pipeline(SEED, tmp_path / "pass1", monkeypatch)
    t1 = time.perf_counter()
    universe._PF_CACHE.clear()
    b, _ = run_pipeline(SEED, tmp_path / "pass2", monkeypatch)
    t2 = time.perf_counter()
    diff = sorted(k for k in a if a[k] != b[k])
    assert a.keys() == b.keys() and not diff, f"non-deterministic outputs: {diff}"

    u = facts["u"]
    insts = {s: u.inputs[s].insts for s in fx.SYMBOLS}
    funding = {s: u.inputs[s].funding for s in fx.SYMBOLS}
    # ingestion / instances / intervals exercised the special cases
    assert len(insts[fx.RELIST]) == 2 and all(len(insts[s]) == 1 for s in fx.SYMBOLS if s != fx.RELIST)
    i0, i1 = insts[fx.RELIST]
    assert i0.last_trade_ts <= fx.DORMANT[0] and i1.first_bar_ts >= fx.DORMANT[1]
    fa = u.funding_annot[fx.TRANS]
    post = fa[fa["funding_ts"] > fx.TRANSITION_TS]
    assert fa["interval_transition"].sum() >= 1 and not post["invalid"].any()
    assert (post["nominal_h"].astype(float) == 4.0).all()
    assert (funding[fx.REST_FILL[0]]["source"] == "rest").sum() == 3
    # labels, models, books, accounting
    samples = facts["samples"]
    assert len(samples) > 200 and samples["symbol"].nunique() >= 8
    assert set(samples["symbol"]) >= {fx.RELIST, fx.TRANS}
    assert facts["fm"].test_pred is not None and len(facts["fm"].test_pred) > 0
    for k, r in facts["runs"].items():
        assert r.valid, (k, r.invalid, r.invalid_detail)
        assert len(r.fills) > 0, f"{k} never traded (the test would not exercise execution)"
        led = facts["leds"][k]
        assert accounting.k10_ok(led) and accounting.daily_ok(led)
        assert abs(led.NET - led.equity_change) <= 1e-9
    assert np.isfinite(facts["cbb"].p_P)
    hl = facts["holdout_leakage"]  # L1 re-checked at every decision through the real provider records
    assert hl.findings == () and dict(hl.checks)["L1"] > 0
    # E_M runs (secondary) are valid books with maker fills
    for k, r in facts["em"].items():
        assert r.valid and r.execution == "E_M", (k, r.invalid)
        assert (r.fills["liquidity"] == "maker").any(), f"{k} E_M had no maker fill"
    # the multiplier symbol traded with unscaled quantities: qty * price is the leg notional (<= 0.5 E / K)
    cap = FROZEN.book.side_gross / FROZEN.book.K
    pepe = pd.concat([r.fills[r.fills["symbol"] == fx.PEPE] for r in facts["runs"].values()])
    assert len(pepe) > 0, "the multiplier symbol was never traded"
    assert ((pepe["qty"] * pepe["ref_mid"]).abs() <= cap * 1.05).all()
    # provenance: every A2 decision used fold 0's model; every input was known by t - 1 min
    lg = facts["logs"]["A2"]
    assert (lg["fold_id"] == fx.FOLD.fold_id).all() and lg["model_hash"].nunique() == 1
    assert (lg["max_known_at"] <= lg["t"] - 60 * 10**9).all()
    # verdict stage: P1-P11 evaluated; with the S-10.7 kill the class is NO-GO: no viable edge
    v = facts["verdict"]
    assert v.cls in verdict.CLASSES
    assert v.cls == verdict.INVALID or set(v.criteria) == set(verdict.P_IDS)
    if facts["kill"]:
        assert facts["verdict_kill"] in (verdict.NO_EDGE, verdict.INVALID)
    # the guards ran on the real calendar: the windows are inside W_DEV, a W_HOLD window is refused
    for lo, hi in (fx.BOOK, (int(samples["decision_ts"].min()), int(samples["label_known_at"].max()))):
        assert windows.W_DEV[0] <= lo < hi <= windows.W_DEV[1]
        guard.check_outcome_window(lo, hi, now_ns=fx.NOW)
    with pytest.raises(guard.HoldoutBlocked):
        guard.check_outcome_window(windows.W_HOLD[0], windows.W_HOLD[0] + D, now_ns=fx.NOW)
    n0, n2 = len(facts["runs"]["A0"].fills), len(facts["runs"]["A2"].fills)
    print(f"\ne2e pass 1: {t1 - t0:.1f} s, pass 2: {t2 - t1:.1f} s, {len(a)} hashed outputs, "
          f"{len(samples)} samples, fills A0 {n0} / A2 {n2}, verdict {v.cls!r}, kill {facts['kill']}")
