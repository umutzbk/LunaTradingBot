"""Spec pinning and V6 boundaries (spec S-1.1, S-17).

V7 never hard-codes V6 boundaries: they are read at runtime from the V6 artefacts at tag
``v6-code-frozen`` and refused if a hash differs. The V7 spec is pinned by tag, commit and blob hash.
"""

from __future__ import annotations

import functools
import hashlib
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

import luna
from luna.v7.timeutil import D, to_ns

SPEC_TAG = "v7-ml-spec-frozen"
SPEC_REL = "docs/superpowers/specs/2026-10-14-luna-v7-ml-research-design.md"
FREEZE_COMMIT = "29959fc563c0fc58430744810095cefa1d63c9a8"
SPEC_BLOB_SHA256 = "ce7370fa5cd49a47ae0912a46430f2612c972108536d85f2d8306e0389db9dd4"

V6_TAG = "v6-code-frozen"
V6_CONSTANTS_REL = "docs/superpowers/specs/luna-v6-frozen-constants.json"
V6_CONSTANTS_SHA256 = "152af4cb19e688b3551feb8210aeec5c471aec78b708592fcbb6eeef08946974"
V6_RECORD_REL = "docs/results/v6-code-freeze-record.json"
V6_RECORD_SHA256 = "2f84499556480b6a715df86b2c20f66f22142fa62b931c1806b4806cc962d413"
V6_LOOK_DAYS = (90, 180, 365)


class SpecMismatch(RuntimeError):
    pass


@dataclass(frozen=True)
class V6Bounds:
    holdout_start: int
    holdout_end: int
    t_f: int
    looks: tuple[int, int, int]


@functools.cache
def _repo_of(module_file: str) -> Path:
    return Path(module_file).resolve().parents[1]


def code_repo() -> Path:
    """The code repository (the parent of the ``luna`` package), resolved once per process and package file
    (keyed by ``luna.__file__``): the guards' location seam (``guard._roots``) calls it on every check, and a
    path resolution per call dominated the label cost (red-team r2 MJ1 round 2, AMB-38 (h)). The checks
    themselves are unchanged; a test that monkeypatches ``guard._roots`` replaces the seam as before."""
    return _repo_of(luna.__file__)


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True)


def _git_show(repo: Path, rev: str, path: str) -> bytes:
    r = _git(repo, "show", f"{rev}:{path}")
    if r.returncode != 0:
        raise SpecMismatch(f"git show {rev}:{path} failed: {r.stderr.decode(errors='replace').strip()}")
    return r.stdout


def _working_copy(repo: Path) -> bytes:
    p = Path(repo) / SPEC_REL
    if not p.is_file():
        raise SpecMismatch(f"working copy of the spec missing: {p}")
    return p.read_bytes()


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def read_frozen_spec(repo: Path | None = None) -> str:
    """The frozen spec text from the tag blob, hash-checked (raises SpecMismatch otherwise)."""
    repo = Path(repo or code_repo())
    blob = _git_show(repo, SPEC_TAG, SPEC_REL)
    if _sha(blob) != SPEC_BLOB_SHA256:
        raise SpecMismatch(f"spec blob at {SPEC_TAG} has sha256 {_sha(blob)}, expected {SPEC_BLOB_SHA256}")
    return blob.decode("utf-8")


def verify_spec(repo: Path | None = None) -> None:
    """Tag blob hash, tag -> FREEZE_COMMIT, and working copy == blob after CRLF -> LF."""
    repo = Path(repo or code_repo())
    blob = _git_show(repo, SPEC_TAG, SPEC_REL)
    if _sha(blob) != SPEC_BLOB_SHA256:
        raise SpecMismatch(f"spec blob sha256 {_sha(blob)} != {SPEC_BLOB_SHA256}")
    r = _git(repo, "rev-parse", f"{SPEC_TAG}^{{commit}}")
    commit = r.stdout.decode().strip()
    if r.returncode != 0 or commit != FREEZE_COMMIT:
        raise SpecMismatch(f"tag {SPEC_TAG} resolves to {commit or '?'}, expected {FREEZE_COMMIT}")
    work = _working_copy(repo).replace(b"\r\n", b"\n")
    if work != blob:
        raise SpecMismatch("working copy of the spec differs from the frozen blob")


def _v6_json(repo: Path, rel: str, sha: str) -> dict:
    raw = _git_show(repo, V6_TAG, rel)
    if _sha(raw) != sha:
        raise SpecMismatch(f"{rel} at {V6_TAG} has sha256 {_sha(raw)}, expected {sha}")
    return json.loads(raw)


def v6_artifacts(repo: Path | None = None) -> V6Bounds:
    """V6 holdout, T_F and looks (S-1.1). Looks: day1(T_F) + day*D, V6 forward.look_date convention."""
    repo = Path(repo or code_repo())
    consts = _v6_json(repo, V6_CONSTANTS_REL, V6_CONSTANTS_SHA256)
    record = _v6_json(repo, V6_RECORD_REL, V6_RECORD_SHA256)
    t_f = to_ns(record["T_F"])
    # day1: T_F ceiled to the day (V6 forward.day1)
    day1 = t_f if t_f % D == 0 else (t_f // D + 1) * D
    a, b, c = (day1 + d * D for d in V6_LOOK_DAYS)
    h = consts["holdout"]
    return V6Bounds(to_ns(h["start"]), to_ns(h["end"]), t_f, (a, b, c))
