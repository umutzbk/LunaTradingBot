"""Task 26b (SC-01 part 2) benchmark: the outcome-free decision pass at representative scale on SYNTHETIC
data written to a temporary raw store (never var/, no network). Run by the controller (free RAM permitting):

    uv run python scripts/v7/bench_decision_pass.py --symbols 40 [--days 365] [--decision-days 90]
    uv run python scripts/v7/bench_decision_pass.py --symbols 120 ...

Three child processes, so each peak is the phase's own (Windows peak working set from
``GetProcessMemoryInfo`` via ctypes, psutil is not needed; elsewhere ``resource.getrusage`` max RSS):

1. ``write``: ``--symbols`` symbols x ``--days`` days of 15m bars (R3), R4 mark / index / premium, 8 h
   funding (R2) and, for every 10th symbol, a delisting notice with 72 h of 1-minute bars (R5);
2. ``pass``: ``runner.decision_pass`` from a ``rawstore.StoreSource`` with the PRODUCTION chunk-cache budget
   (``rawstore.DEFAULT_CACHE_BYTES`` = 512 MiB) into a ``viewstore.ViewStore``, the §9 features on, over the
   last ``--decision-days`` days (every ``--every``-th decision);
3. ``replay``: a provider reading every stored view back (another book run), nothing recomputed.

Prints JSON: times, per-decision and per-(symbol, decision) cost, the peak working set of each phase and
the extrapolation to W_DEV (500 symbols, 5,000 decisions, 11 book runs). For memory at 500 symbols, run two
``--symbols`` values (e.g. 40 and 120) and extrapolate the pass peak linearly from the two points.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def peak_rss_bytes() -> int:
    """This process's peak resident set (Windows: PeakWorkingSetSize)."""
    if os.name == "nt":
        class PMC(ctypes.Structure):
            _fields_ = [("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong),
                        ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaNonPagedPoolUsage", ctypes.c_size_t), ("PagefileUsage", ctypes.c_size_t),
                        ("PeakPagefileUsage", ctypes.c_size_t)]
        c = PMC()
        c.cb = ctypes.sizeof(PMC)
        k32 = ctypes.WinDLL("kernel32")
        k32.GetCurrentProcess.restype = ctypes.c_void_p
        psapi = ctypes.WinDLL("psapi")
        psapi.GetProcessMemoryInfo.argtypes = [ctypes.c_void_p, ctypes.POINTER(PMC), ctypes.c_ulong]
        if not psapi.GetProcessMemoryInfo(k32.GetCurrentProcess(), ctypes.byref(c), c.cb):
            raise OSError("GetProcessMemoryInfo failed")
        return int(c.PeakWorkingSetSize)
    import resource

    return int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss) * 1024


def _phase(a: argparse.Namespace) -> dict:
    from luna.v7 import k9, rawstore, runner, windows
    from luna.v7.timeutil import D
    from luna.v7.viewstore import ViewStore
    from tests.v7 import perf_fixtures as pf

    work = Path(a.work)
    start = pf.START
    out: dict = {}
    with k9.using(k9.K9Log(work / f"k9_{a.phase}")):
        st = rawstore.RawStore(work / "raw")
        t0 = time.perf_counter()
        meta = pf.scale_store(st, a.symbols, a.days, start=start, write=a.phase == "write")
        if a.phase == "write":
            out["s"] = round(time.perf_counter() - t0, 1)
            return out
        src = rawstore.StoreSource(st, meta)  # production ChunkCache budget (DEFAULT_CACHE_BYTES)
        end = start + a.days * D
        ts = [int(t) for t in windows.decision_times(end - a.decision_days * D, end - D)][::a.every]
        vs = ViewStore(work / "views")
        t0 = time.perf_counter()
        if a.phase == "pass":
            res = runner.decision_pass(src, ts, vs, history_lo=start, chunk_ns=a.chunk_days * D)
            out.update(decisions=len(ts), computed=res["computed"], chunks=res["chunks"])
        else:
            u = runner.ResearchUniverse.for_decisions(src, ts[-1], ts[-1] + 1, history_lo=start)
            p = runner.DecisionProvider(u, True, 1, store=vs)
            for t in ts:
                p(t)
            if p.n_computed:
                raise RuntimeError("replay recomputed views")
        out["s"] = round(time.perf_counter() - t0, 2)
    return out


def run_phase(phase: str, work: Path, args: list[str]) -> dict:
    """One phase in a child process (its own peak working set); returns its JSON line."""
    r = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--work", str(work), *args, "--phase",
                        phase], capture_output=True, text=True, cwd=str(ROOT))
    if r.returncode:
        raise RuntimeError(f"phase {phase} failed:\n{r.stderr[-4000:]}")
    return json.loads(r.stdout.strip().splitlines()[-1])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", type=int, default=40)
    ap.add_argument("--days", type=int, default=365)
    ap.add_argument("--decision-days", type=int, default=90)
    ap.add_argument("--every", type=int, default=1)
    ap.add_argument("--chunk-days", type=int, default=30)
    ap.add_argument("--out", type=str, default=None, help="work directory (default: a temporary one)")
    ap.add_argument("--phase", choices=("write", "pass", "replay"), default=None, help=argparse.SUPPRESS)
    ap.add_argument("--work", type=str, default=None, help=argparse.SUPPRESS)
    a = ap.parse_args()
    if a.phase:  # child process
        res = _phase(a)
        res["peak_rss_mb"] = round(peak_rss_bytes() / 2**20)
        print(json.dumps(res))
        return
    work = Path(a.out) if a.out else Path(tempfile.mkdtemp(prefix="v7_bench_"))
    if "var" in work.resolve().parts:
        raise SystemExit("refusing a work directory under var/")
    args = ["--symbols", str(a.symbols), "--days", str(a.days), "--decision-days", str(a.decision_days),
            "--every", str(a.every), "--chunk-days", str(a.chunk_days)]
    out: dict = {"symbols": a.symbols, "days": a.days, "decision_days": a.decision_days, "work": str(work)}
    for ph in ("write", "pass", "replay"):
        out[ph] = run_phase(ph, work, args)
    n, s = out["pass"]["decisions"], out["pass"]["s"]
    out["per_decision_s"] = round(s / n, 4)
    out["per_symbol_decision_ms"] = round(1000 * s / n / a.symbols, 3)
    out["replay_per_decision_s"] = round(out["replay"]["s"] / n, 4)
    n_sym, n_dec, books = 500, 5000, 11
    out["extrapolation_w_dev"] = {
        "symbols": n_sym, "decisions": n_dec, "book_runs": books,
        "decision_pass_h_single_process": round(out["per_symbol_decision_ms"] * n_sym * n_dec / 3.6e6, 1),
        "replay_h_all_books_linear_upper_bound": round(out["replay_per_decision_s"] * n_sym / a.symbols
                                                       * n_dec * books / 3600, 2),
        "pass_peak_rss_mb_at_these_symbols": out["pass"]["peak_rss_mb"],
    }
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
