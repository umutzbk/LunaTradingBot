"""The metadata store: immutable Wayback captures and the R11 / R1 / R12 tables (spec §3 "All raw files are
immutable once written. Each table has a manifest with per-file sha256", S-1.7 "no announcement or CMS
metadata dated at or after the W_HOLD start", E6, R11, R1, R12 / EC-7; Task 26c, AMB-39 / AMB-40).

Layout (``MetaStore(root)``; the ``rawstore`` conventions: write once, manifests replaced atomically, one
writer per store, K9 required, guarded read-only loads)::

    <root>/wayback_captures/MANIFEST.json            {"<ts>_<sha256>": {ts, original, sha256, bytes, recv_ts}}
    <root>/wayback_captures/<ts>_<sha256>.bin        the raw N6 bytes as received
    <root>/metadata/MANIFEST.json                    {"<table>/<scope>": {file, sha256, rows, content_sha256}}
    <root>/metadata/<table>-<scope>.json             canonical JSON: the table's columns and rows

* ``put_capture``: the capture time passes ``guard.check_data_span`` first (W_HOLD refused before the opening,
  M6 before R_V6, always; K9-logged): nothing dated at or after the W_HOLD start is stored before the
  opening. The file name is content-addressed; a second put of the same capture is a no-op, a different
  original URL for it an ``ImmutabilityViolation``. Only the capture TIME is checked here: a capture whose own
  ``releaseDate`` lies in W_HOLD / M6 can be stored, but ``metaingest.build_notices`` refuses it (nothing of
  it reaches a version or R11; fix round 1, m4).
* ``put_table(table, scope, df)``: R11 / R1 / R12 per scope (e.g. ``W_DEV``), with exactly the S-3 columns;
  every row time (R11 ``releaseDate`` / ``capture_ts``; R1 ``first_bar_ts`` / ``last_trade_ts`` /
  ``instance_end_known_at``; R12 ``settlement_ts`` / ``source_timestamp`` / ``known_at``) passes the same
  guard; R12 rows must pass ``terminal.validate_r12`` (exact official sources only: no placeholder ever).
  Written once: identical content is a no-op, different content an ``ImmutabilityViolation`` (K9-logged).
* ``load_table``: the file's sha256 must equal its manifest entry (``IntegrityBlocker``); the rows' times are
  guarded again. R12 that was never written is the empty, schema-correct table (EC-7: valid with zero rows);
  a missing R11 / R1 is an ``IntegrityBlocker`` (never "no rows").
* ``load_notice_versions``: the R11 versions rebuilt from their stored captures (``captures.extract`` is
  deterministic; the stored ``releaseDate`` and kind must be reproduced, else ``IntegrityBlocker``).
"""

from __future__ import annotations

import json
import math
import re
from pathlib import Path

import numpy as np
import pandas as pd

from luna.v7 import guard, k9, netguard
from luna.v7.data import captures as cap
from luna.v7.data.instruments import _R1_DTYPES
from luna.v7.data.notices import NoticeVersion
from luna.v7.data.schema import TABLES, IntegrityBlocker
from luna.v7.data.terminal import validate_r12
from luna.v7.rawstore import (
    MANIFEST,
    _canonical,
    _create_once,
    _immutable,
    _integrity,
    _replace_atomic,
    _sha_bytes,
    _writer_lock,
)

__all__ = ["CAPTURES_DIR", "DTYPES", "TABLES_DIR", "TIME_COLUMNS", "MetaStore", "empty_table"]

CAPTURES_DIR = "wayback_captures"
TABLES_DIR = "metadata"
TIME_COLUMNS: dict[str, tuple[str, ...]] = {
    "R11": ("releaseDate", "capture_ts"),
    "R1": ("first_bar_ts", "last_trade_ts", "instance_end_known_at"),
    "R12": ("settlement_ts", "source_timestamp", "known_at")}
DTYPES: dict[str, dict[str, object]] = {
    "R11": {"venue": object, "notice_id": object, "version": "int64", "kind": object,
            "matched_instances": object, "releaseDate": "int64", "archive_url": object, "capture_ts": "int64",
            "content_sha256": object, "scheduled_delist_ts": object},
    "R1": dict(_R1_DTYPES),
    "R12": {"venue": object, "contract_instance_id": object, "settlement_ts": "int64",
            "settlement_price": "float64", "source": object, "source_timestamp": "int64", "known_at": "int64",
            "retrieved_at": "int64", "sha256": object}}
for _t, _d in DTYPES.items():  # R1 carries one extra identity column, quote_asset (AMB-40 (e))
    assert list(_d) == [*TABLES[_t].key, *TABLES[_t].columns, *(["quote_asset"] if _t == "R1" else [])], _t
_SCOPE = re.compile(r"[A-Za-z0-9_]{1,40}\Z", re.ASCII)


def _table(table: str) -> None:
    if table not in DTYPES:
        raise ValueError(f"unknown metadata table {table!r} (one of {sorted(DTYPES)})")


def _scope(scope: str) -> str:
    if not isinstance(scope, str) or not _SCOPE.match(scope):
        raise ValueError(f"invalid scope {scope!r}")
    return scope


def _cell(v: object) -> object:
    if v is None or v is pd.NA or v is pd.NaT:
        return None
    if isinstance(v, bool | np.bool_):
        return bool(v)
    if isinstance(v, int | np.integer):
        return int(v)
    if isinstance(v, float | np.floating):
        return None if math.isnan(float(v)) else float(v)
    return str(v)


def _frame(table: str, columns: list[str], rows: list[list[object]]) -> pd.DataFrame:
    dts = DTYPES[table]
    if columns != list(dts):
        raise IntegrityBlocker(f"{table}: stored columns {columns} are not the S-3 columns")
    out = {}
    for j, (c, dt) in enumerate(dts.items()):
        vals = [r[j] for r in rows]
        if dt == "float64":
            out[c] = pd.Series([np.nan if v is None else float(v) for v in vals], dtype="float64")  # type: ignore[arg-type]
        else:
            out[c] = pd.Series(vals, dtype=dt)  # type: ignore[call-overload]
    return pd.DataFrame(out)


def empty_table(table: str) -> pd.DataFrame:
    """The empty, schema-correct table (R12: valid with zero rows, EC-7)."""
    _table(table)
    return _frame(table, list(DTYPES[table]), [])


def _span(table: str, df: pd.DataFrame) -> tuple[int, int] | None:
    vals = [int(v) for c in TIME_COLUMNS[table] for v in df[c].tolist() if _cell(v) is not None]
    return (min(vals), max(vals) + 1) if vals else None


class MetaStore:
    """The metadata store under ``root`` (module docstring)."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    # ---------------------------------------------------------------------------------------- captures
    @property
    def capture_dir(self) -> Path:
        return self.root / CAPTURES_DIR

    def _capture_manifest(self) -> dict:
        p = self.capture_dir / MANIFEST
        return json.loads(p.read_bytes()) if p.exists() else {"kind": "V7_WAYBACK_CAPTURES", "captures": {}}

    def put_capture(self, c: cap.Capture, *, auth: guard.HoldoutAuthorization | None = None) -> dict:
        """Store one accepted capture once (module docstring); returns its manifest entry."""
        k9.require()
        ts = c.capture_ts
        guard.check_data_span(ts, ts + 1, auth=auth, what=f"wayback capture {c.ts} {c.original}")
        key = f"{c.ts}_{c.sha256}"
        entry = {"ts": c.ts, "original": c.original, "sha256": c.sha256, "bytes": len(c.body),
                 "recv_ts": None if c.recv_ts is None else int(c.recv_ts), "file": key + ".bin"}
        req = {"kind": "metadata_store_write", "capture": key}
        with _writer_lock(self.root):
            man = self._capture_manifest()
            old = man["captures"].get(key)
            if old is not None:
                if old["original"] != c.original:
                    _immutable(f"capture {key}: stored for {old['original']}, refusing {c.original}", req)
                return old
            p = self.capture_dir / entry["file"]
            self.capture_dir.mkdir(parents=True, exist_ok=True)
            if p.exists():  # an orphan (crash before the manifest): adopted only when identical
                if _sha_bytes(p.read_bytes()) != c.sha256:
                    _immutable(f"capture {key}: an unrecorded file with different content exists", req)
            else:
                _create_once(p, c.body)
            man["captures"][key] = entry
            man["captures"] = dict(sorted(man["captures"].items()))
            _replace_atomic(self.capture_dir / MANIFEST, _canonical(man))
        return entry

    def captures(self) -> list[dict]:
        """The manifest entries of every stored capture, sorted by (ts, sha256)."""
        return [dict(v) for _, v in sorted(self._capture_manifest()["captures"].items())]

    def has_capture(self, ts: str, original: str) -> bool:
        return any(e["ts"] == ts and e["original"] == original for e in self.captures())

    def get_capture(self, ts: str, sha256: str, *, auth: guard.HoldoutAuthorization | None = None
                    ) -> cap.Capture:
        """A stored capture, guarded (its time) and verified (its sha256)."""
        k9.require()
        key = f"{ts}_{sha256}"
        e = self._capture_manifest()["captures"].get(key)
        if e is None:
            raise KeyError(f"no stored capture {key}")
        t = cap.capture_ts_ns(ts)
        guard.check_data_span(t, t + 1, auth=auth, what=f"wayback capture {key}")
        p = self.capture_dir / e["file"]
        with open(p, "rb") as f:  # read-only
            data = f.read()
        if _sha_bytes(data) != e["sha256"]:
            _integrity(f"{p}: sha256 {_sha_bytes(data)} differs from the manifest {e['sha256']}", p)
        return cap.Capture(e["ts"], e["original"], data, e["sha256"], e["recv_ts"])

    # ------------------------------------------------------------------------------------------ tables
    @property
    def table_dir(self) -> Path:
        return self.root / TABLES_DIR

    def _table_manifest(self) -> dict:
        p = self.table_dir / MANIFEST
        return json.loads(p.read_bytes()) if p.exists() else {"kind": "V7_METADATA_TABLES", "tables": {}}

    def manifest_sha256(self) -> str | None:
        """sha256 of the tables manifest (the DT4 data-manifest hash of the metadata tables)."""
        p = self.table_dir / MANIFEST
        return _sha_bytes(p.read_bytes()) if p.exists() else None

    def put_table(self, table: str, scope: str, df: pd.DataFrame, *,
                  auth: guard.HoldoutAuthorization | None = None) -> dict:
        """Write one (table, scope) once (module docstring); returns its manifest entry."""
        _table(table)
        _scope(scope)
        k9.require()
        cols = list(DTYPES[table])
        missing = [c for c in cols if c not in df.columns]
        if missing:
            raise IntegrityBlocker(f"{table}: missing columns {missing}")
        df = df[cols]
        if table == "R12":
            validate_r12(df)
        elif len(df) and df.duplicated(list(TABLES[table].key)).any():
            raise IntegrityBlocker(f"{table}: duplicate keys")
        sp = _span(table, df)
        if sp is not None:
            guard.check_data_span(*sp, auth=auth, what=f"metadata table {table} {scope}")
        rows = [[_cell(v) for v in r] for r in df.itertuples(index=False, name=None)]
        body = {"kind": "V7_METADATA_TABLE", "table": table, "scope": scope, "columns": cols, "rows": rows}
        data = _canonical(body)
        csha = _sha_bytes(data)
        key, name = f"{table}/{scope}", f"{table}-{scope}.json"
        req = {"kind": "metadata_store_write", "table": table, "scope": scope}
        with _writer_lock(self.root):
            man = self._table_manifest()
            old = man["tables"].get(key)
            if old is not None:
                if old["content_sha256"] != csha:
                    _immutable(f"metadata {key}: written with content {old['content_sha256']}, refusing "
                               f"{csha}", req)
                return old
            p = self.table_dir / name
            self.table_dir.mkdir(parents=True, exist_ok=True)
            if p.exists():
                if _sha_bytes(p.read_bytes()) != csha:
                    _immutable(f"metadata {key}: an unrecorded file with different content exists", req)
            else:
                _create_once(p, data)
            entry = {"file": name, "sha256": csha, "content_sha256": csha, "rows": len(rows)}
            man["tables"][key] = entry
            man["tables"] = dict(sorted(man["tables"].items()))
            _replace_atomic(self.table_dir / MANIFEST, _canonical(man))
        return entry

    def load_table(self, table: str, scope: str, *, auth: guard.HoldoutAuthorization | None = None
                   ) -> pd.DataFrame:
        """A stored table, verified and guarded; R12 never written = empty (module docstring)."""
        _table(table)
        _scope(scope)
        k9.require()
        e = self._table_manifest()["tables"].get(f"{table}/{scope}")
        if e is None:
            if table == "R12":
                return empty_table("R12")
            raise IntegrityBlocker(f"{table} {scope}: not ingested (no stored table)")
        p = self.table_dir / e["file"]
        with open(p, "rb") as f:  # read-only
            data = f.read()
        if _sha_bytes(data) != e["sha256"]:
            _integrity(f"{p}: sha256 {_sha_bytes(data)} differs from the manifest {e['sha256']}", p)
        rec = json.loads(data)
        if rec.get("kind") != "V7_METADATA_TABLE" or rec.get("table") != table or rec.get("scope") != scope:
            raise IntegrityBlocker(f"{p}: not the {table} {scope} table")
        df = _frame(table, rec["columns"], rec["rows"])
        sp = _span(table, df)
        if sp is not None:
            guard.check_data_span(*sp, auth=auth, what=f"metadata table {table} {scope}")
        if table == "R12":
            validate_r12(df)
        return df

    def load_notice_versions(self, scope: str, *, auth: guard.HoldoutAuthorization | None = None
                             ) -> tuple[NoticeVersion, ...]:
        """The R11 versions of ``scope`` rebuilt from their stored captures (module docstring)."""
        r11 = self.load_table("R11", scope, auth=auth)
        out: list[NoticeVersion] = []
        for r in r11.itertuples(index=False):
            pair = netguard.wayback_pair(str(r.archive_url))
            if pair is None:
                raise IntegrityBlocker(f"R11 {r.notice_id} v{r.version}: archive_url is not an N6 URL")
            c = self.get_capture(pair[0], str(r.content_sha256), auth=auth)
            if c.original != pair[1] or c.capture_ts != int(r.capture_ts):
                raise IntegrityBlocker(f"R11 {r.notice_id} v{r.version}: capture does not match the row")
            t = cap.extract(c.body, c.original)
            kind, _ = cap.classify_kind(t.title)
            if t.release_ts != int(r.releaseDate) or kind != r.kind:
                raise IntegrityBlocker(f"R11 {r.notice_id} v{r.version}: the capture does not reproduce the "
                                       "stored releaseDate / kind")
            out.append(NoticeVersion(str(r.notice_id), int(r.version), kind, t.release_ts, c.capture_ts,  # type: ignore[arg-type]
                                     t.text, c.sha256))
        return tuple(out)
