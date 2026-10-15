"""Persisted executed books without any executable format (spec §17, DT4, DEC-19; Task 24 open item; Task 26a
fix round 2; AMB-36 (k)).

A ``BookRun`` is written as a canonical, non-executable record set (no pickle anywhere, nothing is ever
unpickled):

    <root>/<output sha256>/run.json              canonical JSON: every scalar field (tagged), per frame its
                                                 columns (name, pandas dtype, storage kind), rows, file sha256
    <root>/<output sha256>/<frame>.parquet       one per non-empty frame (fills, marks, positions,
                                                 funding_flows, daily, decisions)
    <root>/MANIFEST.json                         per run: sha256 of run.json, arm, h, execution, layer, window

Columns: numpy bool / int / uint / float and pandas string / nullable extension dtypes are parquet columns of
their own arrow type; ``object`` columns (tuples of the decision log, ``None``, mixed cells) are parquet
string columns of one canonical tagged-JSON value per cell (``_enc``: tuple / list / dict / str / int / bool /
None / float by ``repr`` (inf and -0.0 exact; every NaN is the canonical NaN, its payload bits are not
kept) / numpy scalars with their type / ``pd.NA``); any other
dtype or cell type is refused at write. The parquet files carry no pandas or arrow schema metadata
(``store_schema=False``), so a reader rebuilds every frame from run.json + plain columns and the
``BookRun`` dataclass explicitly (``decode_run``). Frames get a ``RangeIndex`` (the output identity ignores
the index).

Canonical: ``encode_run(run)`` is a pure function of the run's content; ``put`` verifies that the decoded
run re-encodes to the same bytes and has the same ``runrecord.run_output_sha256`` (else refused), so a run
rebuilt from disk persists to identical files. ``run_file_sha256(run)`` = sha256 of run.json (it covers every
frame file through their sha256): the value the pre-opening record carries and the opening pins
(``dev_ml_run_file_sha256``).

``RunStore.load(sha, *, file_sha256, auth=None)`` requires the pinned run.json sha256: the run's window is
guarded (``guard.check_data_span``), run.json and every frame file must hash to their recorded values, the
rebuilt run must have the manifest's window and the output sha256 ``sha``; any failure is an
``IntegrityBlocker`` (K9-logged). ``load_pinned(auth)`` takes both hashes from the verified opening pin
(``guard.verify_authorization``): the path ``runner_select.holdout_inputs(dev_run_store=...)`` uses.
"""

from __future__ import annotations

import dataclasses
import hashlib
import io
import json
import re
from collections.abc import Mapping
from pathlib import Path, PurePosixPath, PureWindowsPath

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from luna.v7 import guard, k9
from luna.v7.book.engine import BookRun
from luna.v7.data.schema import IntegrityBlocker
from luna.v7.rawstore import MANIFEST, _canonical, _create_once, _integrity, _replace_atomic, _writer_lock
from luna.v7.runrecord import run_output_sha256

__all__ = ["FORMAT", "FRAMES", "RUN_FILE", "RunStore", "decode_run", "encode_run", "run_file_sha256"]

FORMAT = "bookrun/parquet+json/v1"
RUN_FILE = "run.json"
FRAMES = ("fills", "marks", "positions", "funding_flows", "daily", "decisions")
_SHA = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_SCALARS = tuple(f.name for f in dataclasses.fields(BookRun) if f.name not in FRAMES)


# ------------------------------------------------------------------------------------- tagged JSON values


def _enc(v: object) -> object:
    """A canonical JSON value that ``_dec`` turns back into an equal object of the same type."""
    if v is None or type(v) is str:
        return v
    if v is pd.NA:
        return {"na": 1}
    if isinstance(v, np.generic):
        name = type(v).__name__
        if not hasattr(np, name) or getattr(np, name) is not type(v):
            raise TypeError(f"numpy scalar type {name} cannot be persisted")
        return {"np": name, "v": _enc(v.item())}
    if type(v) is bool:
        return v
    if type(v) is int:
        return {"i": str(v)}
    if type(v) is float:
        return {"f": repr(v)}
    if type(v) is tuple:
        return {"t": [_enc(x) for x in v]}
    if type(v) is list:
        return {"l": [_enc(x) for x in v]}
    if type(v) is dict:
        return {"d": [[_enc(k), _enc(x)] for k, x in v.items()]}
    raise TypeError(f"a {type(v).__name__} value cannot be persisted in a BookRun record")


def _dec(v: object) -> object:
    if v is None or isinstance(v, str | bool):
        return v
    if not isinstance(v, dict) or len(v) not in (1, 2):
        raise IntegrityBlocker(f"malformed tagged value {v!r}")
    if "na" in v:
        return pd.NA
    if "np" in v:
        typ = getattr(np, str(v["np"]), None)
        if not (isinstance(typ, type) and issubclass(typ, np.generic)):
            raise IntegrityBlocker(f"unknown numpy scalar type {v['np']!r}")
        return typ(_dec(v["v"]))
    (tag, x), = v.items()
    if tag == "i":
        return int(x)
    if tag == "f":
        return float(x)
    if tag == "t":
        return tuple(_dec(y) for y in x)
    if tag == "l":
        return [_dec(y) for y in x]
    if tag == "d":
        return {_dec(k): _dec(y) for k, y in x}  # type: ignore[misc]
    raise IntegrityBlocker(f"unknown tag {tag!r}")


def _json_cell(v: object) -> str:
    return json.dumps(_enc(v), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


# -------------------------------------------------------------------------------------------- frames


_EXT = ("Int8", "Int16", "Int32", "Int64", "UInt8", "UInt16", "UInt32", "UInt64", "Float32", "Float64",
        "boolean")


def _kind(s: pd.Series) -> str:
    dt = s.dtype
    if isinstance(dt, np.dtype) and dt.kind in "biuf":
        return "numpy"
    if isinstance(dt, pd.StringDtype) or isinstance(dt, pd.api.extensions.ExtensionDtype) and str(dt) in _EXT:
        return "ext"
    if isinstance(dt, np.dtype) and dt.kind == "O":
        return "json"
    raise TypeError(f"column {s.name!r}: dtype {dt} cannot be persisted")


def _decode_kind(name: str, dtype: str, kind: str) -> None:
    """The writer's dtype allowlist (``_kind``) re-applied to a run.json column entry (Task 26a review minor:
    a pinned but crafted record must not decode into a dtype the writer refuses: category, Sparse, datetime,
    a numpy object column declared numpy, ...)."""
    ok = False
    try:
        if kind == "numpy":
            ok = dtype in {np.dtype(c).name for c in "?bhilqBHILQefd"} and np.dtype(dtype).kind in "biuf"
        elif kind == "ext":
            ok = dtype in _EXT or isinstance(pd.api.types.pandas_dtype(dtype), pd.StringDtype)
        elif kind == "json":
            ok = dtype == "object"
    except (TypeError, ValueError):
        ok = False
    if not ok:
        raise IntegrityBlocker(f"{name}: column dtype {dtype!r} / storage {kind!r} is not a persistable kind")


def _encode_frame(name: str, df: pd.DataFrame | None) -> tuple[dict | None, bytes | None]:
    if df is None:
        return None, None
    if not isinstance(df, pd.DataFrame):
        raise TypeError(f"{name}: not a DataFrame")
    cols = [str(c) for c in df.columns]
    if any(not isinstance(c, str) for c in df.columns) or len(set(cols)) != len(cols):
        raise TypeError(f"{name}: column names must be unique strings")
    meta: dict = {"rows": int(len(df)), "columns": [], "file": None, "sha256": None}
    if not cols:
        return meta, None
    arrays = []
    for c in cols:
        s = df[c]
        kind = _kind(s)
        meta["columns"].append([c, str(s.dtype), kind])
        if kind == "numpy":
            arrays.append(pa.array(s.to_numpy()))
        elif kind == "ext":
            arrays.append(pa.array([None if pd.isna(x) else x for x in s.tolist()],
                                   type=pa.string() if isinstance(s.dtype, pd.StringDtype) else None))
        else:
            arrays.append(pa.array([_json_cell(x) for x in s.tolist()], type=pa.string()))
    tbl = pa.Table.from_arrays(arrays, names=cols)
    buf = io.BytesIO()
    pq.write_table(tbl, buf, compression="zstd", compression_level=9, use_dictionary=False,
                   write_statistics=False, store_schema=False)
    data = buf.getvalue()
    meta["file"] = f"{name}.parquet"
    meta["sha256"] = hashlib.sha256(data).hexdigest()
    return meta, data


def _decode_frame(name: str, meta: dict | None, data: bytes | None) -> pd.DataFrame | None:
    if meta is None:
        return None
    if not meta["columns"]:
        return pd.DataFrame(index=pd.RangeIndex(int(meta["rows"])))
    tbl = pq.read_table(io.BytesIO(data or b""))
    names = [c for c, _, _ in meta["columns"]]
    if tbl.column_names != names or tbl.num_rows != int(meta["rows"]):
        raise IntegrityBlocker(f"{name}: stored columns / rows differ from run.json")
    out: dict[str, object] = {}
    for c, dtype, kind in meta["columns"]:
        _decode_kind(f"{name}.{c}", str(dtype), str(kind))
        col = tbl.column(c)
        if kind == "numpy":
            out[c] = np.asarray(col.to_numpy(zero_copy_only=False)).astype(np.dtype(dtype), copy=True)
        elif kind == "ext":
            out[c] = pd.array(col.to_pylist(), dtype=dtype)
        elif kind == "json":
            vals = [_dec(json.loads(x)) for x in col.to_pylist()]
            arr = np.empty(len(vals), dtype=object)
            arr[:] = vals
            out[c] = arr
        else:
            raise IntegrityBlocker(f"{name}.{c}: unknown storage kind {kind!r}")
    return pd.DataFrame(out, columns=names)


# --------------------------------------------------------------------------------------- record set


def encode_run(run: BookRun) -> dict[str, bytes]:
    """The canonical files of ``run``: {file name: bytes} (``RUN_FILE`` + one parquet per non-empty frame)."""
    if not isinstance(run, BookRun):
        raise TypeError("encode_run takes an executed BookRun")
    files: dict[str, bytes] = {}
    frames: dict[str, dict | None] = {}
    for part in FRAMES:
        meta, data = _encode_frame(part, getattr(run, part))
        frames[part] = meta
        if data is not None:
            files[meta["file"]] = data  # type: ignore[index]
    rec = {"kind": "V7_BOOK_RUN", "format": FORMAT, "output_sha256": run_output_sha256(run),
           "scalars": {f: _enc(getattr(run, f)) for f in _SCALARS}, "frames": frames}
    files[RUN_FILE] = _canonical(rec)
    return files


def decode_run(files: Mapping[str, bytes]) -> BookRun:
    """The ``BookRun`` rebuilt from its canonical files (explicit dataclass construction; every frame file
    must hash to its run.json entry)."""
    rec = json.loads(files[RUN_FILE])
    if rec.get("kind") != "V7_BOOK_RUN" or rec.get("format") != FORMAT:
        raise IntegrityBlocker("not a V7 BookRun record of this format")
    if set(rec["scalars"]) != set(_SCALARS) or set(rec["frames"]) != set(FRAMES):
        raise IntegrityBlocker("BookRun record fields differ from the BookRun dataclass")
    kw: dict[str, object] = {f: _dec(v) for f, v in rec["scalars"].items()}
    for part in FRAMES:
        meta = rec["frames"][part]
        data = None
        if meta is not None and meta["file"] is not None:
            data = files.get(meta["file"])
            if data is None or hashlib.sha256(data).hexdigest() != meta["sha256"]:
                raise IntegrityBlocker(f"{part}: frame file missing or its sha256 differs from run.json")
        kw[part] = _decode_frame(part, meta, data)
    return BookRun(**kw)  # type: ignore[arg-type]


def run_file_sha256(run: BookRun) -> str:
    """sha256 of the canonical run.json of ``run`` (covers every frame file): the pinned file identity."""
    return hashlib.sha256(encode_run(run)[RUN_FILE]).hexdigest()


# ------------------------------------------------------------------------------------------------ store


def _plain_name(name: object) -> bool:
    """A run-manifest file name is a plain file name of the run directory: no separator, no drive prefix
    (``C:x``), not absolute, not hidden (Task 26a review minor)."""
    if not isinstance(name, str) or not name or name.startswith("."):
        return False
    if any(c in name for c in ("/", "\\", ":")):
        return False
    w = PureWindowsPath(name)
    return not (w.drive or w.is_absolute() or PurePosixPath(name).is_absolute()) and w.name == name


class RunStore:
    """Executed ``BookRun`` objects written once under ``root`` (module docstring). One writer per root."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def manifest(self) -> dict:
        p = self.root / MANIFEST
        return json.loads(p.read_bytes()) if p.exists() else {"kind": "V7_BOOK_RUNS", "format": FORMAT,
                                                                "runs": {}}

    def manifest_sha256(self) -> str | None:
        p = self.root / MANIFEST
        return hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else None

    def put(self, run: BookRun) -> str:
        """Persist ``run`` (canonical; verified to round-trip byte-identically); returns its output sha256."""
        files = encode_run(run)
        sha = run_output_sha256(run)
        back = decode_run(files)
        if encode_run(back) != files or run_output_sha256(back) != sha:
            raise IntegrityBlocker("BookRun does not round-trip canonically through its record (refused)")
        with _writer_lock(self.root):
            man = self.manifest()
            if sha in man["runs"]:
                return sha
            d = self.root / sha
            d.mkdir(parents=True, exist_ok=True)
            for name in sorted(files):
                _create_once(d / name, files[name])
            man["runs"][sha] = {"dir": sha, "sha256": hashlib.sha256(files[RUN_FILE]).hexdigest(),
                                "files": sorted(files), "arm": run.arm, "h": int(run.h_star),
                                "execution": run.execution, "layer": run.layer,
                                "window": [int(run.window[0]), int(run.window[1])],
                                "research": bool(run.research)}
            man["runs"] = dict(sorted(man["runs"].items()))
            _replace_atomic(self.root / MANIFEST, _canonical(man))
        return sha

    def file_sha256(self, output_sha256: str) -> str:
        """The run.json sha256 the store's (unpinned, rewritable) manifest records: information only, NOT a
        pin. A caller that must trust a run passes the pinned value to ``load`` / uses ``load_pinned``."""
        return str(self.manifest()["runs"][output_sha256]["sha256"])

    def load(self, output_sha256: str, *, file_sha256: str,
             auth: guard.HoldoutAuthorization | None = None) -> BookRun:
        """The persisted run ``output_sha256`` whose run.json has the (pinned) sha256 ``file_sha256``."""
        k9.require()
        for v in (output_sha256, file_sha256):
            if not isinstance(v, str) or not _SHA.match(v):
                raise ValueError(f"not a sha256: {v!r}")
        e = self.manifest()["runs"].get(output_sha256)
        if e is None:
            raise KeyError(f"no persisted run {output_sha256}")
        window = (int(e["window"][0]), int(e["window"][1]))
        guard.check_data_span(*window, auth=auth, what=f"persisted run {output_sha256[:12]}")
        d = self.root / str(e["dir"])
        if e["sha256"] != file_sha256:
            _integrity(f"{d}: manifest run.json sha256 {e['sha256']} is not the pinned {file_sha256}", d)
        files: dict[str, bytes] = {}
        for name in e["files"]:
            if not _plain_name(name):
                _integrity(f"{d}: invalid file name {name!r} in the run manifest", d)
            with open(d / name, "rb") as f:
                files[name] = f.read()
        if hashlib.sha256(files.get(RUN_FILE, b"")).hexdigest() != file_sha256:
            _integrity(f"{d / RUN_FILE}: sha256 differs from the pinned {file_sha256}", d / RUN_FILE)
        try:
            run = decode_run(files)
        except (IntegrityBlocker, KeyError, ValueError, TypeError, pa.ArrowException) as err:
            _integrity(f"{d}: not a valid BookRun record ({type(err).__name__}: {err})", d)
        if tuple(int(x) for x in run.window) != window or run_output_sha256(run) != output_sha256:
            _integrity(f"{d}: the record is not the BookRun {output_sha256} of window {window}", d)
        return run

    def load_pinned(self, auth: guard.HoldoutAuthorization) -> BookRun:
        """The W_DEV ML* run pinned at the opening; both hashes come from the verified pin."""
        pin = guard.verify_authorization(auth)
        return self.load(pin["dev_ml_run_sha256"], file_sha256=pin["dev_ml_run_file_sha256"], auth=auth)
