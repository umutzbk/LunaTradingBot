"""Task 26b (SC-01 part 2) budget at representative synthetic scale (the larger run is
``scripts/v7/bench_decision_pass.py``, run by the controller): 10 symbols x 100 days written to a raw store
(R3, R4, R2 and R5 72 h notice windows), the outcome-free ``runner.decision_pass`` from the store with the §9
features. Asserts the per-(symbol, decision) compute time and the peak traced memory per symbol under stated
bounds, and that a replay (another book run) reads every view back without recomputing it."""

from __future__ import annotations

import time
import tracemalloc

import pytest

from luna.v7 import k9, rawstore, runner, windows
from luna.v7.timeutil import D
from luna.v7.viewstore import ViewStore, view_key
from tests.v7 import perf_fixtures as pf

N_SYM, N_DAYS = 10, 100
MS_PER_SYMBOL_DECISION = 40.0  # bound (measured about 9 ms on this host; margin for a loaded machine)
PEAK_MB_PER_SYMBOL = 6.0  # traced peak of the pass per symbol (one chunk's universe, store cache budget 8 MB)


@pytest.mark.v7_scale
def test_decision_pass_budget(tmp_path):
    with k9.using(k9.K9Log(tmp_path / "k9")):
        st = rawstore.RawStore(tmp_path / "raw")
        meta = pf.scale_store(st, N_SYM, N_DAYS, notice_every=4)
        end = pf.START + N_DAYS * D
        ts = [int(t) for t in windows.decision_times(end - 10 * D, end - D)]
        key = view_key(data_identity=st.manifest_sha256s(), symbols=list(meta), hedge="BTCUSDT",
                       with_features=True, code_id="budget")
        src = rawstore.StoreSource(st, meta, budget_bytes=8 * 2**20)
        vs = ViewStore(tmp_path / "views", key, with_features=True)
        t0 = time.perf_counter()
        out = runner.decision_pass(src, ts, vs, history_lo=pf.START)
        dt = time.perf_counter() - t0
        assert out["computed"] == len(ts) and vs.stats["writes"] == len(ts)
        per = 1000 * dt / len(ts) / N_SYM
        print(f"\ndecision pass: {len(ts)} decisions x {N_SYM} symbols, {per:.1f} ms per (symbol, decision)")
        assert per < MS_PER_SYMBOL_DECISION
        # memory: a fresh store key, traced
        key2 = view_key(data_identity="traced", symbols=list(meta), hedge="BTCUSDT", with_features=True,
                        code_id="budget")
        src2 = rawstore.StoreSource(st, meta, budget_bytes=8 * 2**20)
        tracemalloc.start()
        try:
            base = tracemalloc.get_traced_memory()[0]
            runner.decision_pass(src2, ts[:6], ViewStore(tmp_path / "views", key2, with_features=True),
                                 history_lo=pf.START)
            peak = (tracemalloc.get_traced_memory()[1] - base) / 2**20
        finally:
            tracemalloc.stop()
        print(f"peak traced {peak:.1f} MB ({peak / N_SYM:.2f} MB per symbol)")
        assert peak / N_SYM < PEAK_MB_PER_SYMBOL
        # replay: every view read back, nothing recomputed
        u = runner.ResearchUniverse.for_decisions(src, ts[-1], ts[-1] + 1, history_lo=pf.START)
        p = runner.DecisionProvider(u, True, 1, store=ViewStore(tmp_path / "views", key, with_features=True))
        p._xs = None  # type: ignore[assignment]  # any recomputation would fail
        for t in ts:
            assert len(p(t).gates) == N_SYM - 1
        assert p.store.stats["hits"] == len(ts)  # type: ignore[union-attr]
