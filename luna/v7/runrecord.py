"""DT4 run records (spec §3 DT4, §17 reproducibility; AMB-26, AMB-27 (c)).

A run record (keyed by a deterministic ``run_id``, the ``data.schema`` DT4 columns) holds the code commit and
working-tree state, the config sha256, the data manifest sha256s, the seeds, the library versions (and the
``uv.lock`` sha256), the identity and output sha256 of every executed book, the sha256 of every leakage report
(``leakage.LeakageReport`` / ``HoldoutLeakageReport``, SC-02, AMB-33) and, per fit, the solver convergence
status (recorded, never raised, AMB-26); for a holdout run the opening summary (``guard.opening_summary``:
opening id, pin, every logged resume attempt, verdict marker; ruling R45). ``run_record`` is pure (every
value is passed in; no wall-clock: a timestamp, if wanted, is the caller's ``extra``). The only I/O is
explicit: ``write_run_record`` (exclusive create at a caller-supplied path: a record is immutable),
``file_sha256`` and ``code_state`` / ``code_commit`` (git, read-only).
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
    """The identity of one executed book: arm, h*, window, execution model, equity layer, validity, whether
    a research entry point produced it, output sha256."""
    return {"arm": run.arm, "h": int(run.h_star), "window": [int(run.window[0]), int(run.window[1])],
            "execution": run.execution, "layer": run.layer, "valid": bool(run.valid),
            "research": bool(run.research), "output_sha256": run_output_sha256(run)}


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
               extra: Mapping | None = None, leakage_reports: Sequence = (),
               opening: Mapping | None = None, kraken: object = None,
               a6_resolutions: Sequence[Mapping] | None = None, store: object = None,
               data_window: tuple[int, int] | None = None) -> dict:
    """The DT4 record with the ``data.schema`` DT4 columns (``run_id`` key, ``code_commit``,
    ``config_sha256``, ``data_manifest_sha256s``, ``seeds``, ``library_versions``) plus the working-tree
    state, the ``uv.lock`` sha256, the identity and output sha256 of every executed book, the per-fit
    convergence rows (with a not-converged count), the leakage reports (kind, sha256, passed / findings; each
    must verify: ``leakage.verify_report``) and ``extra``. ``run_id`` is the sha256 of the canonical
    JSON of everything else (deterministic). Validated (malformed commit / sha256, missing manifest or runs:
    refused) and checked with ``schema.validate("DT4", ...)``. A research record is refused from a dirty
    working tree; otherwise ``code_dirty`` records it. The record is a research record when any covered run
    comes from a research entry point (``BookRun.research``, set by ``runner.run_book``, so also every
    ``dev_selection`` book and every ``holdout_inputs`` run) or the caller passes ``research=True``; the
    caller's flag can never unset it (DK-114 / red-team m10: no longer a caller convention). ``kraken``: the
    ``kraken.KrakenComparison`` of the opening (S-1.6 / L11); its frozen V5 manifest sha256 and V6 artefact
    hashes join ``data_manifest_sha256s`` and its exploratory block is recorded as ``kraken_exploratory``.
    ``a6_resolutions``: A-6 (AMENDMENTS-2026-10-16), the store's DEC-33 conflict resolutions inside the data
    window (``RawStore.a6_resolutions(lo, hi)``), recorded as ``a6_resolutions``; required (fail closed) when
    the manifests come from a raw store (they name ``ingest_ledger_state``), together with that ``store`` and
    the ``data_window``: the store's current ``ingest_state_sha256`` must equal the manifests'
    ``ingest_ledger_state`` and the given records must equal the store's A-6 records of the window (count,
    keys, content), else refused (fix round 1: the listed resolutions are bound to the ledger state)."""
    if not _COMMIT.match(str(commit)):
        raise ValueError("commit must be a 40-hex git commit")
    if not isinstance(dirty, bool):
        raise ValueError("dirty must be a bool (code_state)")
    if not runs or not all(isinstance(r, BookRun) for r in runs):
        raise ValueError("runs: the executed books this record covers are required")
    research = bool(research) or any(r.research for r in runs)
    if research and dirty:
        raise ValueError("a research run record needs a clean working tree (code_dirty)")
    if not data_manifests:
        raise ValueError("data manifest sha256s are required")
    if a6_resolutions is None and "ingest_ledger_state" in data_manifests:
        raise ValueError("a6_resolutions: a raw-store record must list the A-6 resolutions of its window "
                         "(RawStore.a6_resolutions; [] when none)")
    a6 = [dict(r) for r in (a6_resolutions or ())]
    if not all(r.get("status") == "conflict_resolved_A6" for r in a6):
        raise ValueError("a6_resolutions: only conflict_resolved_A6 ledger records")
    if "ingest_ledger_state" in data_manifests:
        from luna.v7.rawstore import RawStore  # local: rawstore imports the runner

        if not isinstance(store, RawStore) or data_window is None:
            raise ValueError("a6_resolutions: a raw-store record needs its store and data_window"
                             " (cross-check)")
        if store.ingest_state_sha256() != data_manifests["ingest_ledger_state"]:
            raise ValueError("a6_resolutions: the store's ingest ledger state differs from the record's "
                             "ingest_ledger_state")
        want = store.a6_resolutions(int(data_window[0]), int(data_window[1]))
        if canonical_json(a6) != canonical_json(want):
            raise ValueError(f"a6_resolutions: {len(a6)} record(s) given, the store holds {len(want)} in the "
                             f"window (keys {[r['keys'] for r in want]}): mismatch")
    kr_block = None
    if kraken is not None:
        from luna.v7 import kraken as kr  # local: kraken imports this module

        if not kr.verify_comparison(kraken):
            raise ValueError("kraken: not a KrakenComparison issued by kraken.comparison")
        clash = sorted(set(kraken.provenance) & set(data_manifests))  # type: ignore[attr-defined]
        if clash:
            raise ValueError(f"data manifests already name {clash}")
        data_manifests = {**data_manifests, **kraken.provenance}  # type: ignore[attr-defined]
        kr_block = kr.dt4_extra(kraken)  # type: ignore[arg-type]
    for name, sha in {**data_manifests, "uv.lock": uv_lock_sha256}.items():
        if not _SHA.match(str(sha)):
            raise ValueError(f"{name}: not a sha256")
    if not seeds or not all(isinstance(v, int) and not isinstance(v, bool) for v in seeds.values()):
        raise ValueError("seeds must be a non-empty mapping of ints")
    from luna.v7 import leakage  # local: leakage imports this module

    leak = []
    for rep in leakage_reports:
        if not leakage.verify_report(rep):
            raise ValueError("leakage_reports: a report not issued by the leakage check runners (or altered)")
        if isinstance(rep, leakage.LeakageReport):
            leak.append({"kind": "pre_opening", "sha256": rep.sha256, "passed": bool(rep.passed),
                         "ml_star": [rep.ml_star[0], int(rep.ml_star[1])]})
        else:
            leak.append({"kind": "holdout", "sha256": rep.sha256, "pre_sha256": rep.pre_sha256,
                         "findings": len(rep.findings)})
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
        "leakage_reports": leak,
        "extra": json.loads(canonical_json(dict(extra or {}))),
        "a6_resolutions": json.loads(canonical_json(a6)),
    }
    if kr_block is not None:  # S-1.6: exploratory (previously observed window)
        body["kraken_exploratory"] = json.loads(canonical_json(kr_block))
    if opening is not None:  # the holdout opening (``guard.opening_summary``): id, pin, resume attempts
        if not isinstance(opening, Mapping) or not _SHA.match(str(opening.get("opening_id", ""))):
            raise ValueError("opening: a guard.opening_summary is required")
        body["holdout_opening"] = json.loads(canonical_json(dict(opening)))
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
           "library_versions", "run_identity", "run_output_sha256", "run_record",
           "write_run_record"]
