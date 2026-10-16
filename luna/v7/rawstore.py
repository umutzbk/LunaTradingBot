"""The real-scale raw store and its read-only lazy loaders (spec §3 "All raw files are immutable once written.
Each table has a manifest with per-file sha256 ... Time units are normalized to ns at ingest", S-1.7
"read-only loaders", §17, DEC-33; SC-01 part 1, SC-03, red-team M1 / M3; AMB-36, AMB-37).

Layout (``RawStore(root)``; one directory per table, one file per (symbol, calendar month))::

    <root>/<table dir>/MANIFEST.json                     table manifest: per-symbol manifest sha256s
    <root>/<table dir>/<SYMBOL>/MANIFEST.json            symbol manifest: per-file sha256, rows, provenance
    <root>/<table dir>/<SYMBOL>/<SYMBOL>-<YYYY-MM>.parquet

Tables (``TABLES``): R3 ``bars_last_15m``, R5 ``bars_last_1m``, R4 ``bars_{mark,index,premium}_15m`` and the
two R2 sources ``funding_archive`` / ``funding_rest`` (merged by ``data.funding.merge`` at load). A chunk
holds exactly the R-table columns (bars: ``open_ts`` int64 ns, the prices / volumes, int64 ``n_trades``;
R4: ``open_ts``, o, h, l, c; funding: ``funding_ts``, ``rate``, nullable ``interval_h_explicit``,
``mark_at_settle``), sorted by the event time, strictly increasing, every row inside the chunk's month.
``known_at`` is not stored: it is the fixed-offset archive rule (``data.schema.known_at``, open_ts + 15 min /
1 min) applied at load (and checked equal at write when the caller supplies it); merged funding gets it from
``data.funding.merge``.

Encoding (``FORMAT``; AMB-36 (b)): parquet, zstd level 9, no dictionary. A bar price / volume column is stored
as int64 = value x 10^k with ``DELTA_BINARY_PACKED`` (k per column per chunk: the decimals of the source CSV
strings, else the smallest exact k), only when ``int / 10**k`` reproduces every float64 value exactly
(checked at write); otherwise that column of that chunk stays float64 (``BYTE_STREAM_SPLIT``). The scales
are in the parquet schema metadata and in the manifest entry. ``content_sha256`` is defined on the float64
values, so the encoding is not part of the content identity.

Write once (``put_chunk``): K9 log required; one writer per store (an OS lock on ``<root>/.writer.lock``);
the store bound (the fetch layer's N1 bound: < 2025-07-01 before the opening, < the W_HOLD end with the
verified authorization) and ``guard.check_data_span`` first; the file is written under a temporary name,
fsynced and hard-linked / renamed to a target that must not exist (never overwritten); the symbol manifest
then the table manifest are replaced atomically. Identical content (``content_sha256``) is a no-op (and heals
a stale table-manifest entry), different content an ``ImmutabilityViolation`` (K9-logged). An orphan file (a
crash between the file and its manifest) is adopted only when its content is identical. The table manifest's
sha256 is the DT4 data-manifest hash (``manifest_sha256s``); it covers every file through the symbol
manifests.

Read only (``TableLoader`` / ``FundingLoader``): nothing is read until asked; each request and each chunk
period pass ``guard.check_data_span`` (W_HOLD without the verified authorization, M6 before R_V6: refused and
K9-logged) before a file is opened ``"rb"``; the file's sha256 must equal its manifest entry (else
``IntegrityBlocker``, K9-logged); a symbol directory with a manifest that the table manifest does not list is
an ``IntegrityBlocker`` (never "no rows"). Only the requested columns are decoded; rows are located with
``searchsorted`` on the sorted chunk ts arrays; a window touches only the chunks it overlaps and copies only
its rows. Decoded columns sit in a ``ChunkCache``: LRU with a fixed byte budget (an implementation parameter,
``DEFAULT_CACHE_BYTES``), deterministic eviction.

Ingest completeness (red-team r2 MJ2, controller ruling R55, AMB-45): ``<root>/INGEST_LEDGER.jsonl`` is the
store's own append-only, fsynced ingest ledger (the records of ``luna.v7.ingest``'s ledger: table, symbol,
month, status, sha256, rows, reason). ``put_chunk`` and ``resume_chunk`` append a ``stored`` record (the
chunk file's sha256); the ingestion appends an ``attempt`` record before it fetches a chunk and its ``gap`` /
``spill_not_requested`` records. ``check_ingest_complete(lo, hi, tables=, symbols=, auth=)`` refuses
(``IntegrityBlocker``, K9-logged) when, among the (table, symbol, month) keys in use (month overlapping
[lo, hi)): a key has an unresolved blocking record (``gap`` / ``spill_not_requested`` / ``attempt`` / an
unknown status; resolved only by a ``stored`` record of the chunk the store now holds, whatever the order;
a record with ``conflict: true`` (DEC-33 / S-2.6; a legacy record without the field: its reason names a
conflict) is not resolved by a re-ingest: A-6 (AMENDMENTS-2026-10-16) supersedes "never": a DEC-33 conflict
of an R3 / R5 key is resolved by a well-formed ``conflict_resolved_A6`` record (``RULE_A6``: dropped keys,
both value sets, source file sha256s) of the chunk the store now holds; an S-2.6 funding conflict still
blocks; an ``R2_rest`` key of a non-perpetual symbol (``perpetual_shaped`` false; S-2.1) without a chunk is
resolved by a ``not_applicable`` record, which is ignored for any other key), a stored chunk has no
``stored`` record of its own sha256 (the ledger is not this store's complete ledger), a ``planned`` key (the
caller's planned listing, optional) has no
stored chunk, or the ledger has an unreadable line. Repair of an unreadable (torn) line, append-only:
``quarantine_unreadable`` copies its bytes to ``INGEST_LEDGER.quarantine.jsonl``, appends a ``quarantined``
record (line number + sha256 of the bytes) and writes a K9 entry; the line is then skipped (whatever record it
held, the chunk's ``attempt`` record stays unresolved until the chunk is stored). With the verified
authorization, every ``.zip`` key of the pinned W_HOLD manifest in use must be a source of its stored
chunk with the pinned size (and md5, for a single-part ETag) (ruling R56: a
manifest entry that never reached the store is a gap; R10 aggTrades are not raw-store tables and are bound at
fetch time only).
``a6_resolutions(lo, hi)``: the A-6 records of the window (DT4, ``runrecord.run_record(a6_resolutions=)``).
The research entry points call it (``StoreSource.check_ingest``; ``runner.ResearchUniverse.from_source`` /
``for_decisions``, ``label_panel``, ``run_book``; ``metaingest.instances_from_store``): a hole is never read
as missing data.

``StoreSource``: the loader-backed ``runner.UniverseSource``. Executed ``BookRun`` objects are persisted by
``luna.v7.runstore`` (canonical parquet + JSON, no pickle), which reuses this module's write-once helpers.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import re
import threading
from collections import OrderedDict
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from luna.v7 import guard, k9, manifest, netguard, windows
from luna.v7.constants import FROZEN
from luna.v7.data import funding as fund_mod
from luna.v7.data.instances import Instance
from luna.v7.data.notices import NoticeVersion
from luna.v7.data.schema import KNOWN_AT_OFFSET_NS, IntegrityBlocker
from luna.v7.data.terminal import TerminalEvent
from luna.v7.features import R4Bars
from luna.v7.runner import SymbolInputs
from luna.v7.timeutil import ns_array
from luna.v7.universe import Identity

__all__ = ["DEFAULT_CACHE_BYTES", "FORMAT", "LEDGER", "MAX_SCALE", "TABLES", "ChunkCache", "FundingLoader",
           "ImmutabilityViolation", "IngestIndex", "RawStore", "StoreBusy", "StoreSource", "StoreTable",
           "SymbolMeta", "TableLoader", "append_ledger", "content_sha256", "ledger_record", "month_period",
           "months_between", "store_bound"]

DEFAULT_CACHE_BYTES = 512 * 2**20  # implementation parameter (AMB-36 (i)): decoded-column LRU budget
FORMAT = "parquet/zstd-9/scaled-int-delta/v1"
MAX_SCALE = 12  # implementation parameter: largest decimal scale k tried for a scaled-int column
MANIFEST = "MANIFEST.json"
LOCK = ".writer.lock"
SCALES_KEY = b"luna.v7.scales"
RULE_IMMUTABLE = "§3 raw files immutable once written (integrity)"
RULE_INTEGRITY = "§3 raw file sha256 differs from its manifest (integrity)"
RULE_INGEST = ("ruling R55 ingest completeness: unresolved ingest gap / DEC-33 conflict / unledgered chunk "
               "in the data used (refused, never read as missing data)")
LEDGER = "INGEST_LEDGER.jsonl"
STORED = "stored"
QUARANTINED = "quarantined"
A6_RESOLVED = "conflict_resolved_A6"  # A-6 (AMENDMENTS-2026-10-16): DEC-33 conflict keys dropped as missing
NOT_APPLICABLE = "not_applicable"  # S-2.1: an N3 key of a non-perpetual symbol (dry-run driver, Task 29e)
RULE_A6 = ("A-6 (AMENDMENTS-2026-10-16): a DEC-33 monthly / daily conflict is resolved by removing the "
           "conflicting keys from both sources (missing bars, neither value chosen); the rest of the chunk "
           "is stored")
_PERP_RE = re.compile(r"[A-Z0-9]+")
RULE_REPAIR = "ruling R55 ingest ledger repair: an unreadable line quarantined (append-only)"
BLOCKING = frozenset({"gap", "spill_not_requested", "attempt"})
_LEDGER_LOCK = threading.Lock()
_SYMBOL = re.compile(r"[A-Z0-9_]{1,40}\Z", re.ASCII)
_MONTH = re.compile(r"(\d{4})-(\d{2})\Z", re.ASCII)
_SHA = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_EXACT = float(2**53)


class ImmutabilityViolation(IntegrityBlocker):
    """A written raw file would get different content (§3: immutable once written); refused, K9-logged."""


class StoreBusy(RuntimeError):
    """Another writer holds the store's lock (one writer per store)."""


# ------------------------------------------------------------------------------------------------ tables


@dataclass(frozen=True)
class StoreTable:
    """One stored table: its directory, event-time column, stored (name, dtype) columns (ts first), the
    fixed known_at offset of the archive rule (None: set by the funding merge), its N1 dataset and whether
    its float columns are stored as scaled ints."""

    key: str
    dirname: str
    ts_col: str
    columns: tuple[tuple[str, str], ...]
    known_at_ns: int | None
    dataset: str | None
    interval: str | None
    daily_union: bool  # DEC-33 monthly union daily (R3 / R5 only; R4 monthly only, ruling R47)
    scaled: bool  # bar prices / volumes as int64 x 10^k (AMB-36 (b))

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(n for n, _ in self.columns)

    @property
    def scaled_names(self) -> tuple[str, ...]:
        return tuple(n for n, dt in self.columns if self.scaled and dt == "float64")


_BAR = (("open_ts", "int64"), ("o", "float64"), ("h", "float64"), ("l", "float64"), ("c", "float64"),
        ("vol_base", "float64"), ("vol_quote", "float64"), ("n_trades", "int64"),
        ("taker_buy_quote", "float64"))
_OHLC = _BAR[:5]
_FUND = (("funding_ts", "int64"), ("rate", "float64"), ("interval_h_explicit", "Int64"),
         ("mark_at_settle", "float64"))
_K15 = KNOWN_AT_OFFSET_NS["R4"]

TABLES: dict[str, StoreTable] = {
    "R3": StoreTable("R3", "bars_last_15m", "open_ts", _BAR, KNOWN_AT_OFFSET_NS["R3"], "klines", "15m", True,
                     True),
    "R5": StoreTable("R5", "bars_last_1m", "open_ts", _BAR, KNOWN_AT_OFFSET_NS["R5"], "klines", "1m", True,
                     True),
    "R4_mark": StoreTable("R4_mark", "bars_mark_15m", "open_ts", _OHLC, _K15, "markPriceKlines", "15m", False,
                          True),
    "R4_index": StoreTable("R4_index", "bars_index_15m", "open_ts", _OHLC, _K15, "indexPriceKlines", "15m",
                           False, True),
    "R4_premium": StoreTable("R4_premium", "bars_premium_15m", "open_ts", _OHLC, _K15, "premiumIndexKlines",
                             "15m", False, True),
    "R2_archive": StoreTable("R2_archive", "funding_archive", "funding_ts", _FUND, None, "fundingRate", None,
                             False, False),
    "R2_rest": StoreTable("R2_rest", "funding_rest", "funding_ts", _FUND, None, None, None, False, False),
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


def store_bound(auth: guard.HoldoutAuthorization | None = None) -> int:
    """The first event time the store never holds in this mode: the fetch layer's N1 bound (2025-07-01 before
    the opening, the W_HOLD end with the verified authorization)."""
    return int(netguard.bounds(auth).n1_end_ns)


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
    """sha256 of a chunk's content (table, column names / dtypes, float64 / int64 values; NaN canonical),
    independent of the file encoding (scales, parquet version)."""
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


def _scaled_ints(v: np.ndarray, k: int) -> np.ndarray | None:
    """int64 = v x 10^k when ``ints / 10**k`` reproduces every value bit for bit (lossless; -0.0 is not
    reproducible from an int and keeps the column float64), else None."""
    if not 0 <= k <= MAX_SCALE or not np.isfinite(v).all():
        return None
    p = float(10**k)
    s = np.rint(v * p)
    if len(s) and np.abs(s).max() >= _EXACT:
        return None
    ints = s.astype(np.int64)
    back = (ints / p).astype(np.float64)
    same = back.view(np.int64) == np.ascontiguousarray(v, dtype=np.float64).view(np.int64)
    return ints if same.all() else None


def _scales(t: StoreTable, cols: Mapping[str, object], decimals: Mapping[str, int | None] | None
            ) -> dict[str, int | None]:
    """k per scaled column: the given source decimals (the CSV strings) when lossless, else (no decimals
    given) the smallest lossless k <= MAX_SCALE; None = stored as float64."""
    out: dict[str, int | None] = {}
    for n in t.scaled_names:
        v = np.asarray(cols[n], dtype=np.float64)
        if decimals is not None:
            k = decimals.get(n)
            out[n] = k if k is not None and _scaled_ints(v, int(k)) is not None else None
        else:
            out[n] = next((k for k in range(MAX_SCALE + 1) if _scaled_ints(v, k) is not None), None)
    return out


def _encode(t: StoreTable, cols: Mapping[str, object], scales: Mapping[str, int | None]) -> bytes:
    arrays, fields, enc = [], [], {}
    for n, dt in t.columns:
        k = scales.get(n)
        if k is not None:
            a = _scaled_ints(np.asarray(cols[n], dtype=np.float64), int(k))
            if a is None:
                raise IntegrityBlocker(f"{t.key}.{n}: scale {k} is not lossless")
            arr, typ, enc[n] = pa.array(a, type=pa.int64()), pa.int64(), "DELTA_BINARY_PACKED"
        elif dt in ("int64", "Int64"):
            arr, typ, enc[n] = pa.array(cols[n], type=pa.int64()), pa.int64(), "DELTA_BINARY_PACKED"
        else:
            arr, typ, enc[n] = pa.array(cols[n], type=pa.float64()), pa.float64(), "BYTE_STREAM_SPLIT"
        arrays.append(arr)
        fields.append(pa.field(n, typ, nullable=(dt == "Int64")))
    meta = {SCALES_KEY: json.dumps({n: scales.get(n) for n in t.scaled_names}, sort_keys=True).encode()}
    tbl = pa.Table.from_arrays(arrays, schema=pa.schema(fields, metadata=meta))
    buf = io.BytesIO()
    pq.write_table(tbl, buf, compression="zstd", compression_level=9, use_dictionary=False,
                   column_encoding=enc)
    return buf.getvalue()


def _decode(t: StoreTable, data: bytes, names: Sequence[str] | None = None,
            scales: Mapping[str, int | None] | None = None) -> dict[str, object]:
    """The ``names`` columns (default all) as float64 / int64 / Int64 arrays. ``scales``: the manifest's
    (must equal the file metadata's)."""
    names = list(t.names if names is None else names)
    tbl = pq.read_table(io.BytesIO(data), columns=names)
    raw = (tbl.schema.metadata or {}).get(SCALES_KEY)
    file_scales = json.loads(raw) if raw is not None else None
    if file_scales is None or set(file_scales) != set(t.scaled_names):
        raise IntegrityBlocker(f"{t.key}: file scales metadata missing or malformed")
    if scales is not None and dict(scales) != file_scales:
        raise IntegrityBlocker(f"{t.key}: file scales {file_scales} differ from the manifest {dict(scales)}")
    dts = dict(t.columns)
    out: dict[str, object] = {}
    for name in names:
        col, dt = tbl.column(name), dts[name]
        if dt == "Int64":
            out[name] = pd.array(col.to_pandas(types_mapper={pa.int64(): pd.Int64Dtype()}.get), dtype="Int64")
            continue
        if col.null_count:
            raise IntegrityBlocker(f"{t.key}.{name}: null values in a non-nullable column")
        k = file_scales.get(name)
        if k is not None:
            out[name] = np.asarray(col.to_numpy(), dtype=np.int64) / float(10 ** int(k))
        else:
            out[name] = np.array(col.to_numpy(), dtype=np.int64 if dt == "int64" else np.float64, copy=True)
    return out


def _canonical(obj: object) -> bytes:
    return (json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode()


def _sha_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _write_tmp(path: Path, data: bytes) -> Path:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return tmp


def _replace_atomic(path: Path, data: bytes) -> None:
    """Write ``data`` to a temporary sibling, fsync, then replace ``path`` (manifests only)."""
    tmp = _write_tmp(path, data)
    try:
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _create_once(path: Path, data: bytes) -> None:
    """Write-once: a temporary sibling, fsync, then a hard link (or, where links are unsupported, a Windows
    rename) to ``path``, both of which fail if ``path`` exists: an existing file is never overwritten. The
    temporary file is removed in every case."""
    tmp = _write_tmp(path, data)
    try:
        try:
            os.link(tmp, path)
        except FileExistsError:
            raise ImmutabilityViolation(f"{path} already exists: raw files are written once") from None
        except OSError:
            if os.name != "nt":
                raise
            try:
                os.rename(tmp, path)  # Windows: fails if the target exists
            except FileExistsError:
                raise ImmutabilityViolation(f"{path} already exists: raw files are written once") from None
    finally:
        tmp.unlink(missing_ok=True)


@contextlib.contextmanager
def _writer_lock(root: Path) -> Iterator[None]:
    """One writer per store: a non-blocking OS lock on ``<root>/.writer.lock`` (released by the OS if the
    process dies), held around every read-modify-write of the manifests."""
    root.mkdir(parents=True, exist_ok=True)
    f = open(root / LOCK, "a+b")  # noqa: SIM115 - closed in finally
    locked = False
    try:
        try:
            if os.name == "nt":
                import msvcrt

                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except OSError:
            raise StoreBusy(f"{root}: another writer holds {LOCK} (one writer per store)") from None
        yield
    finally:
        if locked and os.name == "nt":
            import msvcrt

            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        f.close()


# ------------------------------------------------------------------------------------------- ledger


def ledger_record(table: str, symbol: str, month: str, status: str, *, sha256: str | None = None,
                  rows: int | None = None, reason: str | None = None) -> dict:
    """One ingest-ledger record (the format of ``luna.v7.ingest``'s ledger)."""
    return {"table": table, "symbol": symbol, "month": month, "status": status, "sha256": sha256,
            "rows": rows, "reason": reason}


def append_ledger(path: Path, rec: Mapping) -> None:
    """Append one JSON line (fsynced; serialised across this process's threads; a torn last line of a crash
    is never glued to the new record, it stays unreadable: fail closed)."""
    line = (json.dumps(dict(rec), sort_keys=True, default=str) + "\n").encode()
    path = Path(path)
    with _LEDGER_LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "ab") as f:
            if f.tell() > 0:
                with open(path, "rb") as r:
                    r.seek(-1, os.SEEK_END)
                    if r.read(1) != b"\n":
                        line = b"\n" + line
            f.write(line)
            f.flush()
            os.fsync(f.fileno())


def _conflict(rec: Mapping) -> bool:
    """The structured ``conflict`` field (ruling R55 round 1); a legacy record without it: its reason names a
    conflict (read as it is, fail closed)."""
    if "conflict" in rec:
        return rec.get("conflict") is True
    reason = rec.get("reason")
    return isinstance(reason, str) and "conflict" in reason.lower()


def perpetual_shaped(symbol: object) -> bool:
    """S-2.1 covers PERPETUAL contracts: an alphanumeric (upper-case) symbol not ending in ``SETTLED``
    (delivery ``XXX_YYMMDD`` and settled ``XXX_SETTLED`` / ``XXXSETTLED`` names are not perpetual-shaped)."""
    return (isinstance(symbol, str) and _PERP_RE.fullmatch(symbol) is not None
            and not symbol.endswith("SETTLED"))


def _a6_ok(r: Mapping) -> bool:
    """A well-formed A-6 resolution record: a DEC-33 (monthly union daily) table, the resolved chunk's
    sha256, non-empty int keys with both value sets of the same length, the source files and the rule."""
    t = r.get("table")
    keys, m, d = r.get("keys"), r.get("monthly_values"), r.get("daily_values")
    return (t in TABLES and TABLES[t].daily_union and isinstance(r.get("sha256"), str)
            and isinstance(keys, list) and bool(keys)
            and all(isinstance(x, int) and not isinstance(x, bool) for x in keys)
            and isinstance(m, list) and isinstance(d, list) and len(m) == len(d) == len(keys)
            and isinstance(r.get("sources"), list) and bool(r.get("sources")) and r.get("rule") == RULE_A6)


def _rank(why: str) -> int:
    return 2 if why.startswith("conflict") else 0 if why.startswith("attempt") else 1


@dataclass(frozen=True)
class IngestIndex:
    """The store ledger against the store: ``unresolved`` {key: reason}, ``unledgered`` keys (a chunk with no
    ``stored`` record of its sha256), ``unreadable`` ledger line numbers. key = (table, symbol, month)."""

    unresolved: Mapping[tuple, str]
    unledgered: frozenset
    unreadable: tuple[int, ...]
    stored: frozenset = frozenset()  # keys with a chunk and its stored record
    unreadable_sha: tuple[tuple[int, str], ...] = ()  # (line, sha256 of its bytes)
    a6: Mapping[tuple, tuple] = field(default_factory=dict)  # key -> A-6 records of the current chunk
    not_applicable: frozenset = frozenset()  # keys resolved as not applicable (S-2.1, non-perpetual N3)


# ------------------------------------------------------------------------------------------------ store


class RawStore:
    """The raw store under ``root`` (module docstring). Writing is ``put_chunk`` (the ingestion's only
    write path; one writer per store); reading goes through ``TableLoader`` / ``FundingLoader``."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self._ingest_cache: tuple[object, IngestIndex] | None = None

    # the store's ingest ledger (ruling R55)
    @property
    def ledger_path(self) -> Path:
        return self.root / LEDGER

    def append_ledger(self, rec: Mapping) -> None:
        """Append one record to the store's ingest ledger (module docstring)."""
        append_ledger(self.ledger_path, rec)

    def _stored_record(self, table: str, symbol: str, month: str, e: Mapping) -> None:
        self.append_ledger(ledger_record(_table(table).key, symbol, month, STORED, sha256=e["sha256"],
                                         rows=e["rows"]))

    def ingest_index(self) -> IngestIndex:
        """The ledger against the store's manifests (module docstring); cached on the ledger's size / mtime
        and the table-manifest sha256s."""
        p = self.ledger_path
        try:
            st = p.stat()
            sig: object = (st.st_size, st.st_mtime_ns)
        except OSError:
            sig = None
        key = (sig, tuple(sorted(self.manifest_sha256s().items())))
        if self._ingest_cache is not None and self._ingest_cache[0] == key:
            return self._ingest_cache[1]
        records: list[dict] = []
        bad: list[tuple[int, str]] = []
        if sig is not None:
            for i, raw in enumerate(p.read_bytes().split(b"\n"), 1):
                if not raw.strip():
                    continue
                try:
                    r = json.loads(raw.decode("utf-8"))
                except ValueError:
                    r = None
                if isinstance(r, dict):
                    records.append(r)
                else:
                    bad.append((i, _sha_bytes(raw)))
        quarantined = {(r.get("line"), r.get("line_sha256")) for r in records
                       if r.get("status") == QUARANTINED}
        bad = [b for b in bad if b not in quarantined]
        chunks: dict[tuple, str] = {}
        for t in TABLES:
            tm = self.table_manifest(t) or {"symbols": {}}
            for sym in tm["symbols"]:
                for month, e in (self.symbol_manifest(t, sym) or {"chunks": {}})["chunks"].items():
                    chunks[(t, sym, month)] = e["sha256"]
        stored: dict[tuple, set] = {}
        blocking: dict[tuple, str] = {}
        conflicts: set[tuple] = set()
        a6: dict[tuple, list[dict]] = {}
        na: set[tuple] = set()
        for r in records:
            k = (r.get("table"), r.get("symbol"), r.get("month"))
            if r.get("status") == QUARANTINED:
                continue
            if r.get("status") == STORED:
                stored.setdefault(k, set()).add(r.get("sha256"))
                continue
            if r.get("status") == A6_RESOLVED and _a6_ok(r):
                a6.setdefault(k, []).append(r)
                continue
            if r.get("status") == NOT_APPLICABLE and k[0] == "R2_rest" and not perpetual_shaped(k[1]):
                na.add(k)
                continue
            why = f"{r.get('status')}: {r.get('reason')}"
            if _conflict(r):
                conflicts.add(k)
                why = f"conflict: {why}"
            old = blocking.get(k)
            if old is None or _rank(why) > _rank(old):
                blocking[k] = why  # the most informative record: a conflict, else a gap over an attempt
        resolved = frozenset(k for k, sha in chunks.items() if sha in stored.get(k, ()))
        cur_a6 = {k: tuple(r for r in rs if k in resolved and r["sha256"] == chunks[k])
                  for k, rs in a6.items()}
        cur_a6 = {k: rs for k, rs in cur_a6.items() if rs}

        def settled(k: tuple) -> bool:
            if k in conflicts:  # DEC-33: resolved only by A-6 (an A-6 record of the chunk the store holds)
                return k in cur_a6
            return k in resolved or (k in na and k not in chunks)

        unresolved = {k: why for k, why in blocking.items() if not settled(k)}
        unledgered = frozenset(k for k, sha in chunks.items() if sha not in stored.get(k, ()))
        idx = IngestIndex(unresolved, unledgered, tuple(i for i, _ in bad), resolved, tuple(bad), cur_a6,
                          frozenset(na))
        self._ingest_cache = (key, idx)
        return idx

    def ingest_state_sha256(self) -> str:
        """The sha256 of the ledger's resolved state (the unresolved keys with their reasons, the
        unledgered chunks, the unreadable lines): part of the data identity (``data_manifests``,
        ``StoreSource.identity``) so view stores and run records change when completeness changes (ruling
        R55)."""
        idx = self.ingest_index()
        body: dict = {"unresolved": sorted(([*k, why] for k, why in idx.unresolved.items()), key=repr),
                      "unledgered": sorted((list(k) for k in idx.unledgered), key=repr),
                      "unreadable": list(idx.unreadable)}
        if idx.a6:  # A-6 resolutions and not-applicable keys change the identity (absent: unchanged)
            body["a6_resolved"] = sorted(([*k, r["sha256"], r["keys"]] for k, rs in idx.a6.items()
                                          for r in rs), key=repr)
        if idx.not_applicable:
            body["not_applicable"] = sorted((list(k) for k in idx.not_applicable), key=repr)
        return _sha_bytes(_canonical(body))

    def a6_resolutions(self, lo: int, hi: int, *, tables: Sequence[str] | None = None,
                       symbols: Sequence[str] | None = None) -> list[dict]:
        """A-6: the resolution records of the chunks the store holds whose dropped keys lie in [lo, hi) (for
        DT4, ``runrecord.run_record(a6_resolutions=)``), sorted by (table, symbol, month)."""
        tset = None if tables is None else {_table(t).key for t in tables}
        sset = None if symbols is None else set(symbols)
        out = []
        for k in sorted(self.ingest_index().a6, key=repr):
            if (tset is not None and k[0] not in tset) or (sset is not None and k[1] not in sset):
                continue
            for r in self.ingest_index().a6[k]:
                if any(int(lo) <= x < int(hi) for x in r["keys"]):
                    out.append(json.loads(_canonical(dict(r))))
        return out

    def data_manifests(self) -> dict[str, str]:
        """The DT4 data manifests of this store: every table-manifest sha256 plus ``ingest_ledger_state``
        (``ingest_state_sha256``; ruling R55)."""
        return {**self.manifest_sha256s(), "ingest_ledger_state": self.ingest_state_sha256()}

    def quarantine_unreadable(self) -> int:
        """The documented repair of unreadable (torn) ledger lines, append-only (module docstring): each
        one is copied to ``INGEST_LEDGER.quarantine.jsonl``, a ``quarantined`` record (line, sha256) is
        appended to the ledger and a K9 entry is written. Returns the number quarantined."""
        lg = k9.require()
        idx = self.ingest_index()
        if not idx.unreadable_sha:
            return 0
        lines = self.ledger_path.read_bytes().split(b"\n")
        for i, sha in idx.unreadable_sha:
            raw = lines[i - 1]
            append_ledger(self.root / "INGEST_LEDGER.quarantine.jsonl",
                          {"line": i, "line_sha256": sha, "bytes": raw.decode("utf-8", errors="replace")})
            self.append_ledger({"status": QUARANTINED, "line": i, "line_sha256": sha,
                                "reason": "unreadable ledger line quarantined (repair, append-only)"})
            lg.record(request={"kind": "ingest_ledger_repair", "store": str(self.root), "line": i,
                               "line_sha256": sha}, rule=RULE_REPAIR, reason="unreadable line quarantined")
        return len(idx.unreadable_sha)

    def check_ingest_complete(self, lo: int, hi: int, *, tables: Sequence[str] | None = None,
                              symbols: Sequence[str] | None = None,
                              auth: guard.HoldoutAuthorization | None = None,
                              planned: Sequence[tuple[str, str, str]] | None = None) -> None:
        """Ruling R55 (module docstring): refuse (IntegrityBlocker, K9-logged) when the data in use, the
        (table, symbol, month) keys of ``tables`` / ``symbols`` (None: all) whose month overlaps [lo, hi), is
        not completely ingested."""
        k9.require()
        lo, hi = int(lo), int(hi)
        tset = None if tables is None else {_table(t).key for t in tables}
        sset = None if symbols is None else set(symbols)

        def used(k: tuple) -> bool:
            t, s, m = k
            if not (isinstance(t, str) and isinstance(s, str)):
                return True  # a malformed record is in use (fail closed)
            if (tset is not None and t not in tset) or (sset is not None and s not in sset):
                return False
            try:
                p_lo, p_hi = month_period(m)
            except (ValueError, TypeError):
                return True
            return p_lo < hi and lo < p_hi

        idx = self.ingest_index()
        bad = [f"ledger line {i} unreadable" for i in idx.unreadable]
        bad += [f"{k}: unresolved {why}" for k, why in sorted(idx.unresolved.items(), key=repr) if used(k)]
        bad += [f"{k}: stored chunk without a stored ledger record" for k in sorted(idx.unledgered)
                if used(k)]
        if planned is not None:  # optional listing coverage (AMB-45): every planned key in use is stored
            keys = sorted({tuple(x) for x in planned})
            bad += [f"{k}: planned (listed) but not stored" for k in keys if used(k) and k not in idx.stored]
        if auth is not None:
            bad += self._manifest_gaps(lo, hi, tset, sset, auth)
        if bad:
            more = " ..." if len(bad) > 5 else ""
            e = IntegrityBlocker(f"raw store {self.root}: ingest incomplete for [{lo}, {hi}) ({len(bad)}: "
                                 f"{'; '.join(bad[:5])}{more}); refused (ruling R55)")
            k9.log_refusal(e, request={"kind": "ingest_completeness", "store": str(self.root), "lo": lo,
                                       "hi": hi, "n": len(bad)}, rule=RULE_INGEST)
            raise e

    def _manifest_gaps(self, lo: int, hi: int, tset: set | None, sset: set | None, auth) -> list[str]:
        """Ruling R56: every ``.zip`` key in use of the pinned W_HOLD manifest must be a source of its stored
        chunk."""
        pin = guard.verified_opening(auth)
        m = manifest.load_pinned(pin["manifest_sha256"])
        srcs: dict[tuple[str, str], dict] = {}
        out: list[str] = []
        for table, sym, period, key in m.zips():
            if table not in TABLES:  # R10 aggTrades: not a raw-store table (bound at fetch time, R56b)
                continue
            per = guard.file_period(key)
            if per is None or not (per[0] < hi and lo < per[1]):
                continue
            if (tset is not None and table not in tset) or (sset is not None and sym not in sset):
                continue
            if (table, sym) not in srcs:
                srcs[(table, sym)] = (self.symbol_manifest(table, sym) or {"chunks": {}})["chunks"]
            e = srcs[(table, sym)].get(period[:7])
            url = "https://data.binance.vision/" + key
            src = [s for s in (e or {}).get("sources", []) if s.get("url") == url]
            size, ck = m.entries[key]
            if not src:
                out.append(f"({table}, {sym}, {period[:7]}): pinned W_HOLD file {key} not stored")
            elif manifest.match_source(size, ck, src[0]) is not None:
                out.append(f"({table}, {sym}, {period[:7]}): stored source of {key} differs from the pinned "
                           f"entry ({manifest.match_source(size, ck, src[0])})")
        return out

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

    def resume_chunk(self, table: str, symbol: str, month: str) -> dict:
        """A resumed ingest's view of a completed chunk: the table-manifest entry of ``symbol`` is healed from
        its symbol manifest (a crash between the two), then the chunk is verified (``verify_chunk``)."""
        k9.require()
        t = _table(table)
        with _writer_lock(self.root):
            self._sync_table(t, symbol)
        e = self.verify_chunk(table, symbol, month)
        self._stored_record(table, symbol, month, e)  # ruling R55: a verified chunk is a stored record
        return e

    # the write path
    def put_chunk(self, table: str, symbol: str, month: str, df: pd.DataFrame, *, sources: Sequence[Mapping],
                  decimals: Mapping[str, int | None] | None = None,
                  auth: guard.HoldoutAuthorization | None = None) -> dict:
        """Write one (table, symbol, month) chunk once (module docstring); returns its manifest entry.
        ``decimals``: per scaled column, the decimals of the source strings (None: inferred)."""
        t = _table(table)
        _symbol(symbol)
        lo, hi = month_period(month)
        k9.require()
        what = f"raw store {t.key} {symbol} {month}"
        req = {"kind": "raw_store_write", "table": t.key, "symbol": symbol, "month": month}
        bound = store_bound(auth)
        if hi > bound:
            e = guard.HoldoutBlocked(f"{what}: period ends after the store bound {bound} (the fetch layer's "
                                     "N1 bound): not stored")
            k9.log_refusal(e, request=req, rule=guard.k9_rule(e))
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
        with _writer_lock(self.root):
            old = self.entry(table, symbol, month)
            if old is not None:
                if old["content_sha256"] != csha:
                    _immutable(f"{what}: written with content {old['content_sha256']}, refusing {csha}", req)
                if not path.exists():  # a lost file: re-created byte-identical only (manifest unchanged)
                    data = _encode(t, cols, old["scales"])
                    if _sha_bytes(data) != old["sha256"]:
                        _immutable(f"{what}: re-encoded file differs from the manifest sha256", req)
                    path.parent.mkdir(parents=True, exist_ok=True)
                    _create_once(path, data)
                self._sync_table(t, symbol)  # heals a crash between the symbol and the table manifest
                e = self.verify_chunk(table, symbol, month)
                self._stored_record(table, symbol, month, e)
                return e
            if path.exists():  # orphan (crash before its manifest entry): adopted only when identical
                data = path.read_bytes()
                try:
                    same = content_sha256(t, _decode(t, data)) == csha
                    meta = pq.read_schema(io.BytesIO(data)).metadata or {}
                    scales = json.loads(meta[SCALES_KEY])
                except (IntegrityBlocker, pa.ArrowException, ValueError, KeyError):
                    same, scales = False, {}
                if not same:
                    _immutable(f"{what}: an unrecorded file with different content exists", req)
            else:
                scales = _scales(t, cols, decimals)
                data = _encode(t, cols, scales)
                path.parent.mkdir(parents=True, exist_ok=True)
                _create_once(path, data)
            entry = {"file": path.name, "sha256": _sha_bytes(data), "bytes": len(data),
                     "content_sha256": csha,
                     "rows": int(len(ts)), "period": [lo, hi], "scales": scales,
                     "span": [int(ts[0]), int(ts[-1]) + 1] if len(ts) else None,
                     "sources": [dict(s) for s in sources]}
            self._record(t, symbol, month, entry)
            self._stored_record(table, symbol, month, entry)
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
        p = self._symbol_manifest_path(t.key, symbol)
        if not p.exists():
            return
        sdata = p.read_bytes()
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
    """LRU of decoded chunk columns with a fixed byte budget (``budget_bytes`` > 0; an implementation
    parameter). Eviction is deterministic: least recently used first (access order is total, so there are no
    ties); an item larger than the whole budget is returned to the caller but never cached. Shareable."""

    def __init__(self, budget_bytes: int = DEFAULT_CACHE_BYTES) -> None:
        if isinstance(budget_bytes, bool) or not isinstance(budget_bytes, int) or budget_bytes <= 0:
            raise ValueError(f"budget_bytes must be a positive int, got {budget_bytes!r}")
        self.budget = budget_bytes
        self.bytes = 0
        self._items: OrderedDict[tuple, tuple[object, int]] = OrderedDict()
        self.stats = {"hits": 0, "misses": 0, "evictions": 0}

    def get(self, key: tuple) -> object | None:
        if key in self._items:
            self._items.move_to_end(key)
            self.stats["hits"] += 1
            return self._items[key][0]
        self.stats["misses"] += 1
        return None

    def put(self, key: tuple, value: object, nbytes: int) -> None:
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
    sha256 checked against the table manifest), a chunk's columns when a window first needs them."""

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

    def first_period(self, symbol: str) -> int | None:
        """The start of the symbol's first stored chunk period (its history start), None without data."""
        if not self._known(symbol):
            return None
        los = self._chunks(symbol)[1]
        return int(los[0]) if los.size else None

    def _known(self, symbol: str) -> bool:
        """Whether the symbol has data; a symbol manifest the table manifest does not list is refused."""
        if symbol in self._symbols:
            return True
        p = self.store._symbol_manifest_path(self.t.key, _symbol(symbol))
        if p.exists():
            _integrity(f"{p}: symbol manifest not listed in the {self.t.key} table manifest (an interrupted "
                       "write; resume the ingest to heal it)", p)
        return False

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

    def _chunk(self, symbol: str, month: str, e: dict, names: Sequence[str]) -> dict[str, object]:
        """The ``names`` columns of one chunk (cached per column; one file read for all missing columns)."""
        base = (str(self.store.root), self.t.key, symbol, month)
        out: dict[str, object] = {}
        missing: list[str] = []
        for n in names:
            got = self.cache.get((*base, n))
            if got is None:
                missing.append(n)
            else:
                out[n] = got
        if not missing:
            return out
        guard.check_data_span(int(e["period"][0]), int(e["period"][1]), auth=self.auth,
                              what=f"raw chunk {self.t.key} {symbol} {month}")
        p = self.store.chunk_path(self.t.key, symbol, month)
        with open(p, "rb") as f:  # read-only
            data = f.read()
        if _sha_bytes(data) != e["sha256"]:
            _integrity(f"{p}: sha256 {_sha_bytes(data)} differs from the manifest {e['sha256']}", p)
        cols = _decode(self.t, data, missing, e["scales"])
        for n in missing:
            if len(cols[n]) != int(e["rows"]):  # type: ignore[arg-type]
                _integrity(f"{p}: {n} has {len(cols[n])} rows, manifest {e['rows']}", p)  # type: ignore[arg-type]
            self.cache.put((*base, n), cols[n], int(getattr(cols[n], "nbytes", 0)))
            out[n] = cols[n]
        self.stats["decoded_chunks"] += 1
        self.stats["bytes_read"] += len(data)
        return out

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
        if self._known(symbol):
            months, los, his, ents = self._chunks(symbol)
            i0 = int(np.searchsorted(his, lo, "right"))
            i1 = int(np.searchsorted(los, hi, "left"))
            for i in range(i0, i1):
                cols = self._chunk(symbol, months[i], ents[i], names)
                ts = cols[ts_col]
                a = int(np.searchsorted(ts, lo, "left"))  # type: ignore[call-overload]
                b = int(np.searchsorted(ts, hi, "left"))  # type: ignore[call-overload]
                if b > a:
                    for n in names:
                        parts[n].append(cols[n][a:b])  # type: ignore[index]
        out = {n: (_concat(v) if v else _empty(self.t, [n])[n]) for n, v in parts.items()}
        if self.t.known_at_ns is not None:
            out["known_at"] = out[ts_col] + self.t.known_at_ns  # type: ignore[operator]
        return out

    def frame(self, symbol: str, lo: int, hi: int, columns: Sequence[str] | None = None) -> pd.DataFrame:
        a = self.arrays(symbol, lo, hi, columns)
        return pd.DataFrame({k: (np.array(v, copy=True) if isinstance(v, np.ndarray) else v)
                             for k, v in a.items()})

    def last_before(self, symbol: str, ts: int, floor: int) -> pd.DataFrame:
        """The last row of ``symbol`` with floor <= event time < ts (0 or 1 row, the ``frame`` columns),
        located from the manifests' row spans: only the newest chunk holding such a row is read (guarded as
        every read)."""
        ts, floor = int(ts), int(floor)
        if ts > floor and self._known(symbol):
            months, los, _, ents = self._chunks(symbol)
            for i in range(int(np.searchsorted(los, ts, "left")) - 1, -1, -1):
                sp = ents[i].get("span")
                if int(ents[i]["period"][1]) <= floor:
                    break
                if not sp or int(sp[0]) >= ts:
                    continue
                a, b = max(int(sp[0]), floor), min(int(sp[1]), ts)
                if a < b:
                    df = self.frame(symbol, a, b)
                    if len(df):
                        return df.iloc[-1:].reset_index(drop=True)
        e = _empty(self.t, self.t.names)
        if self.t.known_at_ns is not None:
            e["known_at"] = np.empty(0, np.int64)
        return pd.DataFrame(e)


def _padded(lo: int, hi: int, pad: int, auth: guard.HoldoutAuthorization | None) -> tuple[int, int]:
    """[lo - pad, hi + pad) clipped so the padding never reaches content the window itself may not read: the
    store bound and the canonical M6 start above hi, the M6 end / W_HOLD end below lo (before the opening)."""
    p_lo, p_hi = lo - pad, hi + pad
    bound = store_bound(auth)
    if hi <= bound:
        p_hi = min(p_hi, bound)
    m6_lo, m6_hi = windows.m6_cached()
    if hi <= m6_lo:
        p_hi = min(p_hi, m6_lo)
    if m6_hi is not None and lo >= m6_hi:
        p_lo = max(p_lo, m6_hi)
    if auth is None and lo >= windows.W_HOLD[1]:
        p_lo = max(p_lo, windows.W_HOLD[1])
    return p_lo, p_hi


class FundingLoader:
    """R2: the archive and REST funding sources of a symbol merged by ``data.funding.merge`` (archive
    primary, REST fills, conflicts raise). A window [lo, hi) is guarded as such; the sources are merged over
    [lo - tol, hi + tol) (tol = the frozen +-60 s match tolerance, so a pair across the edge matches as in the
    full merge), the padding clipped at the store bound / M6 (``_padded``: a W_DEV window ending at the W_HOLD
    start reads nothing of W_HOLD), and the merged records with lo <= funding_ts < hi are kept."""

    def __init__(self, store: RawStore, *, auth: guard.HoldoutAuthorization | None = None,
                 cache: ChunkCache | None = None, budget_bytes: int = DEFAULT_CACHE_BYTES) -> None:
        cache = cache if cache is not None else ChunkCache(budget_bytes)
        self.archive = TableLoader(store, "R2_archive", auth=auth, cache=cache)
        self.rest = TableLoader(store, "R2_rest", auth=auth, cache=cache)
        self.cache, self.auth = cache, auth

    def symbols(self) -> tuple[str, ...]:
        return tuple(sorted(set(self.archive.symbols()) | set(self.rest.symbols())))

    def frame(self, symbol: str, lo: int, hi: int) -> pd.DataFrame:
        lo, hi = int(lo), int(hi)
        if hi <= lo:
            raise ValueError(f"empty window [{lo}, {hi})")
        guard.check_data_span(lo, hi, auth=self.auth, what=f"raw R2 {symbol}")
        p_lo, p_hi = _padded(lo, hi, int(FROZEN.funding.tol_ns), self.auth)
        a = self.archive.frame(symbol, p_lo, p_hi)
        r = self.rest.frame(symbol, p_lo, p_hi)
        if not len(a) and not len(r):
            return pd.DataFrame({c: pd.Series(dtype=d) for c, d in (
                ("funding_ts", "int64"), ("rate", "float64"), ("interval_h_explicit", "Int64"),
                ("mark_at_settle", "float64"), ("mark_source", "object"), ("source", "object"),
                ("known_at", "int64"))})[list(fund_mod.OUT_COLUMNS)]
        m = fund_mod.merge(a, r)
        ts = m["funding_ts"].to_numpy(np.int64)
        i, j = int(np.searchsorted(ts, lo, "left")), int(np.searchsorted(ts, hi, "left"))
        return m.iloc[i:j].reset_index(drop=True)


# ---------------------------------------------------------------------------------------- universe source


@dataclass(frozen=True)
class SymbolMeta:
    """The per-symbol inputs that are not raw time series of this store: instances (R1 stage), the R1
    identity, R11 notice versions, classified terminal events, optional (E_M) aggTrades."""

    insts: list[Instance]
    identity: Identity | Mapping[int, Identity] | None  # per symbol, or per instance idx (R1)
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
        self.store = store
        self.auth = auth
        self.meta = dict(meta)
        self.bars = {k: TableLoader(store, k, auth=auth, cache=self.cache)
                     for k in ("R3", "R5", "R4_mark", "R4_index", "R4_premium")}
        self.funding = FundingLoader(store, auth=auth, cache=self.cache)

    def symbols(self) -> tuple[str, ...]:
        return tuple(sorted(self.meta))

    def check_ingest(self, lo: int, hi: int) -> None:
        """Ruling R55: every table this source reads, every symbol of the store, months overlapping [lo, hi)
        completely ingested (``RawStore.check_ingest_complete``; a symbol missing from the metadata because
        its bars never arrived is still in use)."""
        self.store.check_ingest_complete(lo, hi, auth=self.auth)

    def identity(self) -> dict:
        """The data identity of a view-store key (fix round 1, I2): every table-manifest sha256 of the store,
        the sha256 of its ingest ledger's resolved state (ruling R55) and a sha256 of the per-symbol metadata
        the views read (instances, identity, notices, terminal events)."""
        meta = sorted((s, (list(m.insts), m.identity, tuple(m.notices), sorted(dict(m.terminals).items())))
                      for s, m in self.meta.items())
        return {"raw": self.store.manifest_sha256s(), "ingest": self.store.ingest_state_sha256(),
                "meta": hashlib.sha256(repr(meta).encode()).hexdigest()}

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

    def load_decision_inputs(self, symbol: str, lo: int, hi: int, history_lo: int) -> SymbolInputs:
        """What a decision view reads (``runner.ResearchUniverse.for_decisions``, SC-01 part 2): R3 / R4 of
        [lo, hi), each preceded by its last row of [history_lo, lo) (``TableLoader.last_before``: the anchor
        a windowed slice takes when no recent row exists, fix round 1 m1), merged R2 of [history_lo, hi), R5
        only inside the symbol's U6 72 h halt windows (``runner.halt_windows``) within [history_lo, hi), no
        aggTrades. Each read is guarded as usual."""
        from luna.v7.runner import halt_windows  # runtime import: runner is imported by this module's caller

        lo, hi, history_lo = int(lo), int(hi), int(history_lo)
        m = self.meta[symbol]
        def anchored(k: str) -> pd.DataFrame:
            w = self.bars[k].frame(symbol, lo, hi)
            a = self.bars[k].last_before(symbol, lo, history_lo) if lo > history_lo else w.iloc[0:0]
            return pd.concat([a, w], ignore_index=True) if len(a) else w

        r4 = R4Bars(*(anchored(k) for k in ("R4_premium", "R4_mark", "R4_index")))
        wins = [(max(a, history_lo), min(b, hi)) for a, b in sorted(halt_windows(symbol, m.notices,
                                                                                 list(m.insts)))]
        parts = [self.bars["R5"].frame(symbol, a, b) for a, b in wins if a < b]
        if parts:
            b1 = pd.concat(parts, ignore_index=True)
            keep = np.unique(b1["open_ts"].to_numpy(np.int64), return_index=True)[1]
            b1 = b1.iloc[keep].reset_index(drop=True)
        else:
            t5 = TABLES["R5"]
            b1 = pd.DataFrame({**_empty(t5, t5.names), "known_at": np.empty(0, np.int64)})
        return SymbolInputs(symbol, anchored("R3"), b1,
                            self.funding.frame(symbol, history_lo, hi), list(m.insts), r4, m.identity,
                            tuple(m.notices), dict(m.terminals), None)
