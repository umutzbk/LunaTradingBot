"""Guard of the single V5 historical outcome run (frozen protocol rev 3: §12 outcome guard, R1, R9).

``authorize`` is the only producer of the ``OutcomeAuthorization`` that ``backtest.run_arm`` /
``run_study`` / ``parity.run_bridge`` require for the real frozen inputs. It refuses unless:

- the annotated tag ``v5-backtest-protocol-frozen`` exists and is an ancestor of HEAD;
- the working tree is clean;
- the protocol file at HEAD is the one at the tag, with the frozen sha256;
- the frozen input copy verifies against the pinned manifest sha256 (every file re-hashed, no row at
  or after the cut);
- the user's explicit approval phrase is given;
- no outcome run was ever started (an exclusive marker file is created; R9: a later corrected run is
  a separate, explicitly approved decision, never automatic).
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

import pandas as pd

from luna.carry import frozen
from luna.carry.backtest import OutcomeAuthorization, OutcomeBlocked

PROTOCOL = "docs/superpowers/specs/luna-v5-historical-backtest-protocol.md"
TAG = "v5-backtest-protocol-frozen"
PROTOCOL_SHA = "4bd90f5a169025e0483d13c636964a937d01f10fce3355ced1c81058dfcaba54"
FROZEN_MANIFEST_SHA = "eecdc6a21626ccd5e2677cce2503f304a5730845de552554281a6ea54cc53f14"
MARKER = "OUTCOME_RUN_STARTED.json"


def approval_phrase(protocol_sha: str = PROTOCOL_SHA) -> str:
    return f"I approve the single V5 historical outcome run under protocol {protocol_sha[:16]}"


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)


def preflight(repo: Path, frozen_root: Path, *, protocol_rel: str = PROTOCOL, tag: str = TAG,
              protocol_sha: str = PROTOCOL_SHA, manifest_sha: str = FROZEN_MANIFEST_SHA) -> dict:
    """Every §12 check except the approval and the single-run marker; raises ``OutcomeBlocked``."""
    repo = Path(repo)
    fails = []
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    if _git(repo, "rev-parse", "-q", "--verify", f"refs/tags/{tag}").returncode != 0:
        fails.append(f"tag {tag} missing")
    else:
        if _git(repo, "cat-file", "-t", tag).stdout.strip() != "tag":
            fails.append(f"{tag} is not an annotated tag")
        if _git(repo, "merge-base", "--is-ancestor", tag, "HEAD").returncode != 0:
            fails.append(f"{tag} is not an ancestor of HEAD")
        if _git(repo, "diff", "--quiet", tag, "HEAD", "--", protocol_rel).returncode != 0:
            fails.append("protocol differs from the tagged version")
    if _git(repo, "status", "--porcelain").stdout.strip():
        fails.append("working tree not clean")
    blob = subprocess.run(["git", "-C", str(repo), "show", f"HEAD:{protocol_rel}"], capture_output=True)
    got = hashlib.sha256(blob.stdout).hexdigest() if blob.returncode == 0 else None  # committed bytes
    if got != protocol_sha:
        fails.append(f"protocol sha256 {got} != frozen {protocol_sha}")
    try:
        frozen.verify_frozen(Path(frozen_root), manifest_sha)
    except Exception as e:  # noqa: BLE001 - any verification failure blocks the run
        fails.append(f"frozen inputs: {e}")
    if fails:
        raise OutcomeBlocked("outcome run refused: " + "; ".join(fails))
    return {"head": head, "tag": tag, "protocol_sha256": protocol_sha, "manifest_sha256": manifest_sha}


def authorize(repo: Path, frozen_root: Path, ops_dir: Path, approval: str | None, **kw) -> OutcomeAuthorization:
    """Checks, the explicit approval, then the exclusive single-run marker (created before any run)."""
    psha = kw.get("protocol_sha", PROTOCOL_SHA)
    if approval != approval_phrase(psha):
        raise OutcomeBlocked("outcome run refused: the user's explicit approval phrase was not given")
    rec = preflight(repo, frozen_root, **kw)
    ops = Path(ops_dir)
    ops.mkdir(parents=True, exist_ok=True)
    rec = {**rec, "approved": True, "started_at": pd.Timestamp.now(tz="UTC").isoformat()}
    try:
        fd = os.open(ops / MARKER, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise OutcomeBlocked(f"outcome run refused: an outcome run was already started ({ops / MARKER}); "
                             "R9: a corrected run is a separate explicit decision") from None
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(rec, f, indent=1)
    return OutcomeAuthorization(OutcomeAuthorization._secret, rec)
