"""Task 29a (red-team r2 MJ1) benchmark: the OUTCOME side of W_DEV (label panels, book runs, an L9a canary
fold) at representative scale on SYNTHETIC data written to a temporary raw store (never var/, no network).
Run by the controller at two sizes and fit the slope (free RAM permitting), e.g.:

    uv run python scripts/v7/bench_outcome_side.py --symbols 20 --days 60
    uv run python scripts/v7/bench_outcome_side.py --symbols 60 --days 60

``--days`` is the evaluated window with DENSE 1-minute bars for every symbol (about 0.5 % missing minutes,
2 % volume-0 bars); it follows ``--warmup-days`` (default 65) of 15m / R4 / funding history only, so the
U1 60-day instance age passes and decisions have candidates. Funding means alternate +10 / -6 bps per 8 h so
A0 trades. One child process per phase, so each peak working set is the phase's own (Windows
``GetProcessMemoryInfo`` PeakWorkingSetSize via ctypes; elsewhere ``resource.getrusage`` max RSS), every
reader with the PRODUCTION budgets (``rawstore.DEFAULT_CACHE_BYTES`` = 512 MiB shared, the lazy R5 cache
``lazy1m.LAZY_1M_CACHE_BYTES`` = 256 MiB, ``runner.LABEL_BLOCK_NS`` = 30 d, ``lazy1m.SCAN_STEP_NS`` = 7 d):

1. ``write``: the store (R3 15m, R4 mark / index / premium, R2 funding over warm-up + window; R5 1m over the
   window; every 10th symbol a delisting notice);
2. ``pass``: ``runner.decision_pass`` of every decision of the window into a ``viewstore.ViewStore`` (the
   decision side, already budgeted by Task 26b; here it only feeds the next phases);
3. ``labels``: ``runner.label_panel`` at h = 24 / 72 / 168 over every decision of the window, from a lazy
   ``ResearchUniverse.from_source`` (``load_outcome_inputs``), views replayed from the store;
4. ``book_et`` / ``book_em``: one ``runner.run_book`` A0 (h* = 168) under E_T / E_M over the window (E_M:
   sparse synthetic aggTrades in the metadata, one per 5 minutes);
5. ``canary``: one L9a canary fold (``leakage.l9a``: the label record permuted within dates and the real
   ``cv.run_fold`` with the PRODUCTION search space, both regressor families) on a SYNTHETIC panel of
   ``--symbols`` x 2 sides at every decision of the first outer fold's span (training cost only).

Prints JSON: per phase time, counts and peak working set, unit costs and the extrapolation to W_DEV
(``--real-*``: 500 symbols, about 4,900 decisions from D0 2021-01 to 2025-06, gated candidates per decision,
3 horizons, 11 required + 2 E_M runs, L9 = 2 pipelines x 3 horizons x the outer folds). Peaks: fit
peak = a + b x symbols x days from the controller's two runs.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from bench_decision_pass import peak_rss_bytes  # noqa: E402

PHASES = ("write", "pass", "labels", "book_et", "book_em", "canary")


def _canary_panel(h: int, n_sym: int, hi: int, seed: int = 0):
    """Synthetic samples (``leak_fixtures`` style): ``n_sym`` symbols x 2 sides at every decision from D0 to
    ``hi``, a signal spread over four features and an AR(1) label part per (symbol, side)."""
    import numpy as np
    import pandas as pd

    from luna.v7.features import FEATURES
    from luna.v7.timeutil import MIN, H
    from luna.v7.windows import D0, decision_times

    rng = np.random.default_rng([seed, h])
    dec = decision_times(D0, hi)
    k = 2 * n_sym
    ts = np.repeat(dec, k)
    n = len(ts)
    df = pd.DataFrame(rng.normal(size=(n, len(FEATURES))), columns=list(FEATURES))
    df["side"] = np.tile(np.repeat([1.0, -1.0], n_sym), len(dec))
    df["decision_ts"] = ts
    df["symbol"] = np.tile([f"S{i:03d}" for i in range(n_sym)] * 2, len(dec))
    df["h"] = h
    df["label_known_at"] = ts + h * H + MIN
    z = sum(df[c] for c in ("f2_same_sign_share_30d", "f5_premium_last", "f9_rvol_24h", "f10_ret_24h"))
    e = np.zeros((len(dec), k))
    e[0] = rng.normal(scale=10.0, size=k)
    for i in range(1, len(dec)):
        e[i] = 0.9 * e[i - 1] + rng.normal(scale=10.0 * np.sqrt(1 - 0.81), size=k)
    df["y_reg"] = 1.5 * z + e.reshape(-1) + rng.normal(scale=4, size=n)
    df["y_pos"] = (df["y_reg"] > 0).astype(float)
    return df


def _frames_bytes(u) -> int:
    """Bytes of the universe's resident frames (15m R3, R4 x 3, funding + its annotation, the halt-window 1m
    rows, aggTrades): what stays linear in symbols x days once the 1-minute bars are lazy."""
    n = 0
    for s, x in u.inputs.items():
        for df in (x.bars15, x.bars1m, x.funding, x.r4.premium, x.r4.mark, x.r4.index, x.aggtrades,
                   u.funding_annot[s]):
            if df is not None:
                n += int(df.memory_usage(index=True, deep=True).sum())
    return n


def _phase(a: argparse.Namespace) -> dict:
    from luna.v7 import k9, rawstore, runner, windows
    from luna.v7.timeutil import D
    from luna.v7.viewstore import ViewStore
    from tests.v7 import perf_fixtures as pf

    work = Path(a.work)
    start = pf.START
    w0 = start + a.warmup_days * D
    end = w0 + a.days * D
    out: dict = {}
    with k9.using(k9.K9Log(work / f"k9_{a.phase}")):
        if a.phase == "canary":
            from luna.v7 import leakage, stats
            from luna.v7.windows import D0, outer_folds

            fold = outer_folds(D0)[0]
            samples = _canary_panel(24, a.symbols, fold.test_hi)
            t0 = time.perf_counter()
            res = leakage.l9a(samples, 24, [fold], stats.SEED)
            out.update(s=round(time.perf_counter() - t0, 1), rows=len(samples), results=len(res))
            return out
        st = rawstore.RawStore(work / "raw")
        t0 = time.perf_counter()
        meta = pf.scale_store(st, a.symbols, a.warmup_days + a.days, start=start, write=a.phase == "write",
                              dense_1m=(w0, end), aggtrades=a.phase == "book_em", carry=True)
        if a.phase == "write":
            out["s"] = round(time.perf_counter() - t0, 1)
            sms = [st.symbol_manifest("R5", s) for s in meta]
            out["r5_rows"] = sum(int(e["rows"]) for sm in sms if sm for e in sm["chunks"].values())
            return out
        src = rawstore.StoreSource(st, meta)  # production budgets (DEFAULT_CACHE_BYTES, LAZY_1M_CACHE_BYTES)
        ts = [int(t) for t in windows.decision_times(w0, end)]
        vs = ViewStore(work / "views")
        t0 = time.perf_counter()
        if a.phase == "pass":
            res = runner.decision_pass(src, ts, vs, history_lo=start)
            out.update(decisions=len(ts), computed=res["computed"])
            out["s"] = round(time.perf_counter() - t0, 1)
            return out
        u = runner.ResearchUniverse.from_source(src, start, end)
        p = runner.DecisionProvider(u, True, 64, store=vs)
        out["universe_s"] = round(time.perf_counter() - t0, 1)
        out["universe_frames_mb"] = round(_frames_bytes(u) / 2**20, 1)
        out["universe_frames_kb_per_symbol_day"] = round(_frames_bytes(u) / 1024 / a.symbols
                                                         / (a.warmup_days + a.days), 2)
        t0 = time.perf_counter()
        if a.phase == "labels":
            n = {}
            for h in (24, 72, 168):
                panel = runner.label_panel(u, p, ts, h, (start, end), pf.NOW)
                n[h] = dict(labels=panel.n_labels, invalid=panel.n_invalid, no_label=panel.n_no_label,
                            samples=len(panel.samples))
                del panel
            out["panels"] = n
            out["labels"] = sum(v["labels"] + v["no_label"] for v in n.values())
        else:
            ex = "E_T" if a.phase == "book_et" else "E_M"
            run = runner.run_book("A0", u, p, (w0, end), 168, pf.NOW, execution=ex)
            out.update(execution=ex, decisions=len(run.decisions), fills=len(run.fills), valid=run.valid,
                       invalid=run.invalid)
        out["s"] = round(time.perf_counter() - t0, 1)
        out["n_computed_views"] = p.n_computed
        out["r5_cache"] = dict(src.r5_lazy.cache.stats, bytes=src.r5_lazy.cache.bytes,
                               decoded_chunks=src.r5_lazy.stats["decoded_chunks"])
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
    ap.add_argument("--symbols", type=int, default=20)
    ap.add_argument("--days", type=int, default=60, help="evaluated window (dense 1-minute bars)")
    ap.add_argument("--warmup-days", type=int, default=65, help="15m / R4 / funding history before it")
    ap.add_argument("--phases", type=str, default=",".join(PHASES))
    ap.add_argument("--real-symbols", type=int, default=500)
    ap.add_argument("--real-decisions", type=int, default=4900)
    ap.add_argument("--real-cands", type=int, default=200, help="gated candidates per decision")
    ap.add_argument("--real-runs", type=int, default=13, help="9 required + 2 more + 2 E_M book runs")
    ap.add_argument("--real-folds", type=int, default=10)
    ap.add_argument("--real-days", type=int, default=1642, help="W_DEV days from D0 2021-01 to 2025-06")
    ap.add_argument("--out", type=str, default=None, help="work directory (default: a temporary one)")
    ap.add_argument("--phase", choices=PHASES, default=None, help=argparse.SUPPRESS)
    ap.add_argument("--work", type=str, default=None, help=argparse.SUPPRESS)
    a = ap.parse_args()
    if a.phase:  # child process
        res = _phase(a)
        res["peak_rss_mb"] = round(peak_rss_bytes() / 2**20)
        print(json.dumps(res))
        return
    work = Path(a.out) if a.out else Path(tempfile.mkdtemp(prefix="v7_bench_out_"))
    if "var" in work.resolve().parts:
        raise SystemExit("refusing a work directory under var/")
    args = ["--symbols", str(a.symbols), "--days", str(a.days), "--warmup-days", str(a.warmup_days)]
    out: dict = {"symbols": a.symbols, "days": a.days, "warmup_days": a.warmup_days, "work": str(work),
                 "pid": os.getpid()}
    for ph in a.phases.split(","):
        out[ph] = run_phase(ph, work, args)
    x: dict = {"real": {"symbols": a.real_symbols, "decisions": a.real_decisions, "cands": a.real_cands,
                        "horizons": 3, "book_runs": a.real_runs, "folds": a.real_folds, "days": a.real_days}}
    if "labels" in out:
        lab = out["labels"]
        per = lab["s"] / max(1, lab["labels"])
        n_real = a.real_decisions * a.real_cands * 2 * 3
        x["label_ms"] = round(1000 * per, 3)
        x["labels_real"] = n_real
        x["labels_h_single_process"] = round(per * n_real / 3600, 1)
    for ph in ("book_et", "book_em"):
        if ph in out:
            b = out[ph]
            per = b["s"] / max(1, b["decisions"]) / a.symbols  # per (decision, symbol)
            x[f"{ph}_ms_per_decision_symbol"] = round(1000 * per, 3)
            x[f"{ph}_h_per_run_linear"] = round(per * a.real_decisions * a.real_symbols / 3600, 2)
    if "book_et" in out:
        x["books_h_all_runs_linear"] = round(x["book_et_h_per_run_linear"] * a.real_runs, 1)
    if "canary" in out:
        c = out["canary"]
        real_rows = a.real_decisions * a.real_cands * 2
        x["canary_s_per_1k_rows"] = round(1000 * c["s"] / max(1, c["rows"]), 3)
        x["l9_h_linear_upper"] = round(c["s"] / c["rows"] * real_rows * a.real_folds * 2 * 3 / 3600, 1)
    if "labels" in out:
        kb = out["labels"]["universe_frames_kb_per_symbol_day"]
        x["universe_frames_kb_per_symbol_day"] = kb
        x["universe_frames_gb_real_upper"] = round(kb * a.real_symbols * a.real_days / 2**20, 1)
    x["peaks_mb"] = {ph: out[ph]["peak_rss_mb"] for ph in a.phases.split(",") if ph in out}
    out["extrapolation_w_dev"] = x
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
