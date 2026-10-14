"""End-to-end synthetic determinism (Task 19): the whole V7 chain on a seeded synthetic universe, twice.

ingestion normalisation (klines.normalise / union, funding.normalise_* / merge) -> instances (DEC-35, one
relisted symbol) -> interval annotation (EC-9, one 8 h -> 4 h regime transition) -> gates U1-U8 -> §9
features -> leg labels (a sample of decisions) -> models (tiny monkeypatched search space; one compressed
synthetic fold) -> executed books of A0 and A2 (engine, E_T, L-norm) -> §8 accounting -> K10 / daily
identity -> S-10.7 kill triggers and the S-13.1 paired bootstrap. Both passes must give byte-identical
outputs (sha256 of every key frame). Everything is dated 2022 - early 2023 on the real calendar, inside
W_DEV: the guards (labels, engine) run unmodified against the canonical M6 / W_HOLD spans.

Runtime is kept modest by sampling decisions (every ``SAMPLE_EVERY``-th sample day) and a short book window;
nothing is skipped.
"""

from __future__ import annotations

import hashlib
import time

import numpy as np
import pandas as pd
import pytest

from luna.v7 import accounting, cv, guard, models, selection, stats, windows
from luna.v7.book import engine
from luna.v7.features import FEATURES
from luna.v7.labels import LabelInputs, leg_label
from luna.v7.timeutil import D
from tests.v7 import fixtures_v7 as fx

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


def _samples(u: fx.Universe, view: fx.DecisionProvider) -> tuple[pd.DataFrame, dict[str, int]]:
    """Features + leg labels (h = 24) of every gated candidate at the sampled decisions (cv.REQUIRED rows)."""
    rows, invalid = [], {}
    for t in fx.sample_times()[::SAMPLE_EVERY]:
        t = int(t)
        for c in view(t).cands.itertuples(index=False):
            li = LabelInputs(c.symbol, u.legs[c.symbol], u.legs[fx.BTC], windows.W_DEV, fx.NOW)
            r = leg_label(li, t, int(c.side), fx.H_STAR)
            if r is None:
                continue
            if r.invalid_reason is not None:
                invalid[r.invalid_reason] = invalid.get(r.invalid_reason, 0) + 1
                continue
            d = {k: getattr(c, k) for k in FEATURES}
            d.update(decision_ts=t, symbol=c.symbol, side=int(c.side), h=fx.H_STAR,
                     label_known_at=r.label_known_at, y_reg=r.y_reg_bps, y_pos=float(r.y_pos), net=r.net)
            rows.append(d)
    return pd.DataFrame(rows), invalid


def run_pipeline(seed: int) -> tuple[dict[str, str], dict]:
    """One full pass. Returns (sha256 per output, facts for the non-triviality assertions)."""
    u = fx.build_universe(seed)
    view = fx.DecisionProvider(u, with_features=True)
    samples, invalid = _samples(u, view)
    fm = cv.run_fold(fx.FOLD, samples, fx.H_STAR)
    inputs = engine.EngineInputs({s: u.instruments[s] for s in fx.ALTS}, u.instruments[fx.BTC], view, fx.NOW)
    runs = {"A0": engine.run("A0", inputs, fx.BOOK, fx.H_STAR),
            "A2": engine.run("A2", inputs, fx.BOOK, fx.H_STAR, model=fx.FoldScorer(fm))}
    leds = {k: accounting.account(r) for k, r in runs.items()}
    edges = list(range(fx.BOOK[0], fx.BOOK[1] + 1, D))  # daily "folds" of the short book window
    kill = selection.dev_kill(leds["A2"], accounting.period_net(leds["A2"], [edges[0], edges[-1]]))
    r_ml, r_a0 = stats.paired(leds["A2"], leds["A0"])
    cbb = stats.cbb_test(r_ml, r_ml - r_a0, ells=(2, 3), B=B_SMALL)

    out: dict[str, str] = {}
    for s in fx.SYMBOLS:
        out[f"bars15/{s}"] = _sha(u.bars15[s])
        out[f"bars1m/{s}"] = _sha(u.bars1m[s])
        out[f"funding_annot/{s}"] = _sha(u.funding_annot[s])
        out[f"instances/{s}"] = _sha(u.insts[s])
    out["samples"] = _sha(samples)
    out["invalid_labels"] = _sha(sorted(invalid.items()))
    out["fold/selection"] = _sha({k: (v.cfg, v.best_iter, v.ic) for k, v in fm.selection.items()})
    out["fold/test_pred"] = _sha(fm.test_pred)
    for k, r in runs.items():
        for part in ("fills", "marks", "positions", "funding_flows", "daily", "decisions"):
            out[f"{k}/{part}"] = _sha(getattr(r, part))
        led = leds[k]
        out[f"{k}/ledger"] = _sha((led.NET, led.F_net, led.C_exec, led.PR, led.MKT, led.CAC, led.n_clusters,
                                    accounting.k10_ok(led), accounting.daily_ok(led)))
    out["kill"] = _sha(kill)
    out["cbb"] = _sha((cbb.mean_r, cbb.mean_x, cbb.p_abs_by_ell, cbb.p_diff_by_ell, cbb.p_P))
    facts = dict(u=u, samples=samples, invalid=invalid, fm=fm, runs=runs, leds=leds, kill=kill, cbb=cbb)
    return out, facts


@pytest.fixture
def tiny_space(monkeypatch):
    monkeypatch.setattr(models, "search_space", lambda h: list(fx.TINY_SPACE))


def test_end_to_end_synthetic(tiny_space):
    t0 = time.perf_counter()
    a, facts = run_pipeline(SEED)
    t1 = time.perf_counter()
    b, _ = run_pipeline(SEED)
    t2 = time.perf_counter()
    diff = sorted(k for k in a if a[k] != b[k])
    assert a.keys() == b.keys() and not diff, f"non-deterministic outputs: {diff}"

    u = facts["u"]
    # ingestion / instances / intervals exercised the special cases
    assert len(u.insts[fx.RELIST]) == 2 and all(len(u.insts[s]) == 1 for s in fx.SYMBOLS if s != fx.RELIST)
    i0, i1 = u.insts[fx.RELIST]
    assert i0.last_trade_ts <= fx.DORMANT[0] and i1.first_bar_ts >= fx.DORMANT[1]
    fa = u.funding_annot[fx.TRANS]
    post = fa[fa["funding_ts"] > fx.TRANSITION_TS]
    assert fa["interval_transition"].sum() >= 1 and not post["invalid"].any()
    assert (post["nominal_h"].astype(float) == 4.0).all()
    assert (u.funding[fx.REST_FILL[0]]["source"] == "rest").sum() == 3
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
    # the guards ran on the real calendar: the windows are inside W_DEV, a W_HOLD window is refused
    for lo, hi in (fx.BOOK, (int(samples["decision_ts"].min()), int(samples["label_known_at"].max()))):
        assert windows.W_DEV[0] <= lo < hi <= windows.W_DEV[1]
        guard.check_outcome_window(lo, hi, now_ns=fx.NOW)
    with pytest.raises(guard.HoldoutBlocked):
        guard.check_outcome_window(windows.W_HOLD[0], windows.W_HOLD[0] + D, now_ns=fx.NOW)
    n0, n2 = len(facts["runs"]["A0"].fills), len(facts["runs"]["A2"].fills)
    print(f"\ne2e pass 1: {t1 - t0:.1f} s, pass 2: {t2 - t1:.1f} s, {len(a)} hashed outputs, "
          f"{len(samples)} samples, fills A0 {n0} / A2 {n2}")
