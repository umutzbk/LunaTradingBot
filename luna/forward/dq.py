"""``luna forward-dq``: data-quality report over the raw envelopes (spec §7).

Reports only timing, coverage, gaps, completeness and quote-sanity *counts*. By design it never
outputs a price level, mid, basis, spread magnitude, funding rate, return or P&L.

Funding: ``fut_funding`` (last 48 h per hourly call) and ``fut_funding_full`` (daily, full history,
including periods before the collector existed, e.g. the sealed holdout) are both read. Only
settlement rows with ``timestamp >= collector_start_ts`` (from ``meta.json``) are counted; earlier
rows are only counted as excluded. Every source is streamed; rows are never held in memory.
"""

from __future__ import annotations

import gzip
import json
import math
import time
from collections import Counter, defaultdict
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from luna.forward.config import PERP_SYMBOLS, SOURCES, SPOT_PAIRS, expected_rows
from luna.forward.envelope import NS, iso_ns, ns_from_iso
from luna.forward.meta import Meta
from luna.forward.writer import already_closed, day_of, day_start_s

MS = 1_000_000


def _pct(v: list[float], q: float) -> float:
    """Linear-interpolated percentile of the sorted list ``v``."""
    pos = (len(v) - 1) * q
    lo, hi = math.floor(pos), math.ceil(pos)
    return v[lo] + (v[hi] - v[lo]) * (pos - lo)


def _stats(values: list[float]) -> dict[str, float | int | None]:
    if not values:
        return {"n": 0, "min": None, "p50": None, "p95": None, "max": None, "mean": None}
    v = sorted(values)
    return {
        "n": len(v),
        "min": round(v[0], 3),
        "p50": round(_pct(v, 0.5), 3),
        "p95": round(_pct(v, 0.95), 3),
        "max": round(v[-1], 3),
        "mean": round(sum(v) / len(v), 3),
    }


def _dist(values: list[float]) -> dict[str, float | int | None]:
    """Tail distribution: n, p50, p95, p99, max."""
    if not values:
        return {"n": 0, "p50": None, "p95": None, "p99": None, "max": None}
    v = sorted(values)
    return {
        "n": len(v),
        "p50": round(_pct(v, 0.5), 3),
        "p95": round(_pct(v, 0.95), 3),
        "p99": round(_pct(v, 0.99), 3),
        "max": round(v[-1], 3),
    }


class _StaleQuotes:
    """How long each symbol's (bid, ask) pair stayed identical across consecutive ok snapshots
    (recv_ts of the last identical snapshot minus the first). Only the current pair per symbol is
    held, to compare with the next snapshot; the output is durations only, never a quote. A run
    breaks when the pair changes or the symbol is absent from an ok snapshot; failed snapshots are
    skipped. Single-snapshot runs (no repeat) are not counted; such a symbol reports max 0."""

    def __init__(self) -> None:
        self.cur: dict[str, tuple[Any, int, int]] = {}  # symbol -> (quote pair, first ns, last ns)
        self.runs: list[float] = []
        self.sym_max: dict[str, float] = {}
        self.out_of_order = 0

    def _close(self, sym: str) -> None:
        _, first, last = self.cur.pop(sym)
        if last > first:
            dur = (last - first) / NS
            self.runs.append(dur)
            self.sym_max[sym] = max(self.sym_max[sym], dur)

    def snapshot(self, recv_ns: int, quotes: dict[str, Any]) -> None:
        """``quotes``: symbol -> (bid, ask) of one ok snapshot."""
        for sym in [s for s in self.cur if s not in quotes]:
            self._close(sym)
        for sym, q in quotes.items():
            self.sym_max.setdefault(sym, 0.0)
            run = self.cur.get(sym)
            if run is not None and recv_ns < run[2]:
                self.out_of_order += 1  # late file row: not consecutive, ignored
                continue
            if run is not None and run[0] == q and None not in q:
                self.cur[sym] = (q, run[1], recv_ns)
                continue
            if run is not None:
                self._close(sym)
            self.cur[sym] = (q, recv_ns, recv_ns)

    def out(self) -> dict[str, Any]:
        for sym in list(self.cur):
            self._close(sym)
        return {
            "runs": _dist(self.runs),
            "per_symbol_max": {k: round(v, 3) for k, v in self.sym_max.items()},
            "out_of_order_skipped": self.out_of_order,
        }


def _hour_data(day_dir: Path) -> Iterator[bytes]:
    """Contents of a day dir's closed parts and open files, each row exactly once: an open file
    whose bytes already sit in a closed part (crash between gz replace and unlink) is skipped, and
    an open file that the collector closes while we read (it vanishes) is read from its new gz."""
    try:
        listed = sorted(day_dir.iterdir())
    except FileNotFoundError:
        return
    names = {f.name for f in listed}
    for f in listed:
        data = None
        try:
            if f.name.endswith(".jsonl.gz"):
                data = gzip.decompress(f.read_bytes())
            elif f.name.endswith(".jsonl") and not already_closed(f):
                data = f.read_bytes()
        except FileNotFoundError:
            gz = f.with_name(f.name + ".gz")
            if f.name.endswith(".jsonl") and gz.name not in names and gz.exists():
                data = gzip.decompress(gz.read_bytes())
        if data is not None:
            yield data


def iter_rows(root: Path, source: str, since_ns: int, until_ns: int) -> Iterator[dict[str, Any]]:
    """Stream the envelopes of ``source`` with ``since_ns <= recv < until_ns``, one at a time
    (closed ``.gz`` parts and open ``.jsonl``); unparsable lines are skipped."""
    first_day, last_day = day_of(since_ns), day_of(max(since_ns, until_ns - 1))
    for d in sorted((Path(root) / "raw" / source).glob("date=*")):
        if not first_day <= d.name.removeprefix("date=") <= last_day:
            continue
        for data in _hour_data(d):
            for line in data.splitlines():
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                    recv = ns_from_iso(row["recv_ts"])
                except (ValueError, KeyError, TypeError):
                    continue
                if since_ns <= recv < until_ns:
                    row["_recv_ns"] = recv
                    yield row


def _spot_key_pair(key: str) -> str | None:
    """Kraken result key -> our pair (``XXBTZUSD`` -> ``XBTUSD``, ``SOLUSD`` -> ``SOLUSD``)."""
    if key in SPOT_PAIRS:
        return key
    if key.startswith("X") and key.endswith("ZUSD") and f"{key[1:-4]}USD" in SPOT_PAIRS:
        return f"{key[1:-4]}USD"
    return None


def _float(x: Any) -> float | None:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


class _Sanity:
    def __init__(self) -> None:
        self.quotes = self.bid_le_ask = self.positive = 0

    def add(self, bid: Any, ask: Any) -> None:
        b, a = _float(bid), _float(ask)
        if b is None or a is None:
            return
        self.quotes += 1
        self.bid_le_ask += b <= a
        self.positive += b > 0 and a > 0

    def out(self) -> dict[str, int]:
        return {"quotes": self.quotes, "bid_le_ask": self.bid_le_ask, "positive": self.positive}


def _completeness(counts: list[int], missing: Counter, suspended: Counter | None) -> dict[str, Any]:
    out: dict[str, Any] = {
        "snapshots": len(counts),
        "present_min": min(counts) if counts else None,
        "present_mean": round(sum(counts) / len(counts), 3) if counts else None,
        "present_max": max(counts) if counts else None,
        "missing": dict(sorted(missing.items())),
    }
    if suspended is not None:
        out["suspended"] = dict(sorted(suspended.items()))
    return out


def _gaps(times: list[int], period_s: int) -> list[dict[str, Any]]:
    out = []
    for a, b in zip(times, times[1:], strict=False):
        if b - a > 2 * period_s * NS:
            out.append({"start": iso_ns(a), "end": iso_ns(b), "duration_s": round((b - a) / NS, 3)})
    return out


def _gap_stats_daily(times: list[int], days: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Per UTC day: the largest interval between consecutive ok snapshots and how many exceed
    15 min; an interval counts on the day of its later snapshot."""
    out = {d: {"max_gap_s": None, "gaps_over_15min": 0} for d in days}
    for a, b in zip(times, times[1:], strict=False):
        e = out.setdefault(day_of(b), {"max_gap_s": None, "gaps_over_15min": 0})
        g = round((b - a) / NS, 3)
        e["max_gap_s"] = g if e["max_gap_s"] is None else max(e["max_gap_s"], g)
        e["gaps_over_15min"] += g > GAP_LONG_S
    return dict(sorted(out.items()))


FUNDING_SOURCES = ("fut_funding", "fut_funding_full")
ABS_SKEW_LIMITS_MS = (1000, 2000, 5000)
GAP_LONG_S = 15 * 60


class _Acc:
    """Streaming accumulators: only small numbers per row are kept, never rows or payloads."""

    def __init__(self) -> None:
        self.fut_pair: dict[Any, int] = {}
        self.spot_pair: dict[Any, int] = {}
        self.last_lag: list[float] = []
        self.fut_server: list[float] = []
        self.spot_server: list[float] = []
        self.fut_counts: list[int] = []
        self.spot_counts: list[int] = []
        self.fut_missing: Counter = Counter()
        self.fut_susp: Counter = Counter()
        self.spot_missing: Counter = Counter()
        self.fut_sanity = _Sanity()
        self.spot_sanity = _Sanity()
        self.settled: dict[str, set[str]] = defaultdict(set)
        self.excluded = 0
        self.fut_stale = _StaleQuotes()
        self.spot_stale = _StaleQuotes()

    def fut_tickers(self, r: dict[str, Any]) -> None:
        self.fut_pair[r.get("pair_seq")] = r["_recv_ns"]
        payload = r["payload"] or {}
        if payload.get("serverTime"):
            self.fut_server.append((r["_recv_ns"] - ns_from_iso(payload["serverTime"])) / MS)
        present = set()
        quotes: dict[str, Any] = {}
        for t in payload.get("tickers") or []:
            sym = t.get("symbol")
            present.add(sym)
            if sym:
                quotes[sym] = (t.get("bid"), t.get("ask"))
            if t.get("lastTime"):
                self.last_lag.append((r["_recv_ns"] - ns_from_iso(t["lastTime"])) / MS)
            if t.get("suspended"):
                self.fut_susp[sym] += 1
            self.fut_sanity.add(t.get("bid"), t.get("ask"))
        self.fut_stale.snapshot(r["_recv_ns"], quotes)
        self.fut_counts.append(len(present & set(PERP_SYMBOLS)))
        self.fut_missing.update(s for s in PERP_SYMBOLS if s not in present)

    def spot_ticker(self, r: dict[str, Any]) -> None:
        self.spot_pair[r.get("pair_seq")] = r["_recv_ns"]
        present = set()
        quotes: dict[str, Any] = {}
        for key, q in ((r["payload"] or {}).get("result") or {}).items():
            pair = _spot_key_pair(key)
            if pair:
                present.add(pair)
            if isinstance(q, dict):
                bid, ask = (q.get("b") or [None])[0], (q.get("a") or [None])[0]
                self.spot_sanity.add(bid, ask)
                quotes[pair or key] = (bid, ask)
        self.spot_stale.snapshot(r["_recv_ns"], quotes)
        self.spot_counts.append(len(present))
        self.spot_missing.update(p for p in SPOT_PAIRS if p not in present)

    def spot_time(self, r: dict[str, Any]) -> None:
        unix = ((r["payload"] or {}).get("result") or {}).get("unixtime")
        if unix is not None:
            self.spot_server.append((r["_recv_ns"] - int(unix) * NS) / MS)

    def funding(self, r: dict[str, Any], from_ns: int, until_ns: int) -> None:
        sym = (r.get("params") or {}).get("symbol", "?")
        for row in (r["payload"] or {}).get("rates") or []:
            ts = row.get("timestamp")
            if not ts:
                continue
            t = ns_from_iso(ts)
            if t < from_ns:
                self.excluded += 1
            elif t < until_ns:
                self.settled[sym].add(iso_ns(t))


def forward_dq(
    root: Path, since_ns: int | None = None, until_ns: int | None = None, now_ns: int | None = None
) -> dict[str, Any]:
    """Stream every source once and report data quality. Raises ValueError when
    ``collector_start_ts`` is unknown (no meta.json): warmup/evidence and funding cut-offs depend
    on it, so the report fails closed instead of guessing."""
    root = Path(root)
    start_ns = Meta.load(root).collector_start_ns
    if start_ns is None:
        raise ValueError(f"collector_start_ts unknown ({root / 'meta.json'} missing or empty)")
    until_ns = until_ns if until_ns is not None else (now_ns if now_ns is not None else time.time_ns())
    since_ns = start_ns if since_ns is None else since_ns
    window_start = max(since_ns, start_ns)
    fund_from = window_start
    acc = _Acc()
    coverage: dict[str, dict[str, dict[str, Any]]] = {}
    gaps: dict[str, list[dict[str, Any]]] = {}
    gap_daily: dict[str, dict[str, dict[str, Any]]] = {}
    for s in SOURCES:
        per_day: dict[str, dict[str, Any]] = {}
        day = day_of(window_start)
        while day <= day_of(max(window_start, until_ns - 1)):
            d0 = day_start_s(day)
            a, b = max(d0, window_start / NS), min(d0 + 86400, until_ns / NS)
            per_day[day] = {"expected": expected_rows(s, a, b), "ok": 0, "errors": 0, "at_start": 0}
            day = day_of((d0 + 86400) * NS)
        ok_times: list[int] = []
        for r in iter_rows(root, s, since_ns, until_ns):
            entry = per_day.setdefault(
                day_of(r["_recv_ns"]), {"expected": 0, "ok": 0, "errors": 0, "at_start": 0}
            )
            if r.get("at_start"):
                entry["at_start"] += 1  # extra start fetch: not a scheduled snapshot
            else:
                entry["ok" if r.get("ok") else "errors"] += 1
            if not r.get("ok"):
                continue
            ok_times.append(r["_recv_ns"])
            if s in ("fut_tickers", "spot_ticker", "spot_time"):
                getattr(acc, s)(r)
            elif s in FUNDING_SOURCES:
                acc.funding(r, fund_from, until_ns)
        for entry in per_day.values():
            entry["ratio"] = round(entry["ok"] / entry["expected"], 4) if entry["expected"] else None
        coverage[s] = per_day
        ok_times.sort()
        gaps[s] = _gaps(ok_times, SOURCES[s].period_s)
        gap_daily[s] = _gap_stats_daily(ok_times, per_day)

    pair_diff = [(t - acc.spot_pair[k]) / MS for k, t in acc.fut_pair.items() if k in acc.spot_pair]
    pair_abs = [abs(x) for x in pair_diff]
    hour = 3600 * NS
    first_hour = -(-fund_from // hour) * hour
    expected_settlements = max(0, -(-(until_ns - first_hour) // hour))
    return {
        "window": {
            "since": iso_ns(since_ns),
            "until": iso_ns(until_ns),
            "collector_start_ts": iso_ns(start_ns),
        },
        "paired_skew": {
            "pairs": len(pair_diff),
            "fut_minus_spot_recv_ms": _stats(pair_diff),
            "fut_recv_minus_lasttime_ms": _stats(acc.last_lag),
            "abs_ms": _dist(pair_abs),
            "abs_within_ms": {
                str(lim): (round(sum(x <= lim for x in pair_abs) / len(pair_abs), 4) if pair_abs else None)
                for lim in ABS_SKEW_LIMITS_MS
            },
            "note": "spot Ticker has no quote timestamp: spot skew is request/response time only",
        },
        "clock_skew": {
            "futures_recv_minus_server_ms": _stats(acc.fut_server),
            "spot_recv_minus_server_ms": _stats(acc.spot_server),
        },
        "coverage": coverage,
        "funding_coverage": {
            "since": iso_ns(fund_from),
            "sources": list(FUNDING_SOURCES),
            "expected_settlements_per_symbol": expected_settlements,
            "symbols": {s: {"settlements": len(acc.settled.get(s, ()))} for s in PERP_SYMBOLS},
            "rows_before_start_excluded": acc.excluded,
        },
        "gaps": gaps,
        "gap_stats_daily": gap_daily,
        "stale_quote_age_s": {
            "fut_tickers": acc.fut_stale.out(),
            "spot_ticker": acc.spot_stale.out(),
            "note": "seconds the (bid, ask) pair stayed identical across consecutive ok snapshots",
        },
        "asset_completeness": {
            "fut_tickers": _completeness(acc.fut_counts, acc.fut_missing, acc.fut_susp),
            "spot_ticker": _completeness(acc.spot_counts, acc.spot_missing, None),
        },
        "quote_sanity": {"fut_tickers": acc.fut_sanity.out(), "spot_ticker": acc.spot_sanity.out()},
    }
