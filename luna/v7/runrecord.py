"""DT4 run records (spec §3 DT4, §17 reproducibility; AMB-26, AMB-27 (c)).

A run record holds the code commit, the config sha256, the data manifest sha256s, the seeds, the library
versions (and the ``uv.lock`` sha256) and, per fit, the solver convergence status (recorded, never raised,
AMB-26). ``run_record`` is pure (every value is passed in; no wall-clock: a timestamp, if wanted, is the
caller's ``extra``). The only I/O is explicit: ``write_run_record`` (exclusive create at a caller-supplied
path: a record is immutable), ``file_sha256`` and ``code_commit`` (git, read-only).
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

from luna.v7 import models, stats
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
    r = _git(Path(repo or code_repo()), "rev-parse", "HEAD")
    out = r.stdout.decode().strip()
    if r.returncode != 0 or not _COMMIT.match(out):
        raise ValueError("code commit not determinable (not a git checkout)")
    return out


def library_versions() -> dict[str, str]:
    """Installed versions of the numerical libraries and the Python version."""
    out = {"python": platform.python_version()}
    for lib in LIBRARIES:
        out[lib] = metadata.version(lib)
    return out


def default_seeds() -> dict[str, int]:
    """The frozen seeds in use: model fits (S-10.5) and the bootstrap (S-13.1)."""
    return {"models": int(models.SEED), "bootstrap": int(stats.SEED)}


def run_record(*, commit: str, config: Mapping, data_manifests: Mapping[str, str], seeds: Mapping[str, int],
               uv_lock_sha256: str, libraries: Mapping[str, str], convergence: Sequence[Mapping],
               extra: Mapping | None = None) -> dict:
    """The DT4 record (validated; fail closed on a malformed commit / sha256 / missing manifest). Convergence
    rows (``runner.fold_convergence``) are stored as given, with a summary of the non-converged fits."""
    if not _COMMIT.match(str(commit)):
        raise ValueError("commit must be a 40-hex git commit")
    if not data_manifests:
        raise ValueError("data manifest sha256s are required")
    for name, sha in {**data_manifests, "uv.lock": uv_lock_sha256}.items():
        if not _SHA.match(str(sha)):
            raise ValueError(f"{name}: not a sha256")
    if not seeds or not all(isinstance(v, int) and not isinstance(v, bool) for v in seeds.values()):
        raise ValueError("seeds must be a non-empty mapping of ints")
    conv = [dict(r) for r in convergence]
    bad = [r for r in conv if r.get("converged") is False]
    rec = {
        "dt4": "runs",
        "code_commit": str(commit),
        "config": json.loads(canonical_json(dict(config))),
        "config_sha256": config_sha256(config),
        "data_manifests": dict(sorted(data_manifests.items())),
        "seeds": dict(sorted(seeds.items())),
        "uv_lock_sha256": str(uv_lock_sha256),
        "libraries": dict(sorted(libraries.items())),
        "convergence": json.loads(canonical_json(conv)),
        "convergence_summary": {"fits": len(conv), "not_converged": len(bad)},
        "extra": json.loads(canonical_json(dict(extra or {}))),
    }
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


__all__ = ["canonical_json", "code_commit", "config_sha256", "default_seeds", "file_sha256",
           "library_versions", "run_record", "write_run_record"]
