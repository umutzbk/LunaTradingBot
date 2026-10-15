"""Task 26b (SC-01 part 2) benchmark: the outcome-free decision pass at representative scale on SYNTHETIC
data written to a temporary raw store (never var/, no network).

    uv run python scripts/v7/bench_decision_pass.py [--symbols 40] [--days 365] [--decision-days 90]
                                                    [--every 1] [--chunk-days 30] [--trace] [--out DIR]

Builds ``--symbols`` symbols x ``--days`` days of 15m bars (R3), R4 mark / index / premium, 8 h funding (R2)
and, for every 10th symbol, a delisting notice with 72 h of 1-minute bars (R5), in a ``rawstore.RawStore``;
then runs ``runner.decision_pass`` (StoreSource, chunked decisions-only universes, ``ViewStore``, with the §9
features) over the last ``--decision-days`` days (every ``--every``-th decision) and replays the stored views
once more (a second "book run"). Prints JSON: write time, per-decision and per-(symbol, decision) compute
time, replay time per decision, peak RSS (psutil, when installed) and peak traced memory (``--trace``; slows
the run), and the extrapolation to W_DEV (500 symbols, 5,000 decisions, 11 book runs).
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import threading
import time
import tracemalloc
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from luna.v7 import k9, rawstore, runner, windows  # noqa: E402
from luna.v7.timeutil import D  # noqa: E402
from luna.v7.viewstore import ViewStore, view_key  # noqa: E402
from tests.v7 import perf_fixtures as pf  # noqa: E402


def _rss_sampler():
    try:
        import psutil
    except ImportError:
        return None, lambda: None
    proc, peak, stop = psutil.Process(), [0], threading.Event()

    def run() -> None:
        while not stop.is_set():
            peak[0] = max(peak[0], proc.memory_info().rss)
            stop.wait(0.2)

    th = threading.Thread(target=run, daemon=True)
    th.start()

    def done() -> int:
        stop.set()
        th.join()
        return peak[0]
    return proc.memory_info().rss, done


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", type=int, default=40)
    ap.add_argument("--days", type=int, default=365)
    ap.add_argument("--decision-days", type=int, default=90)
    ap.add_argument("--every", type=int, default=1)
    ap.add_argument("--chunk-days", type=int, default=30)
    ap.add_argument("--trace", action="store_true")
    ap.add_argument("--out", type=str, default=None, help="work directory (default: a temporary one)")
    a = ap.parse_args()
    work = Path(a.out) if a.out else Path(tempfile.mkdtemp(prefix="v7_bench_"))
    if "var" in work.resolve().parts:
        raise SystemExit("refusing a work directory under var/")
    start = pf.START
    out: dict = {"symbols": a.symbols, "days": a.days, "work": str(work)}
    with k9.using(k9.K9Log(work / "k9")):
        st = rawstore.RawStore(work / "raw")
        t0 = time.perf_counter()
        meta = pf.scale_store(st, a.symbols, a.days, start=start)
        out["write_s"] = round(time.perf_counter() - t0, 1)
        src = rawstore.StoreSource(st, meta, budget_bytes=64 * 2**20)
        end = start + a.days * D
        ts = [int(t) for t in windows.decision_times(end - a.decision_days * D, end - D)][::a.every]
        key = view_key(data_identity=st.manifest_sha256s(), symbols=list(meta), hedge="BTCUSDT",
                       with_features=True, code_id="bench")
        vs = ViewStore(work / "views", key, with_features=True)
        rss0, rss_done = _rss_sampler()
        if a.trace:
            tracemalloc.start()
        t0 = time.perf_counter()
        res = runner.decision_pass(src, ts, vs, history_lo=start, chunk_ns=a.chunk_days * D)
        dt = time.perf_counter() - t0
        if a.trace:
            out["peak_traced_mb"] = round(tracemalloc.get_traced_memory()[1] / 2**20, 1)
            tracemalloc.stop()
        rss_peak = rss_done()
        out.update(decisions=len(ts), chunks=res["chunks"], pass_s=round(dt, 1),
                   per_decision_s=round(dt / len(ts), 4),
                   per_symbol_decision_ms=round(1000 * dt / len(ts) / a.symbols, 3))
        if rss0 is not None:
            out["rss_start_mb"], out["rss_peak_mb"] = round(rss0 / 2**20), round(rss_peak / 2**20)
        # replay (another book run): views read back from the store
        u = runner.ResearchUniverse.for_decisions(src, ts[-1], ts[-1] + 1, history_lo=start)
        p = runner.DecisionProvider(u, True, 1, store=ViewStore(work / "views", key, with_features=True))
        t0 = time.perf_counter()
        for t in ts:
            p(t)
        out["replay_per_decision_s"] = round((time.perf_counter() - t0) / len(ts), 4)
        n_sym, n_dec, books = 500, 5000, 11
        comp_h = out["per_symbol_decision_ms"] * n_sym * n_dec / 1000 / 3600
        out["extrapolation_w_dev"] = {
            "symbols": n_sym, "decisions": n_dec, "book_runs": books,
            "decision_pass_h_single_process": round(comp_h, 1),
            "replay_h_all_books_linear_upper_bound": round(out["replay_per_decision_s"] * n_sym / a.symbols * n_dec * books / 3600, 2),
        }
        if rss0 is not None:
            out["extrapolation_w_dev"]["rss_mb_linear_in_symbols"] = round(
                rss0 / 2**20 + (rss_peak - rss0) / 2**20 * n_sym / a.symbols)
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
