"""``luna forward-dq``: data-quality report over the raw envelopes (spec §7).

Reports only timing, coverage, gaps, completeness and quote-sanity *counts*. By design it never
outputs a price level, mid, basis, spread magnitude, funding rate, return or P&L.

Funding: ``fut_funding`` payloads carry the full history (including periods before the collector
existed, e.g. the sealed holdout). Only settlement rows with ``timestamp >= collector_start_ts``
(from ``meta.json``) are counted; earlier rows are only counted as excluded.
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
from luna.forward.writer import day_of, day_start_s

MS = 1_000_000


def _stats(values: list[float]) -> dict[str, float | int | None]:
    if not values:
        return {"n": 0, "min": None, "p50": None, "p95": None, "max": None, "mean": None}
    v = sorted(values)

    def pct(q: float) -> float:
        pos = (len(v) - 1) * q
        lo, hi = math.floor(pos), math.ceil(pos)
        return v[lo] + (v[hi] - v[lo]) * (pos - lo)

    return {"n": len(v), "min": round(v[0], 3), "p50": round(pct(0.5), 3), "p95": round(pct(0.95), 3),
            "max": round(v[-1], 3), "mean": round(sum(v) / len(v), 3)}


def iter_rows(root: Path, source: str, since_ns: int, until_ns: int) -> Iterator[dict[str, Any]]:
    """Envelopes of ``source`` with ``since_ns <= recv < until_ns`` (closed .gz and open .jsonl)."""
    first_day, last_day = day_of(since_ns), day_of(max(since_ns, until_ns - 1))
    for d in sorted((Path(root) / "raw" / source).glob("date=*")):
        day = d.name.removeprefix("date=")
        if not first_day <= day <= last_day:
            continue
        for f in sorted(d.iterdir()):
            if f.name.endswith(".jsonl.gz"):
                data = gzip.decompress(f.read_bytes())
            elif f.name.endswith(".jsonl"):
                data = f.read_bytes()
            else:
                continue
            for line in data.splitlines():
                if not line.strip():
                    continue
                row = json.loads(line)
                recv = ns_from_iso(row["recv_ts"])
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


def forward_dq(root: Path, since_ns: int | None = None, until_ns: int | None = None,
               now_ns: int | None = None) -> dict[str, Any]:
    root = Path(root)
    start_ns = Meta.load(root).collector_start_ns
    until_ns = until_ns if until_ns is not None else (now_ns if now_ns is not None else time.time_ns())
    if since_ns is None:
        since_ns = start_ns if start_ns is not None else 0
    rows = {s: list(iter_rows(root, s, since_ns, until_ns)) for s in SOURCES}
    ok_rows = {s: [r for r in rs if r["ok"]] for s, rs in rows.items()}

    # coverage + gaps
    window_start = max(since_ns, start_ns or since_ns)
    coverage: dict[str, dict[str, dict[str, Any]]] = {}
    gaps: dict[str, list[dict[str, Any]]] = {}
    for s in SOURCES:
        per_day: dict[str, dict[str, Any]] = {}
        day = day_of(window_start)
        while day <= day_of(max(window_start, until_ns - 1)):
            d0 = day_start_s(day)
            a, b = max(d0, window_start / NS), min(d0 + 86400, until_ns / NS)
            per_day[day] = {"expected": expected_rows(s, a, b), "ok": 0, "errors": 0}
            day = day_of((d0 + 86400) * NS)
        for r in rows[s]:
            entry = per_day.setdefault(day_of(r["_recv_ns"]), {"expected": 0, "ok": 0, "errors": 0})
            entry["ok" if r["ok"] else "errors"] += 1
        for entry in per_day.values():
            entry["ratio"] = round(entry["ok"] / entry["expected"], 4) if entry["expected"] else None
        coverage[s] = per_day
        gaps[s] = _gaps(sorted(r["_recv_ns"] for r in ok_rows[s]), SOURCES[s].period_s)

    # paired skew (same pair_seq, both ok) and futures timing
    spot_by_pair = {r.get("pair_seq"): r for r in ok_rows["spot_ticker"]}
    pair_diff, last_lag, fut_server = [], [], []
    fut_counts, spot_counts = [], []
    fut_missing, fut_susp, spot_missing = Counter(), Counter(), Counter()
    fut_sanity, spot_sanity = _Sanity(), _Sanity()
    for r in ok_rows["fut_tickers"]:
        spot = spot_by_pair.get(r.get("pair_seq"))
        if spot is not None:
            pair_diff.append((r["_recv_ns"] - spot["_recv_ns"]) / MS)
        payload = r["payload"] or {}
        if payload.get("serverTime"):
            fut_server.append((r["_recv_ns"] - ns_from_iso(payload["serverTime"])) / MS)
        present = set()
        for t in payload.get("tickers") or []:
            sym = t.get("symbol")
            present.add(sym)
            if t.get("lastTime"):
                last_lag.append((r["_recv_ns"] - ns_from_iso(t["lastTime"])) / MS)
            if t.get("suspended"):
                fut_susp[sym] += 1
            fut_sanity.add(t.get("bid"), t.get("ask"))
        fut_counts.append(len(present & set(PERP_SYMBOLS)))
        fut_missing.update(s for s in PERP_SYMBOLS if s not in present)
    for r in ok_rows["spot_ticker"]:
        result = (r["payload"] or {}).get("result") or {}
        present = set()
        for key, q in result.items():
            pair = _spot_key_pair(key)
            if pair:
                present.add(pair)
            if isinstance(q, dict):
                spot_sanity.add((q.get("b") or [None])[0], (q.get("a") or [None])[0])
        spot_counts.append(len(present))
        spot_missing.update(p for p in SPOT_PAIRS if p not in present)
    spot_server = []
    for r in ok_rows["spot_time"]:
        unix = ((r["payload"] or {}).get("result") or {}).get("unixtime")
        if unix is not None:
            spot_server.append((r["_recv_ns"] - int(unix) * NS) / MS)

    # funding settlements since collector_start_ts only
    fund_from = max(start_ns or since_ns, since_ns)
    settled: dict[str, set[str]] = defaultdict(set)
    excluded = 0
    for r in ok_rows["fut_funding"]:
        sym = (r.get("params") or {}).get("symbol", "?")
        for row in (r["payload"] or {}).get("rates") or []:
            ts = row.get("timestamp")
            if not ts:
                continue
            t = ns_from_iso(ts)
            if t < fund_from:
                excluded += 1
            elif t < until_ns:
                settled[sym].add(ts)
    hour = 3600 * NS
    first_hour = -(-fund_from // hour) * hour
    expected_settlements = max(0, -(-(until_ns - first_hour) // hour))

    return {
        "window": {"since": iso_ns(since_ns), "until": iso_ns(until_ns),
                   "collector_start_ts": iso_ns(start_ns) if start_ns is not None else None},
        "paired_skew": {
            "pairs": len(pair_diff),
            "fut_minus_spot_recv_ms": _stats(pair_diff),
            "fut_recv_minus_lasttime_ms": _stats(last_lag),
            "note": "spot Ticker has no quote timestamp: spot skew is request/response time only",
        },
        "clock_skew": {"futures_recv_minus_server_ms": _stats(fut_server),
                       "spot_recv_minus_server_ms": _stats(spot_server)},
        "coverage": coverage,
        "funding_coverage": {
            "since": iso_ns(fund_from),
            "expected_settlements_per_symbol": expected_settlements,
            "symbols": {s: {"settlements": len(settled.get(s, ()))} for s in PERP_SYMBOLS},
            "rows_before_start_excluded": excluded,
        },
        "gaps": gaps,
        "asset_completeness": {
            "fut_tickers": _completeness(fut_counts, fut_missing, fut_susp),
            "spot_ticker": _completeness(spot_counts, spot_missing, None),
        },
        "quote_sanity": {"fut_tickers": fut_sanity.out(), "spot_ticker": spot_sanity.out()},
    }
