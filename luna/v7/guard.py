"""V7 guards: holdout seal, V6 embargo (M6), historical-field whitelist (spec S-1.2, S-1.3, S-1.7, DEC-19,
DEC-41, DEC-46, L10, K9, section 17).

A guard against accidental runs inside this codebase, not a security boundary (as in V5 / V6).

- ``W_HOLD`` outcome windows need a ``HoldoutAuthorization``, produced only by ``authorize_holdout``
  (spec tag + code tag ``v7-code-frozen`` present, HEAD == code tag, clean tree, frozen spec verified, the
  explicit approval phrase, two exclusive markers keyed by ``RUN_ID``, pinned manifest).
- A window that overlaps M6 = [V6 holdout start, R_V6) is refused even with authorisation (S-1.3, K9).
  M6 stays closed while R_V6 is undetermined (``windows.m6`` end is None).
- Before the opening only file *listings* of ``W_HOLD`` are readable (S-1.7).
- E8: only whitelisted immutable identity fields of current ``exchangeInfo`` may be used as history.

Marker locations are injectable (``home``) so tests never touch the real home or git dir.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pandas as pd

from luna.v7 import windows
from luna.v7.spec import SPEC_BLOB_SHA256, SPEC_TAG, SpecMismatch, _git, code_repo, verify_spec

CODE_TAG = "v7-code-frozen"
MARKER = "HOLDOUT_OPENED.json"
PIN = "HOLDOUT_MANIFEST.json"
RUN_ID = "v7-" + SPEC_BLOB_SHA256[:16]  # canonical run identity of the single opening

# E8 (S-1.7): the only exchangeInfo fields that may establish history (immutable identity / schema, DEC-31).
IDENTITY_FIELDS = frozenset({"baseAsset", "contractType", "underlyingType", "multiplier"})


class GuardFailure(RuntimeError):
    """A guard condition failed (e.g. N6 redirect / timestamp substitution, E8 field misuse)."""


class HoldoutBlocked(RuntimeError):
    pass


class EmbargoBlocked(RuntimeError):
    """An outcome window overlaps M6 before R_V6 (S-1.3, K9)."""


class HoldoutAuthorization:
    _secret = object()

    def __init__(self, key: object, record: dict) -> None:
        if key is not HoldoutAuthorization._secret:
            raise HoldoutBlocked("a HoldoutAuthorization can only come from guard.authorize_holdout")
        self.record = record


# --- pure helpers -------------------------------------------------------------------------------------

_PERIOD = re.compile(r"-(\d{4})-(\d{2})(?:-(\d{2}))?\.zip(?:\.CHECKSUM)?$")


def file_period(name: str) -> tuple[int, int] | None:
    """Half-open ns period of a data.binance.vision file / key from its name (monthly or daily), else None."""
    m = _PERIOD.search(name)
    if not m:
        return None
    y, mo, d = int(m.group(1)), int(m.group(2)), m.group(3)
    try:
        if d is None:
            lo = pd.Timestamp(year=y, month=mo, day=1, tz="UTC")
            hi = lo + pd.DateOffset(months=1)
        else:
            lo = pd.Timestamp(year=y, month=mo, day=int(d), tz="UTC")
            hi = lo + pd.Timedelta(days=1)
    except ValueError:
        return None
    return int(lo.value), int(hi.value)


def historical_field(name: str) -> str:
    """E8: current exchangeInfo may serve history only for whitelisted identity fields."""
    if name not in IDENTITY_FIELDS:
        raise GuardFailure(f"exchangeInfo field {name!r} is time-varying: never backdated as history (E8)")
    return name


def check_outcome_window(lo: int, hi: int, *, auth: HoldoutAuthorization | None = None, now_ns: int,
                         repo: Path | None = None, m6_span: tuple[int, int | None] | None = None) -> None:
    """Refuse an outcome window [lo, hi): M6 overlap before R_V6 (EmbargoBlocked, even with auth), then
    W_HOLD overlap without authorisation (HoldoutBlocked). ``now_ns`` lifts M6 only once R_V6 is known."""
    if hi <= lo:
        raise ValueError(f"empty outcome window [{lo}, {hi})")
    span = m6_span if m6_span is not None else windows.m6(repo)
    released = span[1] is not None and now_ns >= span[1]
    if not released and windows.overlaps(lo, hi, span):
        raise EmbargoBlocked(f"outcome window [{lo}, {hi}) overlaps M6 {span} before R_V6 (S-1.3)")
    if windows.overlaps(lo, hi, windows.W_HOLD):
        if not isinstance(auth, HoldoutAuthorization):
            raise HoldoutBlocked("the sealed W_HOLD needs the user's explicit approval (authorize_holdout)")


def check_listing_only(path: str, *, read_content: bool = False) -> None:
    """S-1.7: file listings (name, size, checksum) of W_HOLD / M6 are allowed; their content is refused."""
    per = file_period(path)
    if per is None or not read_content:
        return
    if per[1] > windows.W_HOLD[0]:
        raise HoldoutBlocked(f"content of {path} (period reaches W_HOLD / M6) is sealed: listing only")


# --- authorisation (V6 pattern) -----------------------------------------------------------------------


def git_common_dir(repo: Path | None = None) -> Path:
    repo = Path(repo or code_repo())
    r = _git(repo, "rev-parse", "--git-common-dir")
    out = r.stdout.decode().strip()
    if r.returncode != 0 or not out:
        raise HoldoutBlocked(f"{repo} is not a git checkout (the markers need the repository identity)")
    p = Path(out)
    return p if p.is_absolute() else (repo / p).resolve()


def marker_paths(repo: Path | None = None, *, home: Path | None = None) -> list[Path]:
    """Two markers keyed by RUN_ID: the user's home (every clone) and the repository's common git dir
    (every worktree). Deleting or switching a checkout cannot re-open the holdout."""
    home = Path(home) if home is not None else Path.home()
    return [home / ".luna" / "v7_holdout" / RUN_ID / MARKER,
            git_common_dir(repo) / "luna-v7" / RUN_ID / MARKER]


def _marker_ok(m: Path) -> bool:
    try:
        rec = json.loads(m.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    ok = isinstance(rec, dict) and rec.get("approved") is True
    return ok and rec.get("spec_sha256") == SPEC_BLOB_SHA256


def holdout_opened(repo: Path | None = None, *, home: Path | None = None) -> bool:
    return all(m.exists() and _marker_ok(m) for m in marker_paths(repo, home=home))


def pin_manifest(manifest_sha256: str, auth: HoldoutAuthorization, repo: Path | None = None, *,
                 home: Path | None = None) -> None:
    """Pins the verified holdout download next to both markers, exclusively and once (all or nothing)."""
    if not isinstance(auth, HoldoutAuthorization):
        raise HoldoutBlocked("pinning the holdout manifest needs the opening's HoldoutAuthorization")
    rec = json.dumps({"run_id": RUN_ID, "spec_sha256": SPEC_BLOB_SHA256, "manifest_sha256": manifest_sha256},
                     indent=1)
    written: list[Path] = []
    try:
        for m in marker_paths(repo, home=home):
            fd = os.open(m.parent / PIN, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            written.append(m.parent / PIN)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(rec)
    except BaseException:
        for w in written:
            try:
                os.remove(w)
            except OSError:
                pass
        raise


def pinned_manifest(repo: Path | None = None, *, home: Path | None = None) -> str | None:
    """The pinned manifest sha256 when both pins exist and agree (and the holdout is opened), else None."""
    if not holdout_opened(repo, home=home):
        return None
    shas = set()
    for m in marker_paths(repo, home=home):
        try:
            rec = json.loads((m.parent / PIN).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if rec.get("spec_sha256") != SPEC_BLOB_SHA256 or rec.get("run_id") != RUN_ID:
            return None
        shas.add(rec.get("manifest_sha256"))
    return shas.pop() if len(shas) == 1 and None not in shas else None


def approval_phrase() -> str:
    return f"I approve the single V7 holdout opening under spec {SPEC_BLOB_SHA256[:16]}"


def preflight(repo: Path | None = None) -> dict:
    repo = Path(repo or code_repo())
    fails: list[str] = []
    missing = False
    for tag in (SPEC_TAG, CODE_TAG):
        if _git(repo, "rev-parse", "-q", "--verify", f"refs/tags/{tag}").returncode != 0:
            fails.append(f"tag {tag} missing")
            missing = True
            continue
        if _git(repo, "cat-file", "-t", tag).stdout.decode().strip() != "tag":
            fails.append(f"{tag} is not an annotated tag")
        if _git(repo, "merge-base", "--is-ancestor", tag, "HEAD").returncode != 0:
            fails.append(f"{tag} is not an ancestor of HEAD")
    head = _git(repo, "rev-parse", "HEAD").stdout.decode().strip()
    if not missing and _git(repo, "rev-parse", f"{CODE_TAG}^{{commit}}").stdout.decode().strip() != head:
        fails.append(f"HEAD is not the {CODE_TAG} commit")
    if _git(repo, "status", "--porcelain").stdout.decode().strip():
        fails.append("working tree not clean")
    try:
        verify_spec(repo)
    except SpecMismatch as e:
        fails.append(f"spec verification failed: {e}")
    if fails:
        raise HoldoutBlocked("holdout opening refused: " + "; ".join(fails))
    return {"head": head, "spec_sha256": SPEC_BLOB_SHA256}


def authorize_holdout(approval_phrase_given: str | None, repo: Path | None = None, *,
                      home: Path | None = None) -> HoldoutAuthorization:
    repo = Path(repo or code_repo())
    if approval_phrase_given != approval_phrase():
        raise HoldoutBlocked("holdout opening refused: the user's explicit approval phrase was not given")
    markers = marker_paths(repo, home=home)
    if len({os.path.normcase(os.path.abspath(m)) for m in markers}) != len(markers):
        raise HoldoutBlocked("holdout opening refused: the marker paths are not distinct")
    done = [str(m) for m in markers if m.exists()]
    if done:
        raise HoldoutBlocked(f"holdout opening refused: the holdout was already opened ({done})")
    rec = {**preflight(repo), "run_id": RUN_ID, "approved": True,
           "opened_at": pd.Timestamp.now(tz="UTC").isoformat()}
    written: list[Path] = []
    try:
        for m in markers:
            m.parent.mkdir(parents=True, exist_ok=True)
            try:
                fd = os.open(m, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                raise HoldoutBlocked(f"holdout opening refused: {m} appeared concurrently") from None
            written.append(m)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(rec, f, indent=1)
    except BaseException:
        for m in written:
            try:
                os.remove(m)
            except OSError:
                pass
        raise
    return HoldoutAuthorization(HoldoutAuthorization._secret, rec)

