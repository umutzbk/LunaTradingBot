"""DT4 run records (spec §3 DT4, §17 reproducibility; AMB-26, AMB-27 (c)).

A run record (keyed by a deterministic ``run_id``, the ``data.schema`` DT4 columns) holds the code commit and
working-tree state, the config sha256, the data manifest sha256s, the seeds, the library versions (and the
``uv.lock`` sha256), the identity and output sha256 of every executed book and, per fit, the solver
convergence status (recorded, never raised, AMB-26). ``run_record`` is pure (every value is passed in; no
wall-clock: a timestamp, if wanted, is the caller's ``extra``). The only I/O is explicit:
``write_run_record`` (exclusive create at a caller-supplied path: a record is immutable), ``file_sha256``
and ``code_state`` / ``code_commit`` (git, read-only).
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
from collections.abc import Mapping, Sequence
from importlib import metadata
from pathlib import Path

import pandas as pd

from luna.v7 import models, stats
from luna.v7.book.engine import BookRun
from luna.v7.data import schema
from luna.v7.spec import _git, code_repo

LIBRARIES = ("numpy", "pandas", "scikit-learn", "lightgbm", "scipy")
_SHA = re.compile(r"\A[0-9a-f]{64}\Z")
_COMMIT = re.compile(r"\A[0-9a-f]{40}\Z")


def canonical_json(obj: object) -> str:
    """Deterministic JSON (sorted keys, no NaN, compact separators)."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), allow_nan=False, default=_default)


def _default(o: object) -> object:
    if isinstance(o, tuple | list):
        return list(o)
    if hasattr(o, "item"):  # numpy scalars
        return o.item()  # type: ignore[attr-defined]
    raise TypeError(f"not JSON-serialisable in a run record: {type(o).__name__}")


def config_sha256(config: Mapping) -> str:
    return hashlib.sha256(canonical_json(dict(config)).encode()).hexdigest()


def file_sha256(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def code_commit(repo: Path | None = None) -> str:
    """HEAD commit of the code repository (read-only git call)."""
    return code_state(repo)[0]


def code_state(repo: Path | None = None) -> tuple[str, bool]:
    """(HEAD commit, working tree dirty) of the code repository (read-only git calls)."""
    root = Path(repo or code_repo())
    r = _git(root, "rev-parse", "HEAD")
    out = r.stdout.decode().strip()
    if r.returncode != 0 or not _COMMIT.match(out):
        raise ValueError("code commit not determinable (not a git checkout)")
    st = _git(root, "status", "--porcelain")
    if st.returncode != 0:
        raise ValueError("working-tree state not determinable")
    return out, bool(st.stdout.decode().strip())


def _frame_sha(h: hashlib._Hash, df: pd.DataFrame | None) -> None:
    if df is None:
        h.update(b"<none>")
        return
    h.update(repr([(str(c), str(t)) for c, t in df.dtypes.items()]).encode())
    try:
        h.update(pd.util.hash_pandas_object(df, index=False).to_numpy().tobytes())
    except TypeError:  # unhashable cells (tuples in the decision log)
        h.update(df.to_csv(index=False, lineterminator="\n").encode())


def run_output_sha256(run: BookRun) -> str:
    """sha256 of an executed book's outputs (fills, marks, positions, funding flows, daily table, decisions,
    integrity findings and the INVALID state): the output identity recorded in DT4."""
    h = hashlib.sha256()
    for part in ("fills", "marks", "positions", "funding_flows", "daily", "decisions"):
        h.update(part.encode())
        _frame_sha(h, getattr(run, part))
    h.update(repr((run.integrity, run.invalid, run.invalid_detail, run.invalid_ts)).encode())
    return h.hexdigest()


def run_identity(run: BookRun) -> dict:
    """The identity of one executed book: arm, h*, window, execution model, equity layer, output sha256."""
    return {"arm": run.arm, "h": int(run.h_star), "window": [int(run.window[0]), int(run.window[1])],
            "execution": run.execution, "layer": run.layer, "valid": bool(run.valid),
            "output_sha256": run_output_sha256(run)}


def library_versions() -> dict[str, str]:
    """Installed versions of the numerical libraries and the Python version."""
    out = {"python": platform.python_version()}
    for lib in LIBRARIES:
        out[lib] = metadata.version(lib)
    return out


def default_seeds() -> dict[str, int]:
    """The frozen seeds in use: model fits (S-10.5) and the bootstrap (S-13.1)."""
    return {"models": int(models.SEED), "bootstrap": int(stats.SEED)}


def run_record(*, commit: str, dirty: bool, config: Mapping, data_manifests: Mapping[str, str],
               seeds: Mapping[str, int], uv_lock_sha256: str, libraries: Mapping[str, str],
               convergence: Sequence[Mapping], runs: Sequence[BookRun], research: bool = False,
               extra: Mapping | None = None) -> dict:
    """The DT4 record with the ``data.schema`` DT4 columns (``run_id`` key, ``code_commit``,
    ``config_sha256``, ``data_manifest_sha256s``, ``seeds``, ``library_versions``) plus the working-tree
    state, the ``uv.lock`` sha256, the identity and output sha256 of every executed book, the per-fit
    convergence rows (with a not-converged count) and ``extra``. ``run_id`` is the sha256 of the canonical
    JSON of everything else (deterministic). Validated (malformed commit / sha256, missing manifest or runs:
    refused) and checked with ``schema.validate("DT4", ...)``. A record that claims a research run
    (``research=True``) is refused from a dirty working tree; otherwise ``code_dirty`` records it."""
    if not _COMMIT.match(str(commit)):
        raise ValueError("commit must be a 40-hex git commit")
    if not isinstance(dirty, bool):
        raise ValueError("dirty must be a bool (code_state)")
    if research and dirty:
        raise ValueError("a research run record needs a clean working tree (code_dirty)")
    if not data_manifests:
        raise ValueError("data manifest sha256s are required")
    for name, sha in {**data_manifests, "uv.lock": uv_lock_sha256}.items():
        if not _SHA.match(str(sha)):
            raise ValueError(f"{name}: not a sha256")
    if not seeds or not all(isinstance(v, int) and not isinstance(v, bool) for v in seeds.values()):
        raise ValueError("seeds must be a non-empty mapping of ints")
    if not runs or not all(isinstance(r, BookRun) for r in runs):
        raise ValueError("runs: the executed books this record covers are required")
    conv = [dict(r) for r in convergence]
    bad = [r for r in conv if r.get("converged") is False]
    body = {
        "dt4": "runs",
        "code_commit": str(commit),
        "code_dirty": bool(dirty),
        "research": bool(research),
        "config": json.loads(canonical_json(dict(config))),
        "config_sha256": config_sha256(config),
        "data_manifest_sha256s": dict(sorted(data_manifests.items())),
        "seeds": dict(sorted(seeds.items())),
        "uv_lock_sha256": str(uv_lock_sha256),
        "library_versions": dict(sorted(libraries.items())),
        "runs": [run_identity(r) for r in runs],
        "convergence": json.loads(canonical_json(conv)),
        "convergence_summary": {"fits": len(conv), "not_converged": len(bad)},
        "extra": json.loads(canonical_json(dict(extra or {}))),
    }
    rec = {"run_id": hashlib.sha256(canonical_json(body).encode()).hexdigest(), **body}
    schema.validate("DT4", pd.DataFrame([{k: [v] if isinstance(v, dict | list) else v
                                          for k, v in rec.items()}]))
    return rec


def write_run_record(path: str | Path, record: Mapping) -> Path:
    """Write the record as canonical JSON to ``path`` (exclusive create; an existing record is never
    overwritten). Returns the path."""
    p = Path(path)
    text = canonical_json(dict(record)) + "\n"
    fd = os.open(p, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    return p


__all__ = ["canonical_json", "code_commit", "code_state", "config_sha256", "default_seeds", "file_sha256",
           "library_versions", "run_identity", "run_output_sha256", "run_record", "write_run_record"]
