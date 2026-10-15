"""Task 26b (SC-01 part 2) budget at representative synthetic scale (the larger runs are
``scripts/v7/bench_decision_pass.py --symbols 40 / 120``, run by the controller): 10 symbols x 100 days
written to a raw store (R3, R4, R2 and R5 72 h notice windows); the outcome-free ``runner.decision_pass`` from
the store (production chunk-cache budget, §9 features) runs in a child process of the benchmark script, so
its PEAK WORKING SET (``GetProcessMemoryInfo``) is the pass's own; a second child replays every view (another
book run) and fails if any view is recomputed. Asserts the per-(symbol, decision) time and the peak working
set under stated bounds."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from luna.v7 import k9, rawstore
from tests.v7 import perf_fixtures as pf

N_SYM, N_DAYS, DEC_DAYS = 10, 100, 10
MS_PER_SYMBOL_DECISION = 40.0  # bound (measured about 9-13 ms on this host; margin for a loaded machine)
PEAK_RSS_MB = 900  # the pass process: interpreter + libraries + one chunk of 10 symbols + caches
SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "v7" / "bench_decision_pass.py"


def _bench():
    spec = importlib.util.spec_from_file_location("bench_decision_pass", SCRIPT)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.mark.v7_scale
def test_decision_pass_budget(tmp_path):
    with k9.using(k9.K9Log(tmp_path / "k9")):
        pf.scale_store(rawstore.RawStore(tmp_path / "raw"), N_SYM, N_DAYS)
    b = _bench()
    args = ["--symbols", str(N_SYM), "--days", str(N_DAYS), "--decision-days", str(DEC_DAYS)]
    run = b.run_phase("pass", tmp_path, args)
    assert run["computed"] == run["decisions"] > 20
    per = 1000 * run["s"] / run["decisions"] / N_SYM
    print(f"\ndecision pass: {run['decisions']} decisions x {N_SYM} symbols, {per:.1f} ms per (symbol, "
          f"decision), peak working set {run['peak_rss_mb']} MB")
    assert per < MS_PER_SYMBOL_DECISION
    assert run["peak_rss_mb"] < PEAK_RSS_MB
    rep = b.run_phase("replay", tmp_path, args)  # raises when any view is recomputed
    assert rep["s"] / run["decisions"] < 0.5
    assert b.peak_rss_bytes() > 0
