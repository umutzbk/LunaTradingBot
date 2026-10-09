"""V4 loaders (spec R1, R7, §3, §7.2, §8).

Historical (Stage 1): every loader is cut at ``V4_DATA_END`` (2026-07-01, start of the sealed
holdout) and passes a guard that raises ``HoldoutLeak`` if a later row reaches it. Bars in
``var/bars`` physically contain holdout rows (the paper bots need them), so they are filtered at
parquet read time (pyarrow filter: holdout rows are never materialised) and then guarded.
``var/history_v4`` files are written already cut by ``backfill-v4``: a leaked row there raises.

Forward (Stage 2): raw collector envelopes are streamed (``luna.forward.dq.iter_rows``) and only
envelopes with ``recv_ts >= collector_start_ts`` are read; funding rows whose period started before
``collector_start_ts`` are dropped. ``split_evidence`` separates warmup from evidence at
``V4_EVIDENCE_START_TS``. Snapshots are aggregated per UTC day while streaming (never held whole).
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from statistics import median
from typing import Any

import pandas as pd
import pyarrow.parquet as pq

from luna.forward.dq import _spot_key_pair, iter_rows
from luna.forward.envelope import NS, ns_from_iso
from luna.forward.meta import Meta
from luna.instruments import asset_from_perp, asset_from_spot, perp_symbol
from luna.v2.data import HoldoutLeak
from luna.v4.constants import BAR, DAY, V4_DATA_END, V4_EVIDENCE_START_TS, V4_UNIVERSE

# --- guards --------------------------------------------------------------------------------------


def guard_bars(df: pd.DataFrame, name: str, cutoff: pd.Timestamp = V4_DATA_END) -> pd.DataFrame:
    """Bars are labelled by open_ts; every bar must close by ``cutoff``."""
    if len(df) and df.index.max() > cutoff - BAR:
        raise HoldoutLeak(f"{name}: bar {df.index.max()} closes after {cutoff}")
    return df


def guard_series(df: pd.DataFrame, name: str, cutoff: pd.Timestamp = V4_DATA_END) -> pd.DataFrame:
    """Funding periods / spread samples must be stamped before ``cutoff``."""
    if len(df) and df.index.max() >= cutoff:
        raise HoldoutLeak(f"{name}: row {df.index.max()} at or after {cutoff}")
    return df


def _utc(df: pd.DataFrame) -> pd.DataFrame:
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    return df.sort_index()


def read_parquet_before(path: Path, bound: pd.Timestamp, op: str = "<") -> pd.DataFrame:
    """Rows with ``index <op> bound`` (``<`` or ``<=``), filtered inside the parquet reader."""
    cols = pq.read_schema(path).pandas_metadata.get("index_columns") or []
    name = cols[0] if cols and isinstance(cols[0], str) else "__index_level_0__"
    return _utc(pd.read_parquet(path, filters=[(name, op, bound)]))


# --- historical ----------------------------------------------------------------------------------

_OHLC = ["open", "high", "low", "close"]


def load_hist_bars(var_dir: Path, venue: str, asset: str, cutoff: pd.Timestamp = V4_DATA_END) -> pd.DataFrame:
    path = Path(var_dir) / "bars" / venue / f"{asset}.parquet"
    if not path.exists():
        from luna.data.store import _empty_bars

        return _empty_bars()
    return guard_bars(read_parquet_before(path, cutoff - BAR, "<="), f"{venue}/{asset}", cutoff)


def _hist(var_dir: Path, kind: str, asset: str, empty: pd.DataFrame) -> pd.DataFrame:
    path = Path(var_dir) / "history_v4" / kind / f"{asset}.parquet"
    return _utc(pd.read_parquet(path)) if path.exists() else empty


def _empty(cols: list[str]) -> pd.DataFrame:
    return pd.DataFrame({c: pd.Series(dtype="float64") for c in cols}, index=pd.DatetimeIndex([], tz="UTC"))


def load_hist_funding(var_dir: Path, asset: str) -> pd.DataFrame:
    df = _hist(var_dir, "funding", asset, _empty(["funding_abs", "funding_rel"]))
    return guard_series(df[["funding_abs", "funding_rel"]], f"funding/{asset}")


def load_hist_mark(var_dir: Path, asset: str) -> pd.DataFrame:
    return guard_bars(_hist(var_dir, "mark", asset, _empty(_OHLC)), f"mark/{asset}")


def load_hist_index(var_dir: Path, asset: str) -> pd.DataFrame:
    return guard_bars(_hist(var_dir, "index", asset, _empty(_OHLC)), f"index/{asset}")


def load_hist_spreads(var_dir: Path, asset: str) -> pd.DataFrame:
    return guard_series(_hist(var_dir, "spreads", asset, _empty(["spread_full"])), f"spreads/{asset}")


@dataclass
class HistoricalInputs:
    perp: dict[str, pd.DataFrame]
    spot: dict[str, pd.DataFrame]
    mark: dict[str, pd.DataFrame]
    index: dict[str, pd.DataFrame]
    funding: dict[str, pd.DataFrame]
    spreads: dict[str, pd.DataFrame]

    @property
    def assets(self) -> list[str]:
        return sorted(self.perp)


def load_historical(var_dir: Path, assets: list[str] | tuple[str, ...] = V4_UNIVERSE) -> HistoricalInputs:
    return HistoricalInputs(
        perp={a: load_hist_bars(var_dir, "kraken_futures", a) for a in assets},
        spot={a: load_hist_bars(var_dir, "kraken_spot", a) for a in assets},
        mark={a: load_hist_mark(var_dir, a) for a in assets},
        index={a: load_hist_index(var_dir, a) for a in assets},
        funding={a: load_hist_funding(var_dir, a) for a in assets},
        spreads={a: load_hist_spreads(var_dir, a) for a in assets},
    )


# --- forward -------------------------------------------------------------------------------------

FUNDING_SOURCES = ("fut_funding", "fut_funding_full")
SKEW_MAX_MS = 2000.0  # §7.2 proposed per-snapshot paired-skew threshold (fixed at freeze)
EXEC_WINDOW = pd.Timedelta(minutes=15)  # §3: no valid snapshot within 15 min -> decisions skipped


def collector_start(root: Path) -> pd.Timestamp:
    ns = Meta.load(Path(root)).collector_start_ns
    if ns is None:
        raise ValueError(f"collector_start_ts unknown ({Path(root) / 'meta.json'} missing or empty)")
    return pd.Timestamp(ns, unit="ns", tz="UTC")


def _ns(ts: pd.Timestamp) -> int:
    return int(pd.Timestamp(ts).value)


def iter_forward(root: Path, source: str, since: pd.Timestamp, until: pd.Timestamp):
    """Envelopes of ``source`` with max(since, collector_start) <= recv_ts < until."""
    lo = max(pd.Timestamp(since), collector_start(root))
    if lo >= until:
        return
    yield from iter_rows(Path(root), source, _ns(lo), _ns(until))


def load_forward_funding(
    root: Path,
    assets: list[str] | tuple[str, ...],
    until: pd.Timestamp,
    since: pd.Timestamp | None = None,
    periods_before: pd.Timestamp | None = None,
) -> dict[str, pd.DataFrame]:
    """Settled funding from the collector: rows of periods starting in [collector_start,
    periods_before (default until)), from envelopes received in [collector_start, until).
    Later receipts win on duplicates."""
    start = collector_start(root)
    lo, hi = _ns(start), _ns(periods_before if periods_before is not None else until)
    syms = {perp_symbol(a): a for a in assets}
    rows: dict[str, dict[int, tuple[int, float, float]]] = {a: {} for a in assets}
    for source in FUNDING_SOURCES:
        for env in iter_forward(root, source, since if since is not None else start, until):
            if not env.get("ok"):
                continue
            asset = syms.get((env.get("params") or {}).get("symbol"))
            if asset is None:
                continue
            for r in (env.get("payload") or {}).get("rates") or []:
                try:
                    t = ns_from_iso(r["timestamp"])
                    a, rel = float(r["fundingRate"]), float(r["relativeFundingRate"])
                except (KeyError, TypeError, ValueError):
                    continue
                if lo <= t < hi:
                    prev = rows[asset].get(t)
                    if prev is None or env["_recv_ns"] >= prev[0]:
                        rows[asset][t] = (env["_recv_ns"], a, rel)
    out = {}
    for asset, by_t in rows.items():
        ts = sorted(by_t)
        out[asset] = pd.DataFrame(
            {"funding_abs": [by_t[t][1] for t in ts], "funding_rel": [by_t[t][2] for t in ts]},
            index=pd.DatetimeIndex(pd.to_datetime(ts, unit="ns", utc=True)),
        )
    return out


def split_evidence(df: pd.DataFrame, start: pd.Timestamp = V4_EVIDENCE_START_TS):
    """(warmup, evidence): rows stamped before / at-or-after ``V4_EVIDENCE_START_TS``."""
    return df[df.index < start], df[df.index >= start]


def _f(x: Any) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _quote_ok(bid: float | None, ask: float | None) -> bool:
    return bid is not None and ask is not None and 0 < bid <= ask


@dataclass
class ForwardDaily:
    """Per-(UTC day, asset) aggregates of the paired ticker snapshots.

    exec: the first skew-valid snapshot in [day 00:00, +15 min): mids, half spreads, mark, volumes.
    daily: basis median / count over the day's skew-valid snapshots, mark / mid extremes.
    quality: per day snapshot counts, skew-valid pairs, longest ticker gap, clock-skew median.
    """

    exec: pd.DataFrame
    daily: pd.DataFrame
    quality: pd.DataFrame
    listing: dict[str, pd.Timestamp] = field(default_factory=dict)


def _spot_quotes(payload: dict) -> dict[str, dict[str, float | None]]:
    out = {}
    for key, q in ((payload or {}).get("result") or {}).items():
        pair = _spot_key_pair(key)
        if pair is None or not isinstance(q, dict):
            continue
        bid, ask = _f((q.get("b") or [None])[0]), _f((q.get("a") or [None])[0])
        v24, p24 = _f((q.get("v") or [None, None])[1]), _f((q.get("p") or [None, None])[1])
        out[asset_from_spot(pair)] = {"bid": bid, "ask": ask,
                                      "vol_usd": v24 * p24 if v24 is not None and p24 is not None else None}
    return out


def load_forward_daily(
    root: Path, assets: list[str] | tuple[str, ...], since: pd.Timestamp, until: pd.Timestamp
) -> ForwardDaily:
    """Stream ``fut_tickers`` / ``spot_ticker`` (paired by ``pair_seq``) one day at a time."""
    wanted = set(assets)
    exec_rows: dict[tuple, dict] = {}
    day_acc: dict[tuple, dict] = {}
    quality = []
    day = pd.Timestamp(since).floor("D")
    while day < until:
        hi = min(day + DAY, pd.Timestamp(until))
        spot: dict[Any, tuple[int, dict]] = {}
        pad = pd.Timedelta(seconds=10)
        for env in iter_forward(root, "spot_ticker", day - pad, hi + pad):
            if env.get("ok") and env.get("pair_seq") is not None:
                spot[env["pair_seq"]] = (env["_recv_ns"], _spot_quotes(env.get("payload")))
        times: list[int] = []
        server_skew: list[float] = []
        pairs = pairs_valid = 0
        for env in iter_forward(root, "fut_tickers", day, hi):
            if not env.get("ok"):
                continue
            recv = env["_recv_ns"]
            times.append(recv)
            payload = env.get("payload") or {}
            if payload.get("serverTime"):
                try:
                    server_skew.append((recv - ns_from_iso(payload["serverTime"])) / 1e6)
                except ValueError:
                    pass
            sp = spot.get(env.get("pair_seq"))
            if sp is None:
                continue
            pairs += 1
            valid = abs(recv - sp[0]) / 1e6 <= SKEW_MAX_MS
            pairs_valid += valid
            ts = pd.Timestamp(recv, unit="ns", tz="UTC")
            for t in payload.get("tickers") or []:
                sym = t.get("symbol") or ""
                if not sym.startswith("PF_"):
                    continue
                asset = asset_from_perp(sym)
                if asset not in wanted:
                    continue
                s = sp[1].get(asset)
                pb, pa, mark = _f(t.get("bid")), _f(t.get("ask")), _f(t.get("markPrice"))
                acc = day_acc.setdefault((day, asset), {"basis": [], "mark": [], "perp": [], "spot": []})
                if mark is not None:
                    acc["mark"].append(mark)
                if not (valid and s is not None and _quote_ok(pb, pa) and _quote_ok(s["bid"], s["ask"])):
                    continue
                pm, sm = (pb + pa) / 2, (s["bid"] + s["ask"]) / 2
                acc["basis"].append(pm / sm - 1.0)
                acc["perp"].append(pm)
                acc["spot"].append(sm)
                if ts < day + EXEC_WINDOW and (day, asset) not in exec_rows:
                    exec_rows[(day, asset)] = {
                        "ts": ts, "perp_mid": pm, "perp_half_spread": (pa - pb) / 2 / pm,
                        "spot_mid": sm, "spot_half_spread": (s["ask"] - s["bid"]) / 2 / sm,
                        "mark": mark, "perp_vol_quote": _f(t.get("volumeQuote")),
                        "spot_vol_usd": s["vol_usd"], "suspended": bool(t.get("suspended")),
                    }
        times.sort()
        gap = max((b - a for a, b in zip(times, times[1:], strict=False)), default=None)
        quality.append({"day": day, "fut_ok": len(times), "pairs": pairs, "pairs_valid": pairs_valid,
                        "max_gap_s": gap / NS if gap is not None else None,
                        "clock_skew_median_ms": median(server_skew) if server_skew else None})
        day += DAY
    daily = {
        k: {"basis_median": median(v["basis"]) if v["basis"] else float("nan"), "basis_n": len(v["basis"]),
            "mark_max": max(v["mark"]) if v["mark"] else float("nan"),
            "mark_min": min(v["mark"]) if v["mark"] else float("nan"),
            "perp_max": max(v["perp"]) if v["perp"] else float("nan"),
            "perp_min": min(v["perp"]) if v["perp"] else float("nan"),
            "spot_min": min(v["spot"]) if v["spot"] else float("nan")}
        for k, v in day_acc.items()
    }
    return ForwardDaily(
        exec=_frame(exec_rows), daily=_frame(daily),
        quality=pd.DataFrame(quality).set_index("day") if quality else pd.DataFrame(),
        listing=load_forward_listing(root, assets, until),
    )


def _frame(rows: dict[tuple, dict]) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame(index=pd.MultiIndex.from_tuples([], names=["day", "asset"]))
    df = pd.DataFrame.from_dict(rows, orient="index")
    df.index = pd.MultiIndex.from_tuples(df.index, names=["day", "asset"])
    return df.sort_index()


def load_forward_listing(root: Path, assets, until: pd.Timestamp) -> dict[str, pd.Timestamp]:
    """Listing date per asset from the collector's ``fut_instruments`` (``openingDate``), else the
    first receipt that listed the perp (conservative: age counts from when the collector saw it)."""
    out: dict[str, pd.Timestamp] = {}
    syms = {perp_symbol(a): a for a in assets}
    for env in iter_forward(root, "fut_instruments", collector_start(root), until):
        if not env.get("ok"):
            continue
        for ins in (env.get("payload") or {}).get("instruments") or []:
            a = syms.get(ins.get("symbol"))
            if a is None or a in out:
                continue
            seen = pd.Timestamp(env["_recv_ns"], unit="ns", tz="UTC")
            od = ins.get("openingDate")
            try:
                out[a] = pd.Timestamp(od).tz_convert("UTC") if od else seen
            except (TypeError, ValueError):
                out[a] = seen
    return out


def funding_hours_by_day(funding: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Settled funding periods per (day, asset) (DQ: 24/24 expected)."""
    counts: dict[tuple, int] = defaultdict(int)
    for a, df in funding.items():
        for t in df.index:
            counts[(t.floor("D"), a)] += 1
    return _frame({k: {"hours": v} for k, v in counts.items()})
