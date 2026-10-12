"""V6 guards (spec S-GUARD-1/2/3, S-DATA-4). A guard against accidental runs inside this codebase,
not a security boundary (as in V5).

Data windows:
- dev: every timestamp < 2026-07-01 (the sealed holdout starts there);
- holdout [2026-07-01, collector_start): only with a ``HoldoutAuthorization``, produced only by
  ``authorize_holdout`` (spec tag + code tag ancestry, code identical to the code tag, clean tree,
  spec and constants sha, the user's explicit approval phrase, two exclusive markers);
- forward: only after the code freeze (T_F in the committed code-freeze record) and after the single
  holdout opening (markers present); evidence uses recv_ts >= T_F.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

import pandas as pd

import luna
from luna.v6.spec import CONSTANTS_SHA256, SPEC_SHA256, SPEC_TAG, constants

_H = constants()["holdout"]
HOLDOUT_START = pd.Timestamp(_H["start"])
HOLDOUT_END = pd.Timestamp(_H["end"])
CODE_TAG = "v6-code-frozen"
SPEC_REL = "docs/superpowers/specs/luna-v6-profitability-spec.md"
CONSTANTS_REL = "docs/superpowers/specs/luna-v6-frozen-constants.json"
FREEZE_RECORD_REL = "docs/results/v6-code-freeze-record.json"
MARKER = "HOLDOUT_OPENED.json"


class HoldoutBlocked(RuntimeError):
    pass


class HoldoutAuthorization:
    _secret = object()

    def __init__(self, key: object, record: dict) -> None:
        if key is not HoldoutAuthorization._secret:
            raise HoldoutBlocked("a HoldoutAuthorization can only come from guard.authorize_holdout")
        self.record = record


def code_repo() -> Path:
    return Path(luna.__file__).resolve().parents[1]


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)


def main_repo(repo: Path | None = None) -> Path:
    """The main checkout (the parent of the common git dir), where var/ lives for the collector."""
    repo = repo or code_repo()
    common = _git(repo, "rev-parse", "--git-common-dir").stdout.strip()
    p = Path(common)
    if not p.is_absolute():
        p = (Path(repo) / p).resolve()
    return p.parent


def marker_paths(repo: Path | None = None) -> list[Path]:
    repo = repo or code_repo()
    return [Path(repo) / "var" / "ops" / "v6_holdout" / MARKER, main_repo(repo) / "var" / "ops" / "v6_holdout" / MARKER]


def holdout_opened(repo: Path | None = None) -> bool:
    return all(m.exists() for m in marker_paths(repo))


def forward_start(repo: Path | None = None) -> pd.Timestamp | None:
    """T_F from the committed code-freeze record (None before the code freeze)."""
    p = Path(repo or code_repo()) / FREEZE_RECORD_REL
    if not p.exists():
        return None
    return pd.Timestamp(json.loads(p.read_text(encoding="utf-8"))["T_F"])


def require_window(lo: pd.Timestamp, hi: pd.Timestamp, scope: str, auth: HoldoutAuthorization | None = None,
                   repo: Path | None = None) -> None:
    """Refuse a data window that the scope may not read. ``hi`` is the last timestamp in the data."""
    lo, hi = pd.Timestamp(lo), pd.Timestamp(hi)
    if scope == "synthetic":
        return
    if scope == "dev":
        if hi >= HOLDOUT_START:
            raise HoldoutBlocked(f"dev data reaches {hi} (the sealed holdout starts {HOLDOUT_START})")
        return
    if scope == "holdout":
        if not isinstance(auth, HoldoutAuthorization):
            raise HoldoutBlocked("the sealed holdout needs the user's explicit approval (authorize_holdout)")
        if hi >= HOLDOUT_END:
            raise HoldoutBlocked(f"holdout data reaches {hi} beyond its end {HOLDOUT_END}")
        return
    if scope == "forward":
        tf = forward_start(repo)
        if tf is None:
            raise HoldoutBlocked("forward evaluation before the code freeze (no T_F)")
        if not holdout_opened(repo):
            raise HoldoutBlocked("forward runs need holdout-window inputs: only after the single holdout opening")
        return
    raise ValueError(f"unknown scope {scope}")


def approval_phrase() -> str:
    return f"I approve the single V6 holdout opening under spec {SPEC_SHA256[:16]}"


def preflight(repo: Path | None = None) -> dict:
    repo = Path(repo or code_repo())
    fails = []
    for tag in (SPEC_TAG, CODE_TAG):
        if _git(repo, "rev-parse", "-q", "--verify", f"refs/tags/{tag}").returncode != 0:
            fails.append(f"tag {tag} missing")
            continue
        if _git(repo, "cat-file", "-t", tag).stdout.strip() != "tag":
            fails.append(f"{tag} is not an annotated tag")
        if _git(repo, "merge-base", "--is-ancestor", tag, "HEAD").returncode != 0:
            fails.append(f"{tag} is not an ancestor of HEAD")
    if not fails and _git(repo, "diff", "--quiet", CODE_TAG, "HEAD", "--", "luna", "scripts").returncode != 0:
        fails.append("code differs from the v6-code-frozen tag")
    if _git(repo, "status", "--porcelain").stdout.strip():
        fails.append("working tree not clean")
    for rel, sha in ((SPEC_REL, SPEC_SHA256), (CONSTANTS_REL, CONSTANTS_SHA256)):
        blob = subprocess.run(["git", "-C", str(repo), "show", f"HEAD:{rel}"], capture_output=True)
        got = hashlib.sha256(blob.stdout).hexdigest() if blob.returncode == 0 else None
        if got != sha:
            fails.append(f"{rel} sha256 {got} != frozen {sha}")
    if fails:
        raise HoldoutBlocked("holdout opening refused: " + "; ".join(fails))
    return {"head": _git(repo, "rev-parse", "HEAD").stdout.strip(), "spec_sha256": SPEC_SHA256,
            "constants_sha256": CONSTANTS_SHA256}


def authorize_holdout(approval: str | None, repo: Path | None = None) -> HoldoutAuthorization:
    repo = Path(repo or code_repo())
    if approval != approval_phrase():
        raise HoldoutBlocked("holdout opening refused: the user's explicit approval phrase was not given")
    markers = marker_paths(repo)
    done = [str(m) for m in markers if m.exists()]
    if done:
        raise HoldoutBlocked(f"holdout opening refused: the holdout was already opened ({done})")
    rec = {**preflight(repo), "approved": True, "opened_at": pd.Timestamp.now(tz="UTC").isoformat()}
    for m in markers:
        m.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(m, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            raise HoldoutBlocked(f"holdout opening refused: {m} appeared concurrently") from None
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(rec, f, indent=1)
    return HoldoutAuthorization(HoldoutAuthorization._secret, rec)
