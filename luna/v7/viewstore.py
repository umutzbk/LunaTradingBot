"""Shared, bounded decision-view store: the outcome-free decision universe computed ONCE per decision and
replayed by every book run (SC-01 part 2 item 8, red-team M1; spec §17, S-10.6; AMB-38).

The U1-U8 gate rows, h-hat, beta-hat, qv24, the S-7.3 staleness and the gated candidate rows with the §9
features at a decision t are the same for every arm, horizon and book run (they depend on the data, the
decision time, ``with_features`` and the code only); arm-specific scores are layered on top by the engine.
``runner.DecisionProvider(store=...)`` reads a view from here before computing it and writes what it computes,
so the S-10.6 driver's books (and the decision logs, the holdout records) replay stored views instead of
recomputing them. It replaces the 2,048-decision LRU that never hit on a sequential replay.

Layout (one store = one key)::

    <root>/KEY.json             canonical JSON of the key body, written once, verified on every open
    <root>/<t>.parquet          the candidate rows (``runstore`` canonical frame encoding, no pickle)
    <root>/<t>.json             canonical JSON: the view's and the record's other fields (tagged values),
                                the frame's columns / rows and the parquet sha256

The key body (``runner.ResearchUniverse.view_identity``; fix round 1, I2) is derived by the code from the
universe itself, never passed as a free string: the data identity of its source (the raw store's
table-manifest sha256s and a hash of the per-symbol metadata, or a content hash of in-memory inputs), the
history start (the funding annotation and U3 depend on it), the symbols, the hedge, ``with_features``, the
code identity (``code_identity``: sha256 of every ``luna/v7`` source file, LF-normalised, in path order) and
``FORMAT`` and the library versions (``library_versions``: Python, numpy, pandas, pyarrow, scikit-learn,
lightgbm; Task 26c). ``bind(body)`` writes ``KEY.json`` on first use and refuses (``IntegrityBlocker``) a
store holding another body: a store filled for one universe / history start / code / library set is never
read by another. Files are written once (``rawstore._create_once``: an existing file is never overwritten; a
second writer of the same t must produce identical bytes, else ``IntegrityBlocker``). A view is stored only
when it round-trips canonically (decode(encode(x)) re-encodes to the same bytes and its frame has a default
``RangeIndex``); otherwise it stays in memory only (recomputed on demand). A stored file that does not decode,
or whose parquet hash differs, is an ``IntegrityBlocker`` (never a silent recompute). The store is a local
cache of this code's own output: anyone who can write ``<root>`` can alter it (as with any working file);
the per-call W_HOLD guard stays in the provider (a stored view is never served for a refused t), and
``get`` itself passes every t through ``guard.check_data_span`` (red-team r2 m4: a stored W_HOLD view, outcome
free but sealed, is readable only with the verified opening and never after the verdict; K9-logged).

Bound: the store holds one view per decision of the evaluated windows (about 5,000 for W_DEV, on disk); the
provider keeps at most ``max_cached_decisions`` decoded views in memory (LRU, deterministic eviction).
"""

from __future__ import annotations

import hashlib
import json
import platform
from collections.abc import Mapping
from importlib import metadata
from pathlib import Path

import pandas as pd

from luna.v7 import guard
from luna.v7.book.engine import DecisionView
from luna.v7.data.schema import IntegrityBlocker
from luna.v7.rawstore import _canonical, _create_once
from luna.v7.runstore import _dec, _decode_frame, _enc, _encode_frame
from luna.v7.universe import GateRow

__all__ = ["FORMAT", "KEY_FILE", "LIBRARIES", "ViewStore", "code_identity", "key_of", "library_versions"]

FORMAT = "decisionview/parquet+json/v3"  # v3 (Task 26c): the key body carries the library versions
KEY_FILE = "KEY.json"
LIBRARIES = ("numpy", "pandas", "pyarrow", "scikit-learn", "lightgbm")
_CODE_ID: list[str] = []
_LIBS: list[dict[str, str]] = []
_GATE_FIELDS = ("u1", "u2", "u3", "u4", "u5", "u6", "u7", "u8", "reasons", "max_known_at")
_REC_FIELDS = ("stale_reasons", "eligible", "n_cands", "max_known_at")


def code_identity() -> str:
    """sha256 of the ``luna/v7`` sources: every ``*.py`` file below the package in sorted relative-path
    order, path and LF-normalised content; computed once per process."""
    if not _CODE_ID:
        root = Path(__file__).resolve().parent
        h = hashlib.sha256(FORMAT.encode())
        for p in sorted(root.rglob("*.py"), key=lambda q: q.relative_to(root).as_posix()):
            h.update(p.relative_to(root).as_posix().encode() + b"\0")
            h.update(p.read_bytes().replace(b"\r\n", b"\n") + b"\0")
        _CODE_ID.append(h.hexdigest())
    return _CODE_ID[0]


def library_versions() -> dict[str, str]:
    """The Python version and the installed versions of ``LIBRARIES`` (the computations a stored view holds
    depend on them as much as on the code; Task 26c carried item 2); read once per process."""
    if not _LIBS:
        out = {"python": platform.python_version()}
        for lib in LIBRARIES:
            out[lib] = metadata.version(lib)
        _LIBS.append(out)
    return dict(_LIBS[0])


def key_of(body: Mapping) -> str:
    return hashlib.sha256(_canonical(dict(body))).hexdigest()


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
    """The decision views of ONE key body under ``root`` (module docstring). A provider / ``decision_pass``
    binds it to its universe's body before any read or write."""

    def __init__(self, root: str | Path) -> None:
        self.dir = Path(root)
        self.body: dict | None = None
        self.stats = {"hits": 0, "misses": 0, "writes": 0, "unstorable": 0}

    @property
    def with_features(self) -> bool | None:
        return None if self.body is None else bool(self.body["with_features"])

    @property
    def key(self) -> str | None:
        return None if self.body is None else key_of(self.body)

    def bind(self, body: Mapping) -> None:
        """Bind to ``body``: written to ``KEY.json`` on first use, else it must equal the stored one."""
        data = _canonical(dict(body))
        if self.body is not None and _canonical(self.body) != data:
            raise IntegrityBlocker(f"{self.dir}: view store already bound to another universe")
        p = self.dir / KEY_FILE
        if p.exists():
            if p.read_bytes() != data:
                raise IntegrityBlocker(f"{p}: the view store holds the views of another key (universe, data, "
                                       f"history start, with_features or code): {p.read_bytes()[:300]!r}")
        else:
            self.dir.mkdir(parents=True, exist_ok=True)
            _create_once(p, data)
        self.body = json.loads(data)

    def _bound(self) -> None:
        if self.body is None:
            raise ValueError("view store not bound to a universe key (ViewStore.bind)")

    def has(self, t: int) -> bool:
        """Whether a view of t is stored (no decoding)."""
        self._bound()
        return self._paths(t)[0].exists()

    def _paths(self, t: int) -> tuple[Path, Path]:
        return self.dir / f"{int(t)}.json", self.dir / f"{int(t)}.parquet"

    def get(self, t: int, *, auth: object = None) -> tuple[DecisionView, object] | None:
        """The stored (view, record) of t, else None. t passes the data guard first (W_HOLD needs the
        verified opening ``auth``, as in ``runner.decision_pass``; M6 / W_FWD7 stay refused)."""
        guard.check_data_span(int(t), int(t) + 1, auth=auth, what="stored decision view")
        self._bound()
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
        self._bound()
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
