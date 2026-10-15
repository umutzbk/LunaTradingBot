"""Shared, bounded decision-view store: the outcome-free decision universe computed ONCE per decision and
replayed by every book run (SC-01 part 2 item 8, red-team M1; spec §17, S-10.6; AMB-38).

The U1-U8 gate rows, h-hat, beta-hat, qv24, the S-7.3 staleness and the gated candidate rows with the §9
features at a decision t are the same for every arm, horizon and book run (they depend on the data, the
decision time, ``with_features`` and the code only); arm-specific scores are layered on top by the engine.
``runner.DecisionProvider(store=...)`` reads a view from here before computing it and writes what it computes,
so the S-10.6 driver's books (and the decision logs, the holdout records) replay stored views instead of
recomputing them. It replaces the 2,048-decision LRU that never hit on a sequential replay.

Layout (one directory per key)::

    <root>/<key>/<t>.parquet    the candidate rows (``runstore`` canonical frame encoding, no pickle)
    <root>/<key>/<t>.json       canonical JSON: the view's and the record's other fields (tagged values),
                                the frame's columns / rows and the parquet sha256

``key`` (``view_key``) is the sha256 of the data identity (the raw store's table-manifest sha256s, or an
explicit identity for in-memory data), the universe's symbols and hedge, ``with_features``, the code identity
and ``FORMAT``. Files are written once (``rawstore._create_once``: an existing file is never overwritten; a
second writer of the same t must produce identical bytes, else ``IntegrityBlocker``). A view is stored only
when it round-trips canonically (decode(encode(x)) re-encodes to the same bytes and its frame has a default
``RangeIndex``); otherwise it stays in memory only (recomputed on demand). A stored file that does not decode,
or whose parquet hash differs, is an ``IntegrityBlocker`` (never a silent recompute). The store is a local
cache of this code's own output: anyone who can write ``<root>`` can alter it (as with any working file);
the per-call W_HOLD guard stays in the provider (a stored view is never served for a refused t).

Bound: the store holds one view per decision of the evaluated windows (about 5,000 for W_DEV, on disk); the
provider keeps at most ``max_cached_decisions`` decoded views in memory (LRU, deterministic eviction).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from pathlib import Path

import pandas as pd

from luna.v7.book.engine import DecisionView
from luna.v7.data.schema import IntegrityBlocker
from luna.v7.rawstore import _canonical, _create_once
from luna.v7.runstore import _dec, _decode_frame, _enc, _encode_frame
from luna.v7.universe import GateRow

__all__ = ["FORMAT", "ViewStore", "view_key"]

FORMAT = "decisionview/parquet+json/v1"
_GATE_FIELDS = ("u1", "u2", "u3", "u4", "u5", "u6", "u7", "u8", "reasons", "max_known_at")
_REC_FIELDS = ("stale_reasons", "eligible", "n_cands", "max_known_at")


def view_key(*, data_identity: Mapping[str, str] | str, symbols: Sequence[str], hedge: str,
             with_features: bool, code_id: str) -> str:
    """The store key: sha256 of the canonical JSON of the data identity (``RawStore.manifest_sha256s()`` or
    an explicit string), the sorted symbols, the hedge, ``with_features``, the code identity and FORMAT."""
    data = dict(sorted(data_identity.items())) if isinstance(data_identity, Mapping) else str(data_identity)
    body = {"format": FORMAT, "data": data, "symbols": sorted(str(s) for s in symbols), "hedge": str(hedge),
            "with_features": bool(with_features), "code": str(code_id)}
    return hashlib.sha256(_canonical(body)).hexdigest()


def _gates_enc(g: Mapping[str, GateRow]) -> object:
    return _enc({str(s): tuple(getattr(r, f) for f in _GATE_FIELDS) for s, r in g.items()})


def _gates_dec(v: object) -> dict[str, GateRow]:
    return {s: GateRow(*x) for s, x in _dec(v).items()}  # type: ignore[union-attr]


def _encode(view: DecisionView, record: object) -> dict[str, bytes] | None:
    """The canonical files of (view, record), or None when they cannot be stored exactly."""
    c = view.cands
    if not (isinstance(c.index, pd.RangeIndex) and c.index.start == 0 and c.index.step == 1):
        return None
    try:
        meta, data = _encode_frame("cands", c)
        body = {"kind": "V7_DECISION_VIEW", "format": FORMAT, "t": _enc(int(view.t)),
                "gates": _gates_enc(view.gates), "h_hat": _enc(dict(view.h_hat)),
                "betas": _enc(dict(view.betas)), "qv24": _enc(dict(view.qv24)), "stale": bool(view.stale),
                "cands": meta,
                "record": {f: _enc(getattr(record, f)) for f in _REC_FIELDS}}
    except TypeError:
        return None
    files = {"json": _canonical(body)}
    if data is not None:
        files["parquet"] = data
    return files


def _decode(files: Mapping[str, bytes]) -> tuple[DecisionView, dict]:
    from luna.v7.runner import DecisionRecord  # runtime import: runner imports this module

    rec = json.loads(files["json"])
    if rec.get("kind") != "V7_DECISION_VIEW" or rec.get("format") != FORMAT:
        raise IntegrityBlocker("not a V7 decision view of this format")
    meta = rec["cands"]
    data = files.get("parquet")
    if meta is not None and meta["file"] is not None:
        if data is None or hashlib.sha256(data).hexdigest() != meta["sha256"]:
            raise IntegrityBlocker("decision view: frame file missing or its sha256 differs")
    cands = _decode_frame("cands", meta, data)
    t = int(_dec(rec["t"]))  # type: ignore[arg-type]
    gates = _gates_dec(rec["gates"])
    h_hat, betas, qv24 = (_dec(rec[k]) for k in ("h_hat", "betas", "qv24"))
    view = DecisionView(t, gates, h_hat, betas, qv24, cands, bool(rec["stale"]))  # type: ignore[arg-type]
    r = {f: _dec(v) for f, v in rec["record"].items()}
    record = DecisionRecord(t, gates, h_hat, betas, qv24, bool(rec["stale"]), r["stale_reasons"],  # type: ignore[arg-type]
                            r["eligible"], r["n_cands"], r["max_known_at"])  # type: ignore[arg-type]
    return view, record  # type: ignore[return-value]


class ViewStore:
    """Decision views of one key under ``root`` (module docstring). ``with_features`` is part of the key and
    checked by the provider."""

    def __init__(self, root: str | Path, key: str, *, with_features: bool) -> None:
        if not isinstance(key, str) or len(key) != 64 or any(ch not in "0123456789abcdef" for ch in key):
            raise ValueError(f"not a view key: {key!r}")
        self.dir = Path(root) / key
        self.key, self.with_features = key, bool(with_features)
        self.stats = {"hits": 0, "misses": 0, "writes": 0, "unstorable": 0}

    def _paths(self, t: int) -> tuple[Path, Path]:
        return self.dir / f"{int(t)}.json", self.dir / f"{int(t)}.parquet"

    def get(self, t: int) -> tuple[DecisionView, object] | None:
        pj, pp = self._paths(t)
        if not pj.exists():
            self.stats["misses"] += 1
            return None
        files = {"json": pj.read_bytes()}
        if pp.exists():
            files["parquet"] = pp.read_bytes()
        try:
            view, record = _decode(files)
        except IntegrityBlocker:
            raise
        except (KeyError, ValueError, TypeError) as e:
            raise IntegrityBlocker(f"{pj}: not a valid decision view ({type(e).__name__}: {e})") from None
        if view.t != int(t):
            raise IntegrityBlocker(f"{pj}: holds the view of t = {view.t}")
        self.stats["hits"] += 1
        return view, record

    def put(self, view: DecisionView, record: object) -> bool:
        """Store (view, record) once; False when it cannot be stored exactly (kept in memory only)."""
        files = _encode(view, record)
        if files is None or _encode(*_decode(files)) != files:
            self.stats["unstorable"] += 1
            return False
        pj, pp = self._paths(view.t)
        self.dir.mkdir(parents=True, exist_ok=True)
        if pj.exists():  # a second writer must have produced the same bytes
            if pj.read_bytes() != files["json"] or files.get("parquet", b"") != (pp.read_bytes()
                                                                                 if pp.exists() else b""):
                raise IntegrityBlocker(f"{pj}: a stored view differs from the recomputed one")
            return True
        if "parquet" in files:
            if pp.exists():  # an orphan of an interrupted write: adopted only when identical
                if pp.read_bytes() != files["parquet"]:
                    raise IntegrityBlocker(f"{pp}: an unrecorded frame file with different content exists")
            else:
                _create_once(pp, files["parquet"])
        _create_once(pj, files["json"])  # written last: a view exists only once complete
        self.stats["writes"] += 1
        return True
