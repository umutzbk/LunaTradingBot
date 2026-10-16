"""Validation C6 phase A: outcome-free real-scale W_DEV dry run, ingestion (task-c6-brief.md).

Drives the FROZEN-candidate ingest (``luna.v7.ingest`` / ``luna.v7.metaingest``) over real Binance public W_DEV
data. Every request goes through ``luna.v7.fetch.Fetcher.default`` (netguard before I/O, K9 logging); nothing
at or after 2025-07-01 is fetched or stored (the guards enforce it; this script never bypasses them). No
labels, models, IC, P&L or book runs.

Layout (everything under ``var/v7/dryrun``)::

    store/                      the RawStore (R3, R4_*, R2_archive, R2_rest, R5)
    meta/                       the MetaStore (Wayback captures, R11 / R1 / R12)
    k9/                         the ONE shared K9 log (K9_REFUSALS.jsonl + incident stubs)
    w<i>/                       per worker thread: FETCHES.jsonl and INGEST_LEDGER.jsonl (Fetcher out_dir)
    progress/                   resumability: per table the planned months (from the pipeline's own N2
                                listings) and the chunks done
    metrics/                    runs.jsonl (wall time / peak working set per stretch), ingest.json (report)

Threads: worker threads share one K9Log object (so ``k9.using`` never restores "no log" under another thread)
and one RawStore; ``put_chunk`` / ``resume_chunk`` are serialised by a process-local lock around the frozen
methods (the store's own OS lock is per file handle and non-blocking, so two threads of one process would
otherwise get ``StoreBusy``). Each thread has its own Fetcher out_dir, so the append-only logs never interleave.

Planning reads only the committed Stage-0 listing summaries (``var/v7/stage0/listings``): the symbol list and
the few daily-only months. The months ingested per symbol come from the pipeline's own N2 listing.

Usage (foreground, bounded stretches; resumable):
    python scripts/v7/dryrun_ingest.py probe
    python scripts/v7/dryrun_ingest.py bars --table R3 --budget 480 --workers 12
    python scripts/v7/dryrun_ingest.py funding-archive --budget 480
    python scripts/v7/dryrun_ingest.py funding-rest --budget 480
    python scripts/v7/dryrun_ingest.py r5 --budget 480
    python scripts/v7/dryrun_ingest.py meta-captures --budget 480
    python scripts/v7/dryrun_ingest.py meta-build
    python scripts/v7/dryrun_ingest.py report
"""

from __future__ import annotations

import argparse
import contextlib
import ctypes
import json
import os
import sys
import threading
import time
import traceback
from collections import Counter, defaultdict
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from luna.v7 import guard, ingest, k9, netguard  # noqa: E402
from luna.v7.fetch import Fetcher, FetchError  # noqa: E402
from luna.v7.rawstore import TABLES, RawStore  # noqa: E402

ROOT = REPO / "var" / "v7" / "dryrun"
STAGE0 = REPO / "var" / "v7" / "stage0"
STORE = ROOT / "store"
META = ROOT / "meta"
PROG = ROOT / "progress"
METRICS = ROOT / "metrics"
BOUND_MONTH = "2025-07"
BOUND_DAY = "2025-07-01"
MIN_FREE_GB = 6.0
R5_MONTHS = ("2024-04", "2024-05", "2024-06")
# Representative R3 set (coordinator fallback when full DEC-33 R3 > ~6 h): every symbol in REP_YEAR plus the
# full history of these 50 (deterministic: the 27 Stage-0 W_DEV terminations, BTC / ETH, the relisted
# ICP / TLM / BNX, 13 evenly spaced other USDT perps (alphabetical), 5 non-USDT / delivery / settled names)
REP_YEAR = "2024"
REP_FULL = ('1000000BOBUSDT', '1000BTTCUSDT', 'AKROUSDT', 'ALPACAUSDT', 'ANCUSDT', 'ANTUSDT', 'AUDIOUSDT', 'BANDUSDT', 'BLUEBIRDUSDT', 'BNXUSDT', 'BTCBUSD', 'BTCUSDC', 'BTCUSDT', 'BTCUSDT_240628', 'BTSUSDT', 'BTTUSDT', 'BZRXUSDT', 'CGPTUSDT', 'COCOSUSDT', 'DODOUSDT', 'DOTECOUSDT', 'DUSDT', 'EOSUSDT', 'ETHUSDT', 'FOOTBALLUSDT', 'FRONTUSDT', 'GALUSDT', 'GMTUSDT', 'HNTUSDT', 'ICPUSDT', 'ICPUSDT_SETTLED', 'IPUSDT', 'KEEPUSDT', 'LENDUSDT', 'LUNAUSDT', 'MAGICUSDT', 'MATICUSDT', 'MBLUSDT', 'NOTUSDT', 'NUUSDT', 'PORT3USDT', 'RNDRUSDT', 'SANTOSUSDT', 'SRMUSDT', 'SUPERUSDT', 'TLMUSDT', 'TLMUSDTSETTLED', 'TOMOUSDT', 'VELODROMEUSDT', 'YFIIUSDT')
DATASET_OF = {"R3": "monthly_klines_15m", "R5": "monthly_klines_1m", "R4_mark": "monthly_markPriceKlines_15m",
              "R4_index": "monthly_indexPriceKlines_15m", "R4_premium": "monthly_premiumIndexKlines_15m",
              "R2_archive": "monthly_fundingRate"}
GUARD_EXC = (guard.GuardFailure, guard.HoldoutBlocked, guard.EmbargoBlocked, netguard.EndpointRefused)

K9LOG = k9.K9Log.under(ROOT)
k9.configure(K9LOG)  # the process log; every k9.using(...) in the frozen code sets this same object


# ----------------------------------------------------------------------------------------------- utils


def peak_ws() -> int:
    """This process's peak working set (Windows PeakWorkingSetSize via GetProcessMemoryInfo)."""
    class PMC(ctypes.Structure):
        _fields_ = [("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong),
                    ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]
    c = PMC()
    c.cb = ctypes.sizeof(PMC)
    k32 = ctypes.WinDLL("kernel32")
    k32.GetCurrentProcess.restype = ctypes.c_void_p
    psapi = ctypes.WinDLL("psapi")
    psapi.GetProcessMemoryInfo.argtypes = [ctypes.c_void_p, ctypes.POINTER(PMC), ctypes.c_ulong]
    if not psapi.GetProcessMemoryInfo(k32.GetCurrentProcess(), ctypes.byref(c), c.cb):
        raise OSError("GetProcessMemoryInfo failed")
    return int(c.PeakWorkingSetSize)


def free_gb() -> float:
    free = ctypes.c_ulonglong(0)
    ctypes.windll.kernel32.GetDiskFreeSpaceExW(ctypes.c_wchar_p(str(REPO)), None, None, ctypes.byref(free))
    return free.value / 2**30


def append_jsonl(path: Path, rec: dict, lock: threading.Lock | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(rec, sort_keys=True, default=str) + "\n"
    if lock is None:
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)
    else:
        with lock, open(path, "a", encoding="utf-8") as f:
            f.write(line)


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for x in path.read_text(encoding="utf-8").splitlines():
        if x.strip():
            try:
                out.append(json.loads(x))
            except json.JSONDecodeError:
                pass  # a torn last line of an interrupted run
    return out


class LockedStore(RawStore):
    """The frozen RawStore with its write / resume methods serialised across this process's threads."""

    _lock = threading.RLock()

    def put_chunk(self, *a, **kw):
        with self._lock:
            return super().put_chunk(*a, **kw)

    def resume_chunk(self, *a, **kw):
        with self._lock:
            return super().resume_chunk(*a, **kw)


_tls = threading.local()
_STACK = contextlib.ExitStack()  # every Fetcher is entered here (``with Fetcher.default(...)``), closed in main
_STACK_LOCK = threading.Lock()


def fetcher() -> Fetcher:
    """This thread's production fetcher (own out_dir for FETCHES / ledger; the shared K9 log). Entered as a
    context manager on the process ExitStack, so its pooled client is closed when the command ends."""
    f = getattr(_tls, "f", None)
    if f is None or getattr(_tls, "gen", None) != _GEN[0]:
        name = threading.current_thread().name
        idx = name.rsplit("_", 1)[-1] if "_" in name else "main"
        with _STACK_LOCK:
            f = _STACK.enter_context(Fetcher.default(ROOT / f"w{idx}", k9_log=K9LOG))
        _tls.f, _tls.gen = f, _GEN[0]
    return f


_GEN = [0]


def stage0_symbols(dataset: str, *, with_daily: bool = False) -> tuple[list[str], dict[str, list[str]]]:
    """Planning only: symbols with a W_DEV file in the Stage-0 listing summary (+ daily-only months)."""
    m = json.loads((STAGE0 / "listings" / f"{dataset}.json").read_text())["symbols"]
    syms = {s for s, v in m.items() if any(mo < BOUND_MONTH for mo, _ in v["files"])}
    donly: dict[str, list[str]] = defaultdict(list)
    if with_daily:
        d = json.loads((STAGE0 / "listings" / "daily_klines_15m.json").read_text())["symbols"]
        for s, v in d.items():
            mm = {mo for mo, _ in m.get(s, {}).get("files", [])}
            for day, _ in v["files"]:
                if day < BOUND_DAY:
                    syms.add(s)
                    if day[:7] not in mm and day[:7] not in donly[s]:
                        donly[s].append(day[:7])
        del d
    del m
    return sorted(syms), {k: sorted(v) for k, v in donly.items()}


def log_run(kind: str, table: str, t0: float, extra: dict) -> None:
    rec = {"kind": kind, "table": table, "start_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t0)),
           "wall_s": round(time.time() - t0, 1), "peak_ws_bytes": peak_ws(), "free_gb": round(free_gb(), 2),
           **extra}
    append_jsonl(METRICS / "runs.jsonl", rec)
    print(json.dumps(rec, sort_keys=True), flush=True)


# ----------------------------------------------------------------------------------------- the probe


def cmd_probe(_a) -> None:
    """Deliberate guard probes (K9-logged refusals, nothing fetched): W_HOLD N1 file, N3 endTime at the
    bound, an off-allowlist fapi endpoint. Run once (marker)."""
    marker = PROG / "probe.done"
    if marker.exists():
        print("probe already run")
        return
    f = fetcher()
    reqs = [
        netguard.Request(ingest.n1_url("R3", "BTCUSDT", "2025-07")),
        netguard.Request(ingest.n1_url("R3", "BTCUSDT", "2025-07-01")),
        netguard.Request(ingest.FAPI_FUNDING, {"symbol": "BTCUSDT", "startTime": "1748736000000",
                                               "endTime": "1751328000000"}),  # endTime == 2025-07-01
        netguard.Request("https://fapi.binance.com/fapi/v1/klines", {"symbol": "BTCUSDT", "interval": "15m"}),
    ]
    out = []
    for r in reqs:
        try:
            f.fetch(r)
            out.append({"request": r.url, "result": "NOT REFUSED"})
        except GUARD_EXC as e:
            out.append({"request": r.url, "params": r.params, "result": f"{type(e).__name__}: {e}"})
    PROG.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps(out, indent=1), encoding="utf-8")
    print(json.dumps(out, indent=1))


# ------------------------------------------------------------------------------------------ bar tables


def plan_months(table: str, symbols: list[str], donly: dict[str, list[str]], deadline: float,
                workers: int) -> dict[str, list]:
    """Per symbol the months to ingest: the pipeline's own N2 monthly listing (``listed_months``), cached in
    progress/<table>.months.json; plus the Stage-0 daily-only months (R3)."""
    p = PROG / f"{table}.months.json"
    plan: dict[str, list] = json.loads(p.read_text()) if p.exists() else {}
    todo = [s for s in symbols if s not in plan]
    lock = threading.Lock()

    def one(sym: str):
        try:
            ms = ingest.listed_months(fetcher(), "R5" if table == "R5" else table, sym)
            return sym, {"monthly": ms, "daily_only": [m for m in donly.get(sym, []) if m not in ms]}
        except GUARD_EXC as e:
            return sym, {"refused": f"{type(e).__name__}: {e}"}
        except FetchError as e:
            return sym, {"fetch_error": f"{type(e).__name__}: {e}"}

    if todo:
        with ThreadPoolExecutor(workers) as ex:
            futs = set()
            it = iter(todo)
            while True:
                while len(futs) < workers * 2 and time.time() < deadline:
                    s = next(it, None)
                    if s is None:
                        break
                    futs.add(ex.submit(one, s))
                if not futs:
                    break
                done, futs = wait(futs, return_when=FIRST_COMPLETED)
                for fu in done:
                    try:
                        sym, v = fu.result()
                    except Exception as e:  # network (httpx) error: retried next run
                        print("plan error", type(e).__name__, e, flush=True)
                        continue
                    if "fetch_error" in v:
                        print("plan fetch error", sym, v, flush=True)
                        continue
                    with lock:
                        plan[sym] = v
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(".tmp")
            tmp.write_text(json.dumps(plan, sort_keys=True), encoding="utf-8")
            os.replace(tmp, p)
    return plan


def _transient(r: dict) -> bool:
    """A gap from a server-side HTTP 5xx (e.g. 503 throttling): retried up to 3 attempts in all."""
    import re
    return r.get("status") == "gap" and re.search(r"HTTP 5\d\d", r.get("reason") or "") is not None


def done_chunks(table: str) -> set[tuple[str, str]]:
    recs = read_jsonl(PROG / f"{table}.chunks.jsonl")
    tries = Counter((r["symbol"], r["month"]) for r in recs if _transient(r))
    return {(r["symbol"], r["month"]) for r in recs
            if r.get("status") in ("stored", "refused", "error_final")
            or (r.get("status") == "gap" and (not _transient(r) or tries[(r["symbol"], r["month"])] >= 3))}


def run_chunk(store: RawStore, table: str, sym: str, month: str, daily_only: bool) -> dict:
    t0 = time.time()
    f = fetcher()
    try:
        if daily_only:  # DEC-33: a month with daily files only (no monthly file), same frozen path
            try:
                e = ingest.ingest_bars(f, store, table, sym, month, monthly=False)
                rec = ingest._ledger(f, ingest._stored(table, sym, month, e), store)
            except ingest.NONFATAL as err:  # R55: gap + conflict flag also in the store ledger
                rec = ingest._ledger(f, ingest._err_gap(table, sym, month, err), store)
        else:
            rec = ingest.ingest_months(f, store, table, sym, [month])[0]
        status, reason = rec["status"], rec["reason"]
    except GUARD_EXC as e:
        status, reason = "refused", f"{type(e).__name__}: {e}"
    except Exception as e:  # noqa: BLE001 - network errors (retried) and code failures (recorded)
        status, reason = "error", f"{type(e).__name__}: {e} | {traceback.format_exc(limit=3)[-600:]}"
    return {"table": table, "symbol": sym, "month": month, "status": status, "reason": reason,
            "daily_only": daily_only, "wall_s": round(time.time() - t0, 3)}


def run_bars(table: str, symbols: list[str], donly: dict[str, list[str]], months_filter, budget: float,
             workers: int, retry_errors: bool = False) -> None:
    t0 = time.time()
    deadline = t0 + budget
    plan = plan_months(table, symbols, donly, deadline, workers)
    done = done_chunks(table)
    errs = Counter((r["symbol"], r["month"]) for r in read_jsonl(PROG / f"{table}.chunks.jsonl")
                   if r.get("status") == "error")
    tasks = []
    for s in symbols:
        v = plan.get(s)
        if v is None or "refused" in v:
            continue
        for m in v["monthly"]:
            if months_filter(s, m) and (s, m) not in done:
                tasks.append((s, m, False))
        for m in v.get("daily_only", []):
            if months_filter(s, m) and (s, m) not in done:
                tasks.append((s, m, True))
    store = LockedStore(STORE)
    plock = threading.Lock()
    n = Counter()
    stop_reason = None
    with ThreadPoolExecutor(workers) as ex:
        futs = set()
        it = iter(tasks)
        while True:
            while len(futs) < workers * 2 and time.time() < deadline and stop_reason is None:
                t = next(it, None)
                if t is None:
                    break
                futs.add(ex.submit(run_chunk, store, table, *t))
            if not futs:
                break
            done_f, futs = wait(futs, return_when=FIRST_COMPLETED)
            for fu in done_f:
                r = fu.result()
                if r["status"] == "error" and errs[(r["symbol"], r["month"])] >= 2:
                    r["status"] = "error_final"  # third failure: recorded, not retried
                append_jsonl(PROG / f"{table}.chunks.jsonl", r, plock)
                n[r["status"]] += 1
                if r["status"] in ("error", "refused", "error_final"):
                    print(r["status"], r["symbol"], r["month"], r["reason"][:300], flush=True)
            if free_gb() < MIN_FREE_GB:
                stop_reason = f"free disk {free_gb():.2f} GB < {MIN_FREE_GB} GB"
    remaining = len(tasks) - sum(n.values())
    log_run("bars", table, t0, {"chunks": dict(n), "tasks_planned_this_run": len(tasks),
                                "remaining": remaining, "planned_symbols": len(plan),
                                "symbols": len(symbols), "stop_reason": stop_reason})


def cmd_bars(a) -> None:
    table = a.table
    if table not in ("R3", "R4_mark", "R4_index", "R4_premium"):
        raise SystemExit("bars: R3 or R4_*")
    syms, donly = stage0_symbols(DATASET_OF[table], with_daily=(table == "R3"))
    if a.symbols:
        syms = [s for s in syms if s in set(a.symbols.split(","))]
    full = set(REP_FULL) if a.representative else None
    if full is not None:  # representative set (coordinator): full history of REP_FULL, plus every symbol in 2024
        syms = sorted(syms, key=lambda s: (s not in full, s))
        filt = lambda s, m: m < BOUND_MONTH and (s in full or m.startswith(REP_YEAR))  # noqa: E731
    else:
        filt = lambda s, m: m < BOUND_MONTH  # noqa: E731
    run_bars(table, syms, donly if table == "R3" else {}, filt, a.budget, a.workers)


def cmd_r5(a) -> None:
    """R5 1m: every symbol with a monthly 1m file in 2024-Q2 (Stage-0 planning), months 2024-04..06."""
    m = json.loads((STAGE0 / "listings" / "monthly_klines_1m.json").read_text())["symbols"]
    syms = sorted(s for s, v in m.items() if any(mo in R5_MONTHS for mo, _ in v["files"]))
    del m
    if "BTCUSDT" not in syms:
        syms.append("BTCUSDT")
    run_bars("R5", syms, {}, lambda s, mo: mo in R5_MONTHS, a.budget, a.workers)


# ------------------------------------------------------------------------------------------- funding


def cmd_funding_archive(a) -> None:
    t0 = time.time()
    deadline = t0 + a.budget
    syms, _ = stage0_symbols(DATASET_OF["R2_archive"])
    p = PROG / "R2_archive.symbols.jsonl"
    done = {r["symbol"] for r in read_jsonl(p) if r["status"] in ("done", "refused", "error_final")}
    errs = Counter(r["symbol"] for r in read_jsonl(p) if r["status"] == "error")
    store = LockedStore(STORE)
    plock = threading.Lock()

    def one(sym: str) -> dict:
        s0 = time.time()
        try:
            recs = ingest.ingest_months(fetcher(), store, "R2_archive", sym)
            st = Counter(r["status"] for r in recs)
            return {"symbol": sym, "status": "done", "ledger": dict(st), "wall_s": round(time.time() - s0, 2)}
        except GUARD_EXC as e:
            return {"symbol": sym, "status": "refused", "reason": f"{type(e).__name__}: {e}"}
        except Exception as e:  # noqa: BLE001
            return {"symbol": sym, "status": "error", "reason": f"{type(e).__name__}: {e} | "
                                                                 f"{traceback.format_exc(limit=3)[-600:]}"}

    n = Counter()
    todo = [s for s in syms if s not in done]
    with ThreadPoolExecutor(a.workers) as ex:
        futs = set()
        it = iter(todo)
        while True:
            while len(futs) < a.workers * 2 and time.time() < deadline:
                s = next(it, None)
                if s is None:
                    break
                futs.add(ex.submit(one, s))
            if not futs:
                break
            dn, futs = wait(futs, return_when=FIRST_COMPLETED)
            for fu in dn:
                r = fu.result()
                if r["status"] == "error" and errs[r["symbol"]] >= 2:
                    r["status"] = "error_final"
                append_jsonl(p, r, plock)
                n[r["status"]] += 1
                if r["status"] != "done":
                    print(r, flush=True)
    log_run("funding_archive", "R2_archive", t0, {"symbols": dict(n), "remaining": len(todo) - sum(n.values())})


def rest_plan() -> list[tuple[str, str]]:
    """S-2.6 gap fill: the (symbol, month) R3 chunks (stored) without a stored R2_archive chunk."""
    store = RawStore(STORE)
    out = []
    r3 = store.table_dir("R3")
    for sd in sorted(x for x in r3.iterdir() if x.is_dir()):
        sm = store.symbol_manifest("R3", sd.name)
        if sm is None:
            continue
        fa = store.symbol_manifest("R2_archive", sd.name)
        have = set(fa["chunks"]) if fa else set()
        for m in sm["chunks"]:
            if m not in have:
                out.append((sd.name, m))
    return out


def cmd_funding_rest(a) -> None:
    t0 = time.time()
    deadline = t0 + a.budget
    p = PROG / "R2_rest.chunks.jsonl"
    done = {(r["symbol"], r["month"]) for r in read_jsonl(p) if r["status"] in ("stored", "gap", "refused")}
    todo = [t for t in rest_plan() if t not in done]
    store = LockedStore(STORE)
    n = Counter()
    for sym, m in todo:  # serial: the N3 endpoint is rate limited (weight); one request per month
        if time.time() > deadline:
            break
        s0 = time.time()
        try:
            rec = ingest.ingest_months(fetcher(), store, "R2_rest", sym, [m])[0]
            st, why = rec["status"], rec["reason"]
        except GUARD_EXC as e:
            st, why = "refused", f"{type(e).__name__}: {e}"
        except Exception as e:  # noqa: BLE001
            st, why = "error", f"{type(e).__name__}: {e}"
        append_jsonl(p, {"symbol": sym, "month": m, "status": st, "reason": why,
                         "wall_s": round(time.time() - s0, 2)})
        n[st] += 1
        if st != "stored":
            print(st, sym, m, (why or "")[:200], flush=True)
        time.sleep(0.25)
    log_run("funding_rest", "R2_rest", t0, {"chunks": dict(n), "planned": len(todo),
                                            "remaining": len(todo) - sum(n.values())})


# ------------------------------------------------------------------------------------------ metadata


def notice_queries():
    """The bounded N5 queries: fapi exchangeInfo (exact) and the announcement URLs of the committed Stage-0
    M3.1 notice set plus the Stage-0 catalog-161 futures-delisting articles (planning lists only; the
    captures themselves come from the frozen N5 -> N6 path)."""
    from luna.v7.metaingest import CdxQuery
    qs = [CdxQuery("fapi.binance.com/fapi/v1/exchangeInfo", "exact", filter="statuscode:200")]
    urls: set[str] = set()
    nf = REPO / "docs" / "results" / "v7-stage0" / "m31" / "notices.json"
    for r in json.loads(nf.read_text(encoding="utf-8")):
        for n_ in r["notices"]:
            u = n_["url"].split("://", 1)[-1]
            if not any(c.isspace() for c in u) and "%20" not in u:
                urls.add(u)
    cat = json.loads((STAGE0 / "delist" / "futures_delist_announcements.json").read_text(encoding="utf-8"))
    for art in cat:
        urls.add(f"www.binance.com/en/support/announcement/{art['code']}")
    for u in sorted(urls):
        qs.append(CdxQuery(u, "exact", filter="statuscode:200", limit=4))
    return qs


def cmd_meta_captures(a) -> None:
    from luna.v7.metaingest import ingest_captures
    from luna.v7.metastore import MetaStore
    t0 = time.time()
    deadline = t0 + a.budget
    p = PROG / "meta.queries.jsonl"
    done = {r["query"] for r in read_jsonl(p) if r["status"] in ("done", "refused")}
    ms = MetaStore(META)
    n = Counter()
    for q in notice_queries():
        if q.url in done:
            continue
        if time.time() > deadline:
            break
        s0 = time.time()
        try:
            outs = ingest_captures(fetcher(), ms, [q])
            rec = {"query": q.url, "status": "done", "outcomes": [o.as_dict() for o in outs]}
        except GUARD_EXC as e:  # seal-class refusal: K9-logged by the fetch layer; recorded, not worked around
            rec = {"query": q.url, "status": "refused", "reason": f"{type(e).__name__}: {e}"}
        except Exception as e:  # noqa: BLE001 - network: retried next run
            rec = {"query": q.url, "status": "error", "reason": f"{type(e).__name__}: {e}"}
        rec["wall_s"] = round(time.time() - s0, 2)
        append_jsonl(p, rec)
        n[rec["status"]] += 1
        print(rec["status"], q.url, Counter(o["status"] for o in rec.get("outcomes", [])), rec.get("reason", ""),
              flush=True)
    log_run("meta_captures", "meta", t0, {"queries": dict(n)})


def cmd_meta_build(a) -> None:
    """N4 + capture identity sources -> R1; notices -> R11; R12 empty; the frozen ingest_metadata report."""
    from luna.v7 import windows
    from luna.v7.metaingest import fetch_exchange_info, identity_sources, ingest_metadata, instances_from_store
    from luna.v7.metastore import MetaStore
    t0 = time.time()
    store = RawStore(STORE)
    ms = MetaStore(META)
    syms = sorted(x.name for x in store.table_dir("R3").iterdir() if x.is_dir())
    insts = instances_from_store(store, syms, windows.W_DEV[1])
    srcs, pit = identity_sources(ms)
    n4, cur = fetch_exchange_info(fetcher())
    sources = [*srcs, n4]
    rp = METRICS / f"meta_report_{a.scope}.json"
    res = ingest_metadata(ms, insts, sources, scope=a.scope, report_path=rp)
    log_run("meta_build", "meta", t0, {"symbols_with_instances": len(insts),
                                       "instances": sum(len(v) for v in insts.values()),
                                       "identity_sources": len(sources), "pit_filter_symbols": len(pit),
                                       "n4_current_filter_symbols": len(cur),
                                       "r1": res.report["instruments"]["counts"],
                                       "notices": res.report["notices"]["counts"], "report": str(rp)})


# --------------------------------------------------------------------------------------------- report


def cmd_report(a) -> None:
    store = RawStore(STORE)
    tables = {}
    for key, t in TABLES.items():
        td = store.table_dir(key)
        if not td.exists():
            continue
        syms = files = rows = nbytes = src_files = src_bytes = 0
        disk = 0
        for sd in sorted(x for x in td.iterdir() if x.is_dir()):
            sm = store.symbol_manifest(key, sd.name)
            if not sm:
                continue
            syms += 1
            for e in sm["chunks"].values():
                files += 1
                rows += e["rows"]
                nbytes += e["bytes"]
                src_files += len(e["sources"])
                src_bytes += sum(int(s.get("bytes") or 0) for s in e["sources"])
            disk += sum(f.stat().st_size for f in sd.iterdir() if f.is_file())
        disk += sum(f.stat().st_size for f in td.iterdir() if f.is_file())
        tables[key] = {"symbols": syms, "chunks": files, "rows": rows, "parquet_bytes": nbytes,
                       "disk_bytes_incl_manifests": disk, "B_per_row": round(nbytes / rows, 2) if rows else None,
                       "source_files": src_files, "source_bytes_fetched": src_bytes}
    # ledgers
    led = []
    for w in sorted(ROOT.glob("w*")):
        led += read_jsonl(w / ingest.LEDGER)
    gaps = defaultdict(Counter)
    for r in led:
        if r.get("status") in ("gap", "spill_not_requested", "cdx_failed", "fetch_failed"):
            reason = (r.get("reason") or "")
            short = reason.split(":")[0] + ":" + ("".join(reason.split(":")[1:2])[-80:] if ":" in reason else "")
            gaps[r.get("table") or r.get("kind")][(r["status"], _reason_class(reason))] += 1
    fetches = Counter()
    for w in sorted(ROOT.glob("w*")):
        for r in read_jsonl(w / "FETCHES.jsonl"):
            fetches[r["kind"] + (".CHECKSUM" if r["url"].endswith(".CHECKSUM") else "")] += 1
    k9recs = K9LOG.records()
    runs = read_jsonl(METRICS / "runs.jsonl")
    wall = defaultdict(float)
    for r in runs:
        wall[r["table"]] += r["wall_s"]
    prog = {}
    for p in sorted(PROG.glob("*.chunks.jsonl")) + sorted(PROG.glob("*.symbols.jsonl")):
        last = {}
        for r in read_jsonl(p):
            last[(r.get("symbol"), r.get("month"))] = r
        prog[p.name] = dict(Counter(r["status"] for r in last.values()))
        errs = Counter(_reason_class(r.get("reason") or "") for r in last.values()
                       if r["status"] in ("error", "error_final", "refused"))
        if errs:
            prog[p.name + ":errors"] = dict(errs)
    meta_rep = {}
    for rp in sorted(METRICS.glob("meta_report_*.json")):
        rep = json.loads(rp.read_text(encoding="utf-8"))
        meta_rep[rp.name] = {"notices": rep["notices"]["counts"],
                             "instruments": {k: rep["instruments"][k] for k in
                                             ("counts", "excluded_by_reason", "universe_by_reason")},
                             "r12": rep["r12"]}
    out = {"kind": "V7_C6_PHASE_A_INGEST", "outcome_free": True,
           "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "store_root": str(STORE), "tables": tables,
           "gaps_by_table_reason": {k: {f"{s} | {c}": n for (s, c), n in v.most_common()} for k, v in gaps.items()},
           "progress": prog, "fetches_by_kind": dict(fetches),
           "k9_refusals": {"count": len(k9recs), "by_rule": dict(Counter(r["rule"] for r in k9recs)),
                           "records": [{k: r[k] for k in ("utc", "rule", "exception", "request")} for r in k9recs]},
           "wall_s_by_table": {k: round(v, 1) for k, v in wall.items()},
           "peak_ws_bytes_max": max((r["peak_ws_bytes"] for r in runs), default=None),
           "runs": len(runs), "metadata": meta_rep, "free_gb_now": round(free_gb(), 2)}
    if a.extra and Path(a.extra).exists():
        out["extrapolation"] = json.loads(Path(a.extra).read_text())
    METRICS.mkdir(parents=True, exist_ok=True)
    (METRICS / "ingest.json").write_text(json.dumps(out, indent=1, sort_keys=True, default=str), encoding="utf-8")
    print(json.dumps({k: out[k] for k in ("tables", "gaps_by_table_reason", "progress", "fetches_by_kind",
                                          "wall_s_by_table", "peak_ws_bytes_max")}, indent=1, default=str))
    print("k9", out["k9_refusals"]["count"], out["k9_refusals"]["by_rule"])


def _reason_class(reason: str) -> str:
    """A short reason class for counting (exception type + the stable part of the message)."""
    import re
    r = re.sub(r"https?://\S+", "<url>", reason)
    r = re.sub(r"\d{4}-\d{2}(-\d{2})?", "<date>", r)
    r = re.sub(r"\d+", "<n>", r)
    return r[:140]


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("probe")
    b = sub.add_parser("bars")
    b.add_argument("--table", required=True)
    b.add_argument("--symbols")
    b.add_argument("--representative", action="store_true")
    for name in ("bars", "r5", "funding-archive", "funding-rest", "meta-captures"):
        s = b if name == "bars" else sub.add_parser(name)
        s.add_argument("--budget", type=float, default=480)
        s.add_argument("--workers", type=int, default=8)
    mb = sub.add_parser("meta-build")
    mb.add_argument("--scope", default="W_DEV")
    rp = sub.add_parser("report")
    rp.add_argument("--extra")
    a = ap.parse_args()
    with _STACK:
        _run(a)


def _run(a) -> None:
    {"probe": cmd_probe, "bars": cmd_bars, "r5": cmd_r5, "funding-archive": cmd_funding_archive,
     "funding-rest": cmd_funding_rest, "meta-captures": cmd_meta_captures, "meta-build": cmd_meta_build,
     "report": cmd_report}[a.cmd](a)


if __name__ == "__main__":
    main()
