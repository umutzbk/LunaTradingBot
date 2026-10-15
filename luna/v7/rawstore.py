"""The real-scale raw store and its read-only lazy loaders (spec §3 "All raw files are immutable once written.
Each table has a manifest with per-file sha256 ... Time units are normalized to ns at ingest", S-1.7
"read-only loaders", §17, DEC-33; SC-01 part 1, SC-03, red-team M1 / M3; AMB-36).

Layout (``RawStore(root)``; one directory per table, one file per (symbol, calendar month))::

    <root>/<table dir>/MANIFEST.json                     table manifest: per-symbol manifest sha256s
    <root>/<table dir>/<SYMBOL>/MANIFEST.json            symbol manifest: per-file sha256, rows, provenance
    <root>/<table dir>/<SYMBOL>/<SYMBOL>-<YYYY-MM>.parquet

Tables (``TABLES``): R3 ``bars_last_15m``, R5 ``bars_last_1m``, R4 ``bars_{mark,index,premium}_15m`` and the
two R2 sources ``funding_archive`` / ``funding_rest`` (merged by ``data.funding.merge`` at load). A chunk
holds exactly the R-table columns (bars: ``open_ts`` int64 ns, float64 prices / volumes, int64
``n_trades``; R4: ``open_ts``, o, h, l, c; funding: ``funding_ts``, ``rate``, nullable
``interval_h_explicit``, ``mark_at_settle``), sorted by the event time, strictly increasing, every row
inside the chunk's month.
``known_at`` is not stored: it is the fixed-offset archive rule (``data.schema.known_at``, open_ts + 15 min /
1 min) applied at load (and checked equal at write when the caller supplies it); merged funding gets it from
``data.funding.merge``. Format: parquet, zstd level 9 (``FORMAT``).

Write once (``put_chunk``): K9 log required; the store bound (the fetch layer's N1 bound: < 2025-07-01
before the opening, < the W_HOLD end with the verified authorization) and ``guard.check_data_span`` first;
the file is written under a temporary name, fsynced and renamed; the symbol manifest then the table manifest
are replaced atomically. An existing chunk is never rewritten: identical content (``content_sha256``) is a
no-op, different content an ``ImmutabilityViolation`` (K9-logged, "§3 ... immutable"). An orphan file (a crash
between the file and its manifest) is adopted only when its content is identical. The table manifest's sha256
is the DT4 data-manifest hash (``manifest_sha256s``); it covers every file through the symbol manifests.

Read only (``TableLoader`` / ``FundingLoader``): nothing is read until asked; each request and each chunk
period pass ``guard.check_data_span`` (W_HOLD without the verified authorization, M6 before R_V6: refused and
K9-logged) before a file is opened ``"rb"``; the file's sha256 must equal its manifest entry (else
``IntegrityBlocker``, K9-logged). Rows are located with ``searchsorted`` on the sorted chunk ts arrays; a
window touches only the chunks it overlaps and copies only its rows. Decoded chunks sit in a ``ChunkCache``:
LRU with a fixed byte budget (an implementation parameter, ``DEFAULT_CACHE_BYTES``), deterministic eviction.

``RunStore``: executed ``BookRun`` objects persisted immutably under their output sha256
(``runrecord.run_output_sha256``), file sha256 checked before unpickling and the output sha256 recomputed
after, so a later (opening) process can pass the pinned W_DEV ML* run to ``runner_select.holdout_inputs``
without a re-run. ``StoreSource``: the loader-backed ``runner.UniverseSource``.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import pickle
import re
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from luna.v7 import guard, k9, netguard
from luna.v7.book.engine import BookRun
from luna.v7.constants import FROZEN
from luna.v7.data import funding as fund_mod
from luna.v7.data.instances import Instance
from luna.v7.data.notices import NoticeVersion
from luna.v7.data.schema import KNOWN_AT_OFFSET_NS, IntegrityBlocker
from luna.v7.data.terminal import TerminalEvent
from luna.v7.features import R4Bars
from luna.v7.runner import SymbolInputs
from luna.v7.runrecord import run_output_sha256
from luna.v7.timeutil import ns_array
from luna.v7.universe import Identity

__all__ = ["DEFAULT_CACHE_BYTES", "FORMAT", "TABLES", "ChunkCache", "FundingLoader", "ImmutabilityViolation",
           "RawStore", "RunStore", "StoreSource", "StoreTable", "SymbolMeta", "TableLoader", "content_sha256",
           "month_period", "months_between"]

DEFAULT_CACHE_BYTES = 512 * 2**20  # implementation parameter (AMB-36): decoded-chunk LRU budget per cache
FORMAT = "parquet/zstd-9"
MANIFEST = "MANIFEST.json"
RULE_IMMUTABLE = "§3 raw files immutable once written (integrity)"
RULE_INTEGRITY = "§3 raw file sha256 differs from its manifest (integrity)"
_SYMBOL = re.compile(r"[A-Z0-9_]{1,40}\Z", re.ASCII)
_MONTH = re.compile(r"(\d{4})-(\d{2})\Z", re.ASCII)
_SHA = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)


class ImmutabilityViolation(IntegrityBlocker):
    """A written raw file would get different content (§3: immutable once written); refused, K9-logged."""


# ------------------------------------------------------------------------------------------------ tables


@dataclass(frozen=True)
class StoreTable:
    """One stored table: its directory, event-time column, stored (name, dtype) columns (ts first), the
    fixed known_at offset of the archive rule (None: set by the funding merge) and its N1 dataset."""

    key: str
    dirname: str
    ts_col: str
    columns: tuple[tuple[str, str], ...]
    known_at_ns: int | None
    dataset: str | None
    interval: str | None
    daily_union: bool  # DEC-33 monthly union daily (R3 / R5 only)

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(n for n, _ in self.columns)


_BAR = (("open_ts", "int64"), ("o", "float64"), ("h", "float64"), ("l", "float64"), ("c", "float64"),
        ("vol_base", "float64"), ("vol_quote", "float64"), ("n_trades", "int64"),
        ("taker_buy_quote", "float64"))
_OHLC = _BAR[:5]
_FUND = (("funding_ts", "int64"), ("rate", "float64"), ("interval_h_explicit", "Int64"),
         ("mark_at_settle", "float64"))

TABLES: dict[str, StoreTable] = {
    "R3": StoreTable("R3", "bars_last_15m", "open_ts", _BAR, KNOWN_AT_OFFSET_NS["R3"], "klines", "15m", True),
    "R5": StoreTable("R5", "bars_last_1m", "open_ts", _BAR, KNOWN_AT_OFFSET_NS["R5"], "klines", "1m", True),
    "R4_mark": StoreTable("R4_mark", "bars_mark_15m", "open_ts", _OHLC, KNOWN_AT_OFFSET_NS["R4"],
                          "markPriceKlines", "15m", False),
    "R4_index": StoreTable("R4_index", "bars_index_15m", "open_ts", _OHLC, KNOWN_AT_OFFSET_NS["R4"],
                           "indexPriceKlines", "15m", False),
    "R4_premium": StoreTable("R4_premium", "bars_premium_15m", "open_ts", _OHLC, KNOWN_AT_OFFSET_NS["R4"],
                             "premiumIndexKlines", "15m", False),
    "R2_archive": StoreTable("R2_archive", "funding_archive", "funding_ts", _FUND, None, "fundingRate", None,
                             False),
    "R2_rest": StoreTable("R2_rest", "funding_rest", "funding_ts", _FUND, None, None, None, False),
}


def _table(table: str) -> StoreTable:
    if table not in TABLES:
        raise ValueError(f"unknown raw table {table!r} (one of {sorted(TABLES)})")
    return TABLES[table]


def _symbol(symbol: str) -> str:
    if not isinstance(symbol, str) or not _SYMBOL.match(symbol):
        raise ValueError(f"invalid symbol {symbol!r}")
    return symbol


def month_period(month: str) -> tuple[int, int]:
    """Half-open ns period of a calendar month ``YYYY-MM`` (UTC)."""
    m = _MONTH.match(month) if isinstance(month, str) else None
    if not m or not 1 <= int(m.group(2)) <= 12:
        raise ValueError(f"invalid month {month!r} (YYYY-MM)")
    lo = pd.Timestamp(year=int(m.group(1)), month=int(m.group(2)), day=1, tz="UTC")
    return int(lo.value), int((lo + pd.DateOffset(months=1)).value)


def months_between(lo: int, hi: int) -> list[str]:
    """The calendar months overlapping [lo, hi)."""
    if hi <= lo:
        return []
    cur = pd.Timestamp(int(lo), unit="ns", tz="UTC").tz_localize(None).normalize().replace(day=1)
    out: list[str] = []
    while int(cur.tz_localize("UTC").value) < hi:
        out.append(cur.strftime("%Y-%m"))
        cur = cur + pd.DateOffset(months=1)
    return out


# ----------------------------------------------------------------------------------------- content


def _columns(t: StoreTable, df: pd.DataFrame) -> dict[str, object]:
    """The stored columns of ``df`` (exactly the table's; known_at, if present, must follow the rule)."""
    miss = [c for c in t.names if c not in df.columns]
    if miss:
        raise IntegrityBlocker(f"{t.key}: required columns missing {miss}")
    out: dict[str, object] = {}
    for name, dt in t.columns:
        s = df[name]
        if name == t.ts_col:
            out[name] = ns_array(s.to_numpy(), f"{t.key}.{name}") if len(s) else np.empty(0, np.int64)
        elif dt == "int64":
            v = pd.to_numeric(s)
            if v.isna().any() or (v.astype("float64") != v.astype("float64").round()).any():
                raise IntegrityBlocker(f"{t.key}.{name}: missing or non-integer values")
            out[name] = v.to_numpy(np.int64)
        elif dt == "float64":
            out[name] = pd.to_numeric(s).to_numpy(np.float64)
        else:  # nullable integer
            out[name] = pd.array(pd.to_numeric(s), dtype="Int64")
    if "known_at" in df.columns and t.known_at_ns is not None and len(df):
        ka = ns_array(df["known_at"].to_numpy(), f"{t.key}.known_at")
        if not (ka == out[t.ts_col] + t.known_at_ns).all():  # type: ignore[operator]
            raise IntegrityBlocker(f"{t.key}: known_at differs from the archive rule {t.ts_col} + "
                                   f"{t.known_at_ns // 10**9} s (it is not stored; refusing to lose it)")
    return out


def content_sha256(t: StoreTable, cols: Mapping[str, object]) -> str:
    """sha256 of a chunk's content (table, column names / dtypes, values; NaN canonical), independent of the
    file encoding (a re-encoding by another pyarrow version is not a content change)."""
    h = hashlib.sha256(f"{t.key}\n".encode())
    for name, dt in t.columns:
        a = cols[name]
        h.update(f"{name}:{dt}:{len(a)}\n".encode())  # type: ignore[arg-type]
        if dt == "Int64":
            ia = pd.array(a, dtype="Int64")
            mask = np.asarray(ia.isna())
            h.update(mask.tobytes())
            h.update(np.asarray(ia.fillna(0), dtype="<i8").tobytes())
        elif dt == "float64":
            v = np.array(a, dtype="<f8")
            v[np.isnan(v)] = np.nan
            h.update(v.tobytes())
        else:
            h.update(np.asarray(a, dtype="<i8").tobytes())
    return h.hexdigest()


def _encode(t: StoreTable, cols: Mapping[str, object]) -> bytes:
    tbl = pa.table({n: pa.array(cols[n], type=pa.int64() if dt in ("int64", "Int64") else pa.float64())
                    for n, dt in t.columns})
    buf = io.BytesIO()
    pq.write_table(tbl, buf, compression="zstd", compression_level=9)
    return buf.getvalue()


def _decode(t: StoreTable, data: bytes) -> dict[str, object]:
    tbl = pq.read_table(io.BytesIO(data))
    if tuple(tbl.column_names) != t.names:
        raise IntegrityBlocker(f"{t.key}: stored columns {tbl.column_names} differ from {list(t.names)}")
    out: dict[str, object] = {}
    for name, dt in t.columns:
        col = tbl.column(name)
        if dt == "Int64":
            out[name] = pd.array(col.to_pandas(types_mapper={pa.int64(): pd.Int64Dtype()}.get), dtype="Int64")
        else:
            if col.null_count:
                raise IntegrityBlocker(f"{t.key}.{name}: null values in a non-nullable column")
            out[name] = np.array(col.to_numpy(), dtype=np.int64 if dt == "int64" else np.float64, copy=True)
    return out


def _nbytes(cols: Mapping[str, object]) -> int:
    return int(sum(int(getattr(a, "nbytes", 0)) for a in cols.values()))


def _canonical(obj: object) -> bytes:
    return (json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode()


def _sha_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _replace_atomic(path: Path, data: bytes) -> None:
    """Write ``data`` to a temporary sibling, fsync, then replace ``path`` (manifests only)."""
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _create_once(path: Path, data: bytes) -> None:
    """Write-once: a temporary sibling, fsync, then rename to ``path`` (which must not exist)."""
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    if path.exists():
        tmp.unlink()
        raise ImmutabilityViolation(f"{path} already exists: raw files are written once")
    os.replace(tmp, path)


# ------------------------------------------------------------------------------------------------ store


class RawStore:
    """The raw store under ``root`` (module docstring). Writing is ``put_chunk`` (the ingestion's only
    write path); reading goes through ``TableLoader`` / ``FundingLoader``."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    # paths
    def table_dir(self, table: str) -> Path:
        return self.root / _table(table).dirname

    def chunk_path(self, table: str, symbol: str, month: str) -> Path:
        month_period(month)
        return self.table_dir(table) / _symbol(symbol) / f"{symbol}-{month}.parquet"

    def _symbol_manifest_path(self, table: str, symbol: str) -> Path:
        return self.table_dir(table) / _symbol(symbol) / MANIFEST

    # manifests
    def table_manifest(self, table: str) -> dict | None:
        p = self.table_dir(table) / MANIFEST
        return json.loads(p.read_bytes()) if p.exists() else None

    def manifest_sha256(self, table: str) -> str | None:
        p = self.table_dir(table) / MANIFEST
        return _sha_bytes(p.read_bytes()) if p.exists() else None

    def manifest_sha256s(self) -> dict[str, str]:
        """{table: table-manifest sha256} of every written table (DT4: ``runrecord.run_record``
        ``data_manifests``)."""
        return {k: s for k in sorted(TABLES) if (s := self.manifest_sha256(k)) is not None}

    def symbol_manifest(self, table: str, symbol: str) -> dict | None:
        p = self._symbol_manifest_path(table, symbol)
        return json.loads(p.read_bytes()) if p.exists() else None

    def entry(self, table: str, symbol: str, month: str) -> dict | None:
        month_period(month)
        sm = self.symbol_manifest(table, symbol)
        return None if sm is None else sm["chunks"].get(month)

    def verify_chunk(self, table: str, symbol: str, month: str) -> dict:
        """The manifest entry of a written chunk whose file still has the recorded sha256 (else
        ``IntegrityBlocker``, K9-logged; a missing entry is a KeyError)."""
        k9.require()
        e = self.entry(table, symbol, month)
        if e is None:
            raise KeyError(f"{table} {symbol} {month}: no such chunk")
        p = self.chunk_path(table, symbol, month)
        got = _sha_bytes(p.read_bytes()) if p.exists() else None
        if got != e["sha256"]:
            _integrity(f"{p}: sha256 {got} differs from the manifest {e['sha256']}", p)
        return e

    # the write path
    def put_chunk(self, table: str, symbol: str, month: str, df: pd.DataFrame, *, sources: Sequence[Mapping],
                  auth: guard.HoldoutAuthorization | None = None) -> dict:
        """Write one (table, symbol, month) chunk once (module docstring); returns its manifest entry."""
        t = _table(table)
        _symbol(symbol)
        lo, hi = month_period(month)
        k9.require()
        what = f"raw store {t.key} {symbol} {month}"
        bound = netguard.bounds(auth).n1_end_ns
        if hi > bound:
            e = guard.HoldoutBlocked(f"{what}: period ends after the store bound {bound} (the fetch layer's "
                                     "N1 bound): not stored")
            k9.log_refusal(e, request={"kind": "raw_store_write", "table": t.key, "symbol": symbol,
                                       "month": month}, rule=guard.k9_rule(e))
            raise e
        guard.check_data_span(lo, hi, auth=auth, what=what)
        cols = _columns(t, df)
        ts = cols[t.ts_col]
        assert isinstance(ts, np.ndarray)
        if len(ts) and not (np.diff(ts) > 0).all():
            raise IntegrityBlocker(f"{what}: {t.ts_col} not sorted strictly increasing (duplicates / order)")
        if len(ts) and (ts[0] < lo or ts[-1] >= hi):
            raise IntegrityBlocker(f"{what}: rows outside the chunk period [{lo}, {hi}) (period)")
        csha = content_sha256(t, cols)
        path = self.chunk_path(table, symbol, month)
        req = {"kind": "raw_store_write", "table": t.key, "symbol": symbol, "month": month}
        old = self.entry(table, symbol, month)
        if old is not None:
            if old["content_sha256"] != csha:
                _immutable(f"{what}: written with content {old['content_sha256']}, refusing {csha}", req)
            if not path.exists():  # a lost file: re-created with identical content (manifest unchanged)
                data = _encode(t, cols)
                if _sha_bytes(data) != old["sha256"]:
                    _immutable(f"{what}: re-encoded file differs from the manifest sha256", req)
                path.parent.mkdir(parents=True, exist_ok=True)
                _create_once(path, data)
            self._sync_table(t, symbol)  # heals a crash between the symbol and the table manifest
            return self.verify_chunk(table, symbol, month)
        if path.exists():  # orphan (crash before its manifest entry): adopted only when identical
            data = path.read_bytes()
            try:
                same = content_sha256(t, _decode(t, data)) == csha
            except (IntegrityBlocker, pa.ArrowException, ValueError):
                same = False
            if not same:
                _immutable(f"{what}: an unrecorded file with different content exists", req)
        else:
            data = _encode(t, cols)
            path.parent.mkdir(parents=True, exist_ok=True)
            _create_once(path, data)
        entry = {"file": path.name, "sha256": _sha_bytes(data), "bytes": len(data), "content_sha256": csha,
                 "rows": int(len(ts)), "period": [lo, hi],
                 "span": [int(ts[0]), int(ts[-1]) + 1] if len(ts) else None,
                 "sources": [dict(s) for s in sources]}
        self._record(t, symbol, month, entry)
        return entry

    def _record(self, t: StoreTable, symbol: str, month: str, entry: dict) -> None:
        sm = self.symbol_manifest(t.key, symbol) or {"kind": "V7_RAW_SYMBOL", "table": t.key,
                                                     "symbol": symbol, "chunks": {}}
        sm["chunks"][month] = json.loads(_canonical(entry))
        sm["chunks"] = dict(sorted(sm["chunks"].items()))
        _replace_atomic(self._symbol_manifest_path(t.key, symbol), _canonical(sm))
        self._sync_table(t, symbol)

    def _sync_table(self, t: StoreTable, symbol: str) -> None:
        """The table manifest's entry of ``symbol`` from its symbol manifest (rewritten only if changed)."""
        sdata = self._symbol_manifest_path(t.key, symbol).read_bytes()
        sm = json.loads(sdata)
        tm = self.table_manifest(t.key) or {
            "kind": "V7_RAW_TABLE", "table": t.key, "name": t.dirname, "format": FORMAT,
            "columns": [list(c) for c in t.columns], "ts_col": t.ts_col, "known_at_offset_ns": t.known_at_ns,
            "symbols": {}}
        want = {"manifest_sha256": _sha_bytes(sdata), "chunks": len(sm["chunks"]),
                "rows": int(sum(c["rows"] for c in sm["chunks"].values()))}
        if tm["symbols"].get(symbol) == want:
            return
        tm["symbols"][symbol] = want
        tm["symbols"] = dict(sorted(tm["symbols"].items()))
        _replace_atomic(self.table_dir(t.key) / MANIFEST, _canonical(tm))


def _immutable(msg: str, request: Mapping) -> None:
    e = ImmutabilityViolation(msg)
    k9.log_refusal(e, request=request, rule=RULE_IMMUTABLE)
    raise e


def _integrity(msg: str, path: Path) -> None:
    e = IntegrityBlocker(msg)
    k9.log_refusal(e, request={"kind": "raw_store_read", "path": str(path)}, rule=RULE_INTEGRITY)
    raise e


# ------------------------------------------------------------------------------------------------ cache


class ChunkCache:
    """LRU of decoded chunks with a fixed byte budget (``budget_bytes`` > 0; an implementation parameter).
    Eviction is deterministic: least recently used first (access order is total, so there are no ties); an
    item larger than the whole budget is returned to the caller but never cached. Shareable by loaders."""

    def __init__(self, budget_bytes: int = DEFAULT_CACHE_BYTES) -> None:
        if isinstance(budget_bytes, bool) or not isinstance(budget_bytes, int) or budget_bytes <= 0:
            raise ValueError(f"budget_bytes must be a positive int, got {budget_bytes!r}")
        self.budget = budget_bytes
        self.bytes = 0
        self._items: OrderedDict[tuple, tuple[dict, int]] = OrderedDict()
        self.stats = {"hits": 0, "misses": 0, "evictions": 0}

    def get(self, key: tuple) -> dict | None:
        if key in self._items:
            self._items.move_to_end(key)
            self.stats["hits"] += 1
            return self._items[key][0]
        self.stats["misses"] += 1
        return None

    def put(self, key: tuple, value: dict, nbytes: int) -> None:
        if key in self._items:
            self.bytes -= self._items.pop(key)[1]
        if nbytes > self.budget:
            return
        self._items[key] = (value, int(nbytes))
        self.bytes += int(nbytes)
        while self.bytes > self.budget:
            _, (_, b) = self._items.popitem(last=False)
            self.bytes -= b
            self.stats["evictions"] += 1

    def keys(self) -> tuple[tuple, ...]:
        return tuple(self._items)


# ----------------------------------------------------------------------------------------------- loaders


def _empty(t: StoreTable, names: Sequence[str]) -> dict[str, object]:
    return {n: (pd.array([], dtype="Int64") if dt == "Int64" else np.empty(0, np.int64 if dt == "int64"
                                                                           else np.float64))
            for n, dt in t.columns if n in names}


def _concat(parts: list) -> object:
    if isinstance(parts[0], np.ndarray):
        return np.concatenate(parts)
    return pd.array(pd.concat([pd.Series(p, dtype="Int64") for p in parts], ignore_index=True), dtype="Int64")


class TableLoader:
    """Read-only lazy loader of one table (module docstring). ``auth``: the hardened holdout authorization
    (None before the opening); ``cache``: a shared ``ChunkCache`` (default: its own of ``budget_bytes``);
    ``manifest_sha256``: when given, the table manifest must have this hash (e.g. the one recorded in DT4).
    The table manifest is read at construction, a symbol's manifest when the symbol is first used (its
    sha256 checked against the table manifest), a chunk when a window first needs it."""

    def __init__(self, store: RawStore, table: str, *, auth: guard.HoldoutAuthorization | None = None,
                 cache: ChunkCache | None = None, budget_bytes: int = DEFAULT_CACHE_BYTES,
                 manifest_sha256: str | None = None) -> None:
        k9.require()
        self.store, self.t, self.auth = store, _table(table), auth
        self.cache = cache if cache is not None else ChunkCache(budget_bytes)
        p = store.table_dir(table) / MANIFEST
        data = p.read_bytes() if p.exists() else None
        self.manifest_sha256 = None if data is None else _sha_bytes(data)
        if manifest_sha256 is not None and manifest_sha256 != self.manifest_sha256:
            raise IntegrityBlocker(f"{table}: table manifest sha256 {self.manifest_sha256} is not the "
                                   f"expected {manifest_sha256}")
        tm = json.loads(data) if data is not None else {"symbols": {}}
        if data is not None and (tm.get("table") != self.t.key or tm.get("format") != FORMAT
                                 or tm.get("columns") != [list(c) for c in self.t.columns]):
            raise IntegrityBlocker(f"{table}: table manifest does not describe this table / format")
        self._symbols: dict[str, dict] = tm["symbols"]
        self._index: dict[str, tuple[list[str], np.ndarray, np.ndarray, list[dict]]] = {}
        self.stats = {"decoded_chunks": 0, "bytes_read": 0}

    def symbols(self) -> tuple[str, ...]:
        return tuple(sorted(self._symbols))

    def rows(self, symbol: str) -> int:
        return int(self._symbols[symbol]["rows"]) if symbol in self._symbols else 0

    def _chunks(self, symbol: str) -> tuple[list[str], np.ndarray, np.ndarray, list[dict]]:
        if symbol not in self._index:
            p = self.store._symbol_manifest_path(self.t.key, symbol)
            data = p.read_bytes()
            if _sha_bytes(data) != self._symbols[symbol]["manifest_sha256"]:
                _integrity(f"{p}: symbol manifest sha256 differs from the table manifest", p)
            sm = json.loads(data)
            months = sorted(sm["chunks"])
            ents = [sm["chunks"][m] for m in months]
            los = np.array([e["period"][0] for e in ents], dtype=np.int64)
            his = np.array([e["period"][1] for e in ents], dtype=np.int64)
            self._index[symbol] = (months, los, his, ents)
        return self._index[symbol]

    def _chunk(self, symbol: str, month: str, e: dict) -> dict:
        key = (str(self.store.root), self.t.key, symbol, month)
        got = self.cache.get(key)
        if got is not None:
            return got
        guard.check_data_span(int(e["period"][0]), int(e["period"][1]), auth=self.auth,
                              what=f"raw chunk {self.t.key} {symbol} {month}")
        p = self.store.chunk_path(self.t.key, symbol, month)
        with open(p, "rb") as f:  # read-only
            data = f.read()
        if _sha_bytes(data) != e["sha256"]:
            _integrity(f"{p}: sha256 {_sha_bytes(data)} differs from the manifest {e['sha256']}", p)
        cols = _decode(self.t, data)
        if len(cols[self.t.ts_col]) != int(e["rows"]):  # type: ignore[arg-type]
            _integrity(f"{p}: {len(cols[self.t.ts_col])} rows, manifest {e['rows']}", p)  # type: ignore[arg-type]
        self.stats["decoded_chunks"] += 1
        self.stats["bytes_read"] += len(data)
        self.cache.put(key, cols, _nbytes(cols))
        return cols

    def arrays(self, symbol: str, lo: int, hi: int,
               columns: Sequence[str] | None = None) -> dict[str, object]:
        """The rows of ``symbol`` with lo <= event time < hi, sorted: {column: array} (the ts column, the
        requested stored columns (default all) and, for bars, ``known_at``). Guarded (module docstring)."""
        lo, hi = int(lo), int(hi)
        if hi <= lo:
            raise ValueError(f"empty window [{lo}, {hi})")
        ts_col = self.t.ts_col
        names = self.t.names if columns is None else (ts_col, *[c for c in columns if c != ts_col])
        bad = [c for c in names if c not in self.t.names]
        if bad:
            raise ValueError(f"{self.t.key}: unknown columns {bad}")
        guard.check_data_span(lo, hi, auth=self.auth, what=f"raw {self.t.key} {symbol}")
        parts: dict[str, list] = {n: [] for n in names}
        if symbol in self._symbols:
            months, los, his, ents = self._chunks(symbol)
            i0 = int(np.searchsorted(his, lo, "right"))
            i1 = int(np.searchsorted(los, hi, "left"))
            for i in range(i0, i1):
                cols = self._chunk(symbol, months[i], ents[i])
                ts = cols[self.t.ts_col]
                a = int(np.searchsorted(ts, lo, "left"))  # type: ignore[call-overload]
                b = int(np.searchsorted(ts, hi, "left"))  # type: ignore[call-overload]
                if b > a:
                    for n in names:
                        parts[n].append(cols[n][a:b])  # type: ignore[index]
        out = {n: (_concat(v) if v else _empty(self.t, [n])[n]) for n, v in parts.items()}
        if self.t.known_at_ns is not None:
            out["known_at"] = out[self.t.ts_col] + self.t.known_at_ns  # type: ignore[operator]
        return out

    def frame(self, symbol: str, lo: int, hi: int, columns: Sequence[str] | None = None) -> pd.DataFrame:
        a = self.arrays(symbol, lo, hi, columns)
        return pd.DataFrame({k: (np.array(v, copy=True) if isinstance(v, np.ndarray) else v)
                             for k, v in a.items()})


class FundingLoader:
    """R2: the archive and REST funding sources of a symbol merged by ``data.funding.merge`` (archive
    primary, REST fills, conflicts raise). A window [lo, hi) merges the sources over [lo - tol, hi + tol)
    (tol = the frozen +-60 s match tolerance, so a pair across the edge is matched exactly as in the full
    merge) and keeps the merged records with lo <= funding_ts < hi."""

    def __init__(self, store: RawStore, *, auth: guard.HoldoutAuthorization | None = None,
                 cache: ChunkCache | None = None, budget_bytes: int = DEFAULT_CACHE_BYTES) -> None:
        cache = cache if cache is not None else ChunkCache(budget_bytes)
        self.archive = TableLoader(store, "R2_archive", auth=auth, cache=cache)
        self.rest = TableLoader(store, "R2_rest", auth=auth, cache=cache)
        self.cache = cache

    def symbols(self) -> tuple[str, ...]:
        return tuple(sorted(set(self.archive.symbols()) | set(self.rest.symbols())))

    def frame(self, symbol: str, lo: int, hi: int) -> pd.DataFrame:
        tol = int(FROZEN.funding.tol_ns)
        a = self.archive.frame(symbol, int(lo) - tol, int(hi) + tol)
        r = self.rest.frame(symbol, int(lo) - tol, int(hi) + tol)
        if not len(a) and not len(r):
            return pd.DataFrame({c: pd.Series(dtype=d) for c, d in (
                ("funding_ts", "int64"), ("rate", "float64"), ("interval_h_explicit", "Int64"),
                ("mark_at_settle", "float64"), ("mark_source", "object"), ("source", "object"),
                ("known_at", "int64"))})[list(fund_mod.OUT_COLUMNS)]
        m = fund_mod.merge(a, r)
        ts = m["funding_ts"].to_numpy(np.int64)
        i, j = int(np.searchsorted(ts, int(lo), "left")), int(np.searchsorted(ts, int(hi), "left"))
        return m.iloc[i:j].reset_index(drop=True)


# ---------------------------------------------------------------------------------------- universe source


@dataclass(frozen=True)
class SymbolMeta:
    """The per-symbol inputs that are not raw time series of this store: instances (R1 stage), the R1
    identity, R11 notice versions, classified terminal events, optional (E_M) aggTrades."""

    insts: list[Instance]
    identity: Identity | None
    notices: tuple[NoticeVersion, ...]
    terminals: Mapping[int, TerminalEvent]
    aggtrades: pd.DataFrame | None = None


class StoreSource:
    """``runner.UniverseSource`` over the raw store: ``load(symbol, lo, hi)`` -> ``SymbolInputs`` with the
    R3 / R5 / R4 bars and the merged R2 funding of [lo, hi) (lazy, guarded, one shared ``ChunkCache``) and the
    symbol's ``SymbolMeta``. ``symbols()`` = the symbols with metadata."""

    def __init__(self, store: RawStore, meta: Mapping[str, SymbolMeta], *,
                 auth: guard.HoldoutAuthorization | None = None, cache: ChunkCache | None = None,
                 budget_bytes: int = DEFAULT_CACHE_BYTES) -> None:
        self.cache = cache if cache is not None else ChunkCache(budget_bytes)
        self.meta = dict(meta)
        self.bars = {k: TableLoader(store, k, auth=auth, cache=self.cache)
                     for k in ("R3", "R5", "R4_mark", "R4_index", "R4_premium")}
        self.funding = FundingLoader(store, auth=auth, cache=self.cache)

    def symbols(self) -> tuple[str, ...]:
        return tuple(sorted(self.meta))

    def load(self, symbol: str, lo: int, hi: int) -> SymbolInputs:
        m = self.meta[symbol]
        r4 = R4Bars(*(self.bars[k].frame(symbol, lo, hi) for k in ("R4_premium", "R4_mark", "R4_index")))
        agg = m.aggtrades
        if agg is not None:
            ts = (ns_array(agg["ts"].to_numpy(), f"{symbol}.aggtrades.ts") if len(agg)
                  else np.empty(0, np.int64))
            agg = agg.iloc[int(np.searchsorted(ts, lo, "left")):int(np.searchsorted(ts, hi, "left"))]
        return SymbolInputs(symbol, self.bars["R3"].frame(symbol, lo, hi),
                            self.bars["R5"].frame(symbol, lo, hi), self.funding.frame(symbol, lo, hi),
                            list(m.insts), r4, m.identity, tuple(m.notices), dict(m.terminals), agg)


# ------------------------------------------------------------------------------------------- book runs


class RunStore:
    """Executed ``BookRun`` objects, written once under ``root`` as ``<output sha256>.pkl`` with a manifest
    (``MANIFEST.json``: file sha256, bytes, arm, h, execution, layer, window, research). ``load(sha)``
    guards the run's window (``guard.check_data_span``), checks the file sha256 BEFORE unpickling and
    recomputes ``runrecord.run_output_sha256`` after (it must equal ``sha``). Pickle is used because it
    round-trips the frames exactly (object / tuple columns included), so the output hash is reproducible;
    the files are this project's own outputs, bound to the manifest (not a defence against a hostile
    writer with access to both)."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def manifest(self) -> dict:
        p = self.root / MANIFEST
        return json.loads(p.read_bytes()) if p.exists() else {"kind": "V7_BOOK_RUNS", "runs": {}}

    def manifest_sha256(self) -> str | None:
        p = self.root / MANIFEST
        return _sha_bytes(p.read_bytes()) if p.exists() else None

    def put(self, run: BookRun) -> str:
        if not isinstance(run, BookRun):
            raise TypeError("RunStore.put takes an executed BookRun")
        sha = run_output_sha256(run)
        man = self.manifest()
        if sha in man["runs"]:
            return sha
        data = pickle.dumps(run, protocol=5)
        self.root.mkdir(parents=True, exist_ok=True)
        p = self.root / f"{sha}.pkl"
        _create_once(p, data)
        man["runs"][sha] = {"file": p.name, "sha256": _sha_bytes(data), "bytes": len(data), "arm": run.arm,
                            "h": int(run.h_star), "execution": run.execution, "layer": run.layer,
                            "window": [int(run.window[0]), int(run.window[1])],
                            "research": bool(run.research)}
        man["runs"] = dict(sorted(man["runs"].items()))
        _replace_atomic(self.root / MANIFEST, _canonical(man))
        return sha

    def load(self, output_sha256: str, *, auth: guard.HoldoutAuthorization | None = None) -> BookRun:
        k9.require()
        if not isinstance(output_sha256, str) or not _SHA.match(output_sha256):
            raise ValueError(f"not a sha256: {output_sha256!r}")
        e = self.manifest()["runs"].get(output_sha256)
        if e is None:
            raise KeyError(f"no persisted run {output_sha256}")
        guard.check_data_span(int(e["window"][0]), int(e["window"][1]), auth=auth,
                              what=f"persisted run {output_sha256[:12]}")
        p = self.root / e["file"]
        with open(p, "rb") as f:
            data = f.read()
        if _sha_bytes(data) != e["sha256"]:
            _integrity(f"{p}: sha256 differs from the run manifest (not unpickled)", p)
        run = pickle.loads(data)  # noqa: S301 - this project's own file, hash-checked above
        if not isinstance(run, BookRun) or run_output_sha256(run) != output_sha256:
            _integrity(f"{p}: the unpickled object is not the BookRun {output_sha256}", p)
        return run
