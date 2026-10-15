"""V7 guards: holdout seal, V6 embargo (M6), forward seal (W_FWD7 before L7), historical-field whitelist (spec
S-1.2, S-1.3, S-1.7, DEC-19, DEC-41, DEC-46, L10, L14, K9, §15, §17; SC-05 / SC-06, ruling R45, AMB-34).

A guard against accidental or premature runs inside this codebase, not a security boundary (as in V5 / V6).

- ``W_HOLD`` outcome windows need a ``HoldoutAuthorization`` issued by ``authorize_holdout`` (or by
  ``resume_holdout`` for the same opening). ``verify_authorization`` re-derives the CANONICAL marker / pin
  paths from the guard's own ``repo`` (git common dir) and ``home`` (never from the authorization's record or
  any caller path) and requires: both markers and both pins present, valid for this spec / ``RUN_ID``, equal
  to each other, linked (``pin.marker_sha256``, the same ``opening_id``) and equal to the authorization's
  record. Verified objects are cached by identity (``WeakSet``); a fresh check (``engine.run`` start,
  ``holdout_inputs``, ``check_outcome_window(fresh=True)``) re-reads the files and evicts on failure.
  The class has no public key: key and issuer live only in the closure that defines ``authorize_holdout`` /
  ``resume_holdout`` (no module-level issuer).
- ``authorize_holdout`` (DEC-19) requires, through ``preflight``: the spec tag and the annotated
  ``v7-code-frozen`` tag, HEAD == the code tag, a clean tree, the frozen spec verified; the persisted
  pre-opening record (``luna.v7.opening``) re-verified at HEAD; the W_HOLD file manifest sha256; then the
  explicit approval phrase and no previous opening (no marker or pin present). It creates the immutable
  ``opening_id`` (sha256 of the code commit, the manifest, the pre-opening record / DevSelection /
  LeakageReport shas, ML*, the fold- / final-model hashes, the W_DEV ML* run sha and the opening timestamp)
  and writes, by exclusive create, the two pins FIRST and then the two markers (a hard kill part-way leaves a
  partial state that neither a new opening nor a resume accepts: governance).
- ``resume_holdout`` (controller ruling R45, user-approved 2026-10-15, implementation clarification): an
  idempotent continuation of the SAME opening after a crash, never a second opening. It needs the distinct
  resume phrase; the complete canonical opening state (else refused; a partial state is left to governance,
  never repaired); no produced verdict (``mark_verdict_produced``: then permanently refused); the complete
  preflight again with HEAD == ``pin.code_commit``; the pre-opening record, DevSelection, LeakageReport and
  manifest equal to the pinned ones and the recomputed ``opening_id`` equal to the pinned one. It writes no
  marker and no new ``opening_id``. EVERY attempt (resumed or refused, with the UTC time and the reason) is
  appended to ``RESUME_ATTEMPTS.jsonl`` in the canonical marker directories: a log that no guard ever reads
  as an authorization.
- A window that overlaps M6 = [V6 holdout start, R_V6) is refused even with authorisation (S-1.3, K9). M6
  comes only from ``windows.m6_cached`` (canonical, hash-checked); it is lifted only when the canonical R_V6
  exists and BOTH the caller's ``now_ns`` and the trusted clock (``release.trusted_now_ns``) have reached it.
- §15 "no outcome is computed before L7": a window overlapping W_FWD7 = [T_F7, T_F7 + 180 d) (canonical
  T_F7) is refused (``SealedBeforeL7``) until the canonical L7, on both clocks; one overlapping the extension
  [T_F7 + 180 d, T_F7 + 270 d) until the extended L7.
- Before the opening only file *listings* of ``W_HOLD`` are readable (S-1.7).
- E8: only whitelisted immutable identity fields of current ``exchangeInfo`` may be used as history.

Marker locations are injectable (``home``) and the repository is injectable (``repo``) so tests never touch
the real home or git dir.
"""

from __future__ import annotations

import functools
import hashlib
import json
import os
import re
import weakref
from collections.abc import Mapping
from pathlib import Path

import pandas as pd

from luna.v7 import release, windows
from luna.v7.constants import FROZEN
from luna.v7.forward_store import SealedBeforeL7
from luna.v7.spec import SPEC_BLOB_SHA256, SPEC_TAG, SpecMismatch, _git, code_repo, verify_spec
from luna.v7.timeutil import D

CODE_TAG = "v7-code-frozen"
MARKER = "HOLDOUT_OPENED.json"
PIN = "HOLDOUT_MANIFEST.json"
VERDICT = "HOLDOUT_VERDICT.json"  # written once, when the W_HOLD verdict is produced (no second look)
RESUME_LOG = "RESUME_ATTEMPTS.jsonl"  # append-only log of every resume attempt (never an authorization)
RUN_ID = "v7-" + SPEC_BLOB_SHA256[:16]  # canonical run identity of the single opening

# E8 (S-1.7): the only exchangeInfo fields that may establish history (immutable identity / schema, DEC-31).
IDENTITY_FIELDS = frozenset({"baseAsset", "contractType", "underlyingType", "multiplier"})
_SHA = re.compile(r"\A[0-9a-f]{64}\Z")
_PIN_SHAS = ("manifest_sha256", "pre_opening_sha256", "dev_selection_sha256", "leakage_report_sha256",
             "final_model_hash", "dev_ml_run_sha256", "marker_sha256", "opening_id")
_OPENING_ID_FIELDS = ("run_id", "spec_sha256", "code_commit", "manifest_sha256", "pre_opening_sha256",
                      "dev_selection_sha256", "leakage_report_sha256", "ml_star", "fold_model_hashes",
                      "final_model_hash", "dev_ml_run_sha256", "opened_at")


class GuardFailure(RuntimeError):
    """A guard condition failed (e.g. N6 redirect / timestamp substitution, E8 field misuse)."""


class HoldoutBlocked(RuntimeError):
    pass


class EmbargoBlocked(RuntimeError):
    """An outcome window overlaps M6 before R_V6 (S-1.3, K9)."""


# --- pure helpers -------------------------------------------------------------------------------------

_PERIOD = re.compile(r"-(\d{4})-(\d{2})(?:-(\d{2}))?\.zip(?:\.CHECKSUM)?\Z", re.ASCII)


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


def _released(end: int | None, now_ns: int) -> bool:
    """A canonical seal end is lifted only when both the caller's clock and the trusted clock reached it."""
    return end is not None and int(now_ns) >= end and release.trusted_now_ns() >= end


def _check_forward_seal(lo: int, hi: int, now_ns: int, repo: Path | None) -> None:
    tf7 = release.t_f7(repo)
    if tf7 is None:  # no canonical review PASS: no forward test has started (W_FWD7 undefined)
        return
    base = (tf7, tf7 + FROZEN.forward.days * D)
    ext = (base[1], base[1] + FROZEN.forward.extension_days * D)
    for span, extended in ((base, False), (ext, True)):
        if windows.overlaps(lo, hi, span) and not _released(release.l7(repo, extended=extended), now_ns):
            raise SealedBeforeL7(f"outcome window [{lo}, {hi}) overlaps W_FWD7 {span}: no forward outcome "
                                 f"before L7{' (extended)' if extended else ''} (§15)")


def check_outcome_window(lo: int, hi: int, *, auth: HoldoutAuthorization | None = None, now_ns: int,
                         repo: Path | None = None, home: Path | None = None,
                         m6_span: tuple[int, int | None] | None = None, fresh: bool = False) -> None:
    """Refuse an outcome window [lo, hi): M6 overlap before R_V6 (EmbargoBlocked, even with auth), W_FWD7
    overlap before L7 (SealedBeforeL7), then W_HOLD overlap without a verified authorisation (HoldoutBlocked).

    M6, R_V6, T_F7 and L7 always come from the canonical, hash-checked sources (``windows.m6_cached`` /
    ``luna.v7.release``, read once per process). A supplied ``m6_span`` is only a cross-check: anything
    differing from the canonical span raises EmbargoBlocked. ``now_ns`` can only keep a seal closed: lifting
    needs the trusted clock as well. A W_HOLD window needs an authorization verified against the canonical
    markers / pins of ``repo`` / ``home`` (cached by identity; ``fresh=True`` re-reads the files)."""
    if hi <= lo:
        raise ValueError(f"empty outcome window [{lo}, {hi})")
    span = windows.m6_cached(repo)
    if m6_span is not None and tuple(m6_span) != tuple(span):
        raise EmbargoBlocked(f"m6_span {m6_span} differs from the canonical M6 {span} (S-1.2, S-1.3)")
    if windows.overlaps(lo, hi, span) and not _released(span[1], now_ns):
        raise EmbargoBlocked(f"outcome window [{lo}, {hi}) overlaps M6 {span} before R_V6 (S-1.3)")
    _check_forward_seal(lo, hi, now_ns, repo)
    if windows.overlaps(lo, hi, windows.W_HOLD):
        if fresh or not _is_verified(auth):
            verify_authorization(auth, repo=repo, home=home)


def check_listing_only(path: str, *, access: str) -> None:
    """S-1.7: file listings (name, size, checksum) of W_HOLD / M6 are allowed; their content is refused.
    ``access`` is "listing" or "content" (required). Content access fails closed: refused when the period
    reaches W_HOLD / M6 and also when it cannot be parsed from the name."""
    if access == "listing":
        return
    if access != "content":
        raise ValueError(f"access must be 'listing' or 'content', got {access!r}")
    per = file_period(path)
    if per is None:
        raise HoldoutBlocked(f"content of {path}: period not parseable from the name, refused")
    if per[1] > windows.W_HOLD[0]:
        raise HoldoutBlocked(f"content of {path} (period reaches W_HOLD / M6) is sealed: listing only")


# --- canonical markers and pins (V6 pattern) ----------------------------------------------------------


@functools.cache
def _common_dir(repo_key: str) -> str:
    r = _git(Path(repo_key), "rev-parse", "--git-common-dir")
    out = r.stdout.decode().strip()
    if r.returncode != 0 or not out:
        raise HoldoutBlocked(f"{repo_key} is not a git checkout (the markers need the repository identity)")
    p = Path(out)
    return str(p if p.is_absolute() else (Path(repo_key) / p).resolve())


def git_common_dir(repo: Path | None = None) -> Path:
    """The repository's common git dir (memoised per resolved repository)."""
    return Path(_common_dir(release.repo_key(repo)))


def marker_paths(repo: Path | None = None, *, home: Path | None = None) -> list[Path]:
    """The two CANONICAL markers keyed by RUN_ID: the user's home (every clone) and the repository's common
    git dir (every worktree). Deleting or switching a checkout cannot re-open the holdout. The pins, the
    verdict marker and the resume log sit next to them."""
    home = Path(home) if home is not None else Path.home()
    return [home / ".luna" / "v7_holdout" / RUN_ID / MARKER,
            git_common_dir(repo) / "luna-v7" / RUN_ID / MARKER]


def _read(p: Path) -> dict | None:
    try:
        rec = json.loads(Path(p).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return rec if isinstance(rec, dict) else None


def _sha(rec: Mapping) -> str:
    return hashlib.sha256(json.dumps(dict(rec), sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _opening_id(pin: Mapping) -> str:
    return _sha({k: pin.get(k) for k in _OPENING_ID_FIELDS})


def _marker_ok(rec: dict | None) -> bool:
    return (isinstance(rec, dict) and rec.get("approved") is True
            and rec.get("spec_sha256") == SPEC_BLOB_SHA256 and rec.get("run_id") == RUN_ID)


def _pin_ok(rec: dict | None) -> bool:
    if not isinstance(rec, dict) or rec.get("run_id") != RUN_ID or rec.get("spec_sha256") != SPEC_BLOB_SHA256:
        return False
    return all(isinstance(rec.get(k), str) and _SHA.match(rec[k]) for k in _PIN_SHAS)


def _canonical_state(repo: Path | None, home: Path | None) -> tuple[dict, dict] | None:
    """(marker, pin) of the canonical opening; None when nothing of an opening exists; HoldoutBlocked on any
    partial or inconsistent state (left to governance, never repaired)."""
    markers = marker_paths(repo, home=home)
    pins = [m.parent / PIN for m in markers]
    if not any(p.exists() for p in markers + pins):
        return None
    ms, ps = [_read(m) for m in markers], [_read(p) for p in pins]
    ok = (all(_marker_ok(m) for m in ms) and all(_pin_ok(p) for p in ps) and ms[0] == ms[1] and ps[0] == ps[1]
          and ps[0]["marker_sha256"] == _sha(ms[0]) and ps[0]["opening_id"] == _opening_id(ps[0])
          and ms[0].get("opening_id") == ps[0]["opening_id"] and ms[0].get("opened_at") == ps[0]["opened_at"])
    if not ok:
        raise HoldoutBlocked("partial or inconsistent holdout opening state (markers / pins missing, invalid "
                             "or unequal): refused, left to governance (never repaired automatically)")
    return ms[0], ps[0]


def holdout_opened(repo: Path | None = None, *, home: Path | None = None) -> bool:
    """The canonical opening is complete and consistent (both markers and both pins)."""
    try:
        return _canonical_state(repo, home) is not None
    except HoldoutBlocked:
        return False


def pinned_manifest(repo: Path | None = None, *, home: Path | None = None) -> dict | None:
    """The pin of the complete, consistent canonical opening, else None."""
    try:
        st = _canonical_state(repo, home)
    except HoldoutBlocked:
        return None
    return None if st is None else dict(st[1])


def verdict_produced(repo: Path | None = None, *, home: Path | None = None) -> dict | None:
    """The verdict marker (either canonical directory), else None."""
    for m in marker_paths(repo, home=home):
        p = m.parent / VERDICT
        if p.exists():
            return _read(p) or {"unreadable": str(p)}
    return None


# --- verification ---------------------------------------------------------------------------------------

_VERIFIED: weakref.WeakSet = weakref.WeakSet()


def _is_verified(auth: object) -> bool:
    try:
        return auth in _VERIFIED
    except TypeError:
        return False


def verify_authorization(auth: object, *, repo: Path | None = None, home: Path | None = None) -> dict:
    """Full check against the CANONICAL opening of ``repo`` / ``home`` (paths re-derived here, never taken
    from the authorization): ``holdout_opened`` and ``pinned_manifest`` hold independently and equal the
    authorization's record. Returns the pin; HoldoutBlocked otherwise (and the object leaves the cache)."""
    try:
        if not isinstance(auth, HoldoutAuthorization):
            raise HoldoutBlocked("the sealed W_HOLD needs the user's explicit approval (authorize_holdout)")
        try:
            rec = auth.record
            marker, pin = rec["marker"], rec["pin"]
        except (AttributeError, KeyError, TypeError, ValueError):
            raise HoldoutBlocked("not an authorization issued by authorize_holdout") from None
        if not holdout_opened(repo, home=home):
            raise HoldoutBlocked("the holdout is not opened (canonical markers / pins missing or "
                                 "inconsistent)")
        st = _canonical_state(repo, home)
        canonical_pin = pinned_manifest(repo, home=home)
        if st is None or canonical_pin is None or canonical_pin != pin or st[0] != marker:
            raise HoldoutBlocked("the authorization does not match the canonical opening (markers / pin)")
    except HoldoutBlocked:
        try:
            _VERIFIED.discard(auth)
        except TypeError:
            pass
        raise
    _VERIFIED.add(auth)
    return dict(pin)


def approval_phrase() -> str:
    return f"I approve the single V7 holdout opening under spec {SPEC_BLOB_SHA256[:16]}"


def resume_phrase() -> str:
    return f"I resume the same V7 holdout opening under spec {SPEC_BLOB_SHA256[:16]} (no second opening)"


# --- the opening (DEC-19) -----------------------------------------------------------------------------


def preflight(repo: Path | None = None, *, pre_opening: str | Path | None = None,
              manifest_sha256: str | None = None) -> dict:
    """Every DEC-19 prerequisite except the phrase and the markers: code freeze (tags, HEAD == code tag,
    clean tree, frozen spec), the persisted pre-opening record re-verified at HEAD (``opening``), the W_HOLD
    file manifest sha256. Returns {head, spec_sha256, pre_opening}; HoldoutBlocked listing every failure."""
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
    pre = None
    from luna.v7 import opening  # local: opening imports this module

    try:
        pre = opening.load_pre_opening(pre_opening, head=head)
    except HoldoutBlocked as e:
        fails.append(str(e).removeprefix("holdout opening refused: "))
    if not isinstance(manifest_sha256, str) or not _SHA.match(manifest_sha256):
        fails.append("the W_HOLD file manifest sha256 to pin is missing or malformed (DEC-19 pinned "
                     "manifest)")
    if fails:
        raise HoldoutBlocked("holdout opening refused: " + "; ".join(fails))
    return {"head": head, "spec_sha256": SPEC_BLOB_SHA256, "pre_opening": pre}


def _create(p: Path, text: str) -> None:
    """Exclusive create of one file (never overwrites)."""
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(p, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise HoldoutBlocked(f"{p} already exists (exclusive create)") from None
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)


def _rollback(written: list[Path]) -> None:
    for p in written:
        try:
            os.remove(p)
        except OSError:
            pass


def _write_exclusive(paths_and_text: list[tuple[Path, str]]) -> None:
    """All or nothing on an exception (rollback); a hard kill can leave a prefix of the list (governance)."""
    written: list[Path] = []
    try:
        for p, text in paths_and_text:
            _create(p, text)
            written.append(p)
    except BaseException:
        _rollback(written)
        raise


def _authorize(phrase: str | None, repo: Path | None, home: Path | None, pre_opening, manifest_sha256,
               issue) -> HoldoutAuthorization:
    repo = Path(repo or code_repo())
    if phrase != approval_phrase():
        raise HoldoutBlocked("holdout opening refused: the user's explicit approval phrase was not given")
    markers = marker_paths(repo, home=home)
    if len({os.path.normcase(os.path.abspath(m)) for m in markers}) != len(markers):
        raise HoldoutBlocked("holdout opening refused: the marker paths are not distinct")
    done = [str(p) for m in markers for p in (m, m.parent / PIN, m.parent / VERDICT) if p.exists()]
    if done:
        raise HoldoutBlocked(f"holdout opening refused: the holdout was already opened ({done})")
    pf = preflight(repo, pre_opening=pre_opening, manifest_sha256=manifest_sha256)
    pre = pf["pre_opening"]
    rep = pre.leakage_report
    opened_at = pd.Timestamp.now(tz="UTC").isoformat()
    pin = {"run_id": RUN_ID, "spec_sha256": SPEC_BLOB_SHA256, "code_commit": pf["head"],
           "manifest_sha256": manifest_sha256, "pre_opening_sha256": pre.record_sha256,
           "dev_selection_sha256": pre.dev_selection_sha256, "leakage_report_sha256": rep.sha256,
           "ml_star": [rep.ml_star[0], int(rep.ml_star[1])],
           "fold_model_hashes": {str(h): list(hs) for h, hs in rep.fold_model_hashes},
           "final_model_hash": rep.final_model_hash, "dev_ml_run_sha256": pre.dev_ml_run_sha256,
           "opened_at": opened_at}
    pin["opening_id"] = _opening_id(pin)
    marker = {"head": pf["head"], "spec_sha256": SPEC_BLOB_SHA256, "run_id": RUN_ID, "approved": True,
              "opened_at": opened_at, "opening_id": pin["opening_id"]}
    pin["marker_sha256"] = _sha(marker)
    pins = [m.parent / PIN for m in markers]
    _write_exclusive([(p, json.dumps(pin, indent=1)) for p in pins]  # pins first, then markers
                     + [(m, json.dumps(marker, indent=1)) for m in markers])
    auth = issue({"marker": marker, "pin": pin, "opening_id": pin["opening_id"], "resumed": False}, rep)
    verify_authorization(auth, repo=repo, home=home)
    return auth


def _log_dirs(repo: Path, home: Path | None) -> list[Path]:
    home_dir = (Path(home) if home is not None else Path.home()) / ".luna" / "v7_holdout" / RUN_ID
    try:
        return [home_dir, git_common_dir(repo) / "luna-v7" / RUN_ID]
    except HoldoutBlocked:
        return [home_dir]


def _log_attempt(dirs: list[Path], entry: Mapping) -> None:
    """Append one line to the resume log in every canonical directory (append-only)."""
    line = json.dumps(dict(entry), sort_keys=True) + "\n"
    for d in dirs:
        d.mkdir(parents=True, exist_ok=True)
        with open(d / RESUME_LOG, "a", encoding="utf-8") as f:
            f.write(line)
            f.flush()
            os.fsync(f.fileno())


def _resume(phrase, repo: Path, home: Path | None, pre_opening, manifest_sha256, issue):
    if phrase != resume_phrase():
        raise HoldoutBlocked("resume refused: the explicit resume phrase (distinct from the opening phrase) "
                             "was not given")
    st = _canonical_state(repo, home)  # partial / inconsistent -> governance
    if st is None:
        raise HoldoutBlocked("resume refused: the holdout was not opened (nothing to resume)")
    marker, pin = st
    v = verdict_produced(repo, home=home)
    if v is not None:
        raise HoldoutBlocked("resume refused permanently: the W_HOLD verdict was already produced (no second "
                             "look)")
    pf = preflight(repo, pre_opening=pre_opening, manifest_sha256=manifest_sha256)
    pre = pf["pre_opening"]
    rep = pre.leakage_report
    diffs = [name for name, now, pinned in (
        ("code commit", pf["head"], pin["code_commit"]), ("marker head", pf["head"], marker["head"]),
        ("manifest", manifest_sha256, pin["manifest_sha256"]),
        ("pre-opening record", pre.record_sha256, pin["pre_opening_sha256"]),
        ("DevSelection", pre.dev_selection_sha256, pin["dev_selection_sha256"]),
        ("LeakageReport", rep.sha256, pin["leakage_report_sha256"]),
        ("final model", rep.final_model_hash, pin["final_model_hash"]),
        ("W_DEV ML* run", pre.dev_ml_run_sha256, pin["dev_ml_run_sha256"])) if now != pinned]
    if diffs:
        raise HoldoutBlocked(f"resume refused: differs from the pinned opening ({', '.join(diffs)}); the "
                             "holdout stays sealed")
    auth = issue({"marker": marker, "pin": pin, "opening_id": pin["opening_id"], "resumed": True}, rep)
    verify_authorization(auth, repo=repo, home=home)
    return auth, pf["head"], pin["opening_id"]


def _resume_logged(phrase, repo: Path | None, home: Path | None, pre_opening, manifest_sha256, issue):
    repo = Path(repo or code_repo())
    dirs = _log_dirs(repo, home)
    base = {"utc": pd.Timestamp.now(tz="UTC").isoformat(), "pid": os.getpid(), "run_id": RUN_ID}
    try:
        auth, head, oid = _resume(phrase, repo, home, pre_opening, manifest_sha256, issue)
    except HoldoutBlocked as e:
        _log_attempt(dirs, {**base, "result": "refused", "reason": str(e)})
        raise
    try:
        _log_attempt(dirs, {**base, "result": "resumed", "reason": "same opening, unchanged pin",
                            "head": head, "opening_id": oid})
    except OSError as e:  # an unlogged resume is not allowed
        raise HoldoutBlocked(f"resume refused: the attempt could not be logged ({e})") from None
    return auth


def resume_records(repo: Path | None = None, *, home: Path | None = None) -> list[dict]:
    """Every logged resume attempt (canonical home directory log), in order."""
    p = (Path(home) if home is not None else Path.home()) / ".luna" / "v7_holdout" / RUN_ID / RESUME_LOG
    if not p.exists():
        return []
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]


def mark_verdict_produced(auth: object, verdict_sha256: str, *, repo: Path | None = None,
                          home: Path | None = None) -> dict:
    """Record, once and by exclusive create in both canonical directories, that the W_HOLD verdict was
    produced (after which any resume is permanently refused). HoldoutBlocked if one already exists."""
    pin = verify_authorization(auth, repo=repo, home=home)
    if not isinstance(verdict_sha256, str) or not _SHA.match(verdict_sha256):
        raise ValueError("verdict_sha256 must be a sha256")
    if verdict_produced(repo, home=home) is not None:
        raise HoldoutBlocked("the W_HOLD verdict was already produced (no second look)")
    rec = {"run_id": RUN_ID, "opening_id": pin["opening_id"], "verdict_sha256": verdict_sha256,
           "utc": pd.Timestamp.now(tz="UTC").isoformat()}
    try:
        _write_exclusive([(m.parent / VERDICT, json.dumps(rec, indent=1))
                          for m in marker_paths(repo, home=home)])
    except HoldoutBlocked:
        raise HoldoutBlocked("the W_HOLD verdict was already produced (no second look)") from None
    return rec


def opening_summary(auth: object, *, repo: Path | None = None, home: Path | None = None) -> dict:
    """For the DT4 run record: the opening id, the pin, the marker sha, every logged resume attempt and the
    verdict marker (verified against the canonical opening)."""
    pin = verify_authorization(auth, repo=repo, home=home)
    return {"opening_id": pin["opening_id"], "pin": pin, "marker_sha256": pin["marker_sha256"],
            "resumes": resume_records(repo, home=home), "verdict_produced": verdict_produced(repo, home=home)}


def _opening_api():
    """The authorization class and the two entry points that can issue it; the key and the issuer exist only
    in this closure (no class attribute, no module-level issuer)."""
    key = object()

    class HoldoutAuthorization:
        """Issued only by ``authorize_holdout`` / ``resume_holdout``; ``record`` = the opening's canonical
        marker and pin and its ``opening_id``; ``leakage_report`` = the pinned LeakageReport reloaded in this
        process. Worth nothing by itself: every use is verified against the canonical files."""

        __slots__ = ("_record", "_report", "__weakref__")

        def __init__(self, k: object, record: Mapping, report: object = None) -> None:
            if k is not key:
                raise HoldoutBlocked("a HoldoutAuthorization can only come from guard.authorize_holdout")
            self._record = json.dumps(dict(record), sort_keys=True)
            self._report = report

        @property
        def record(self) -> dict:
            return json.loads(self._record)

        @property
        def opening_id(self) -> str:
            return self.record["opening_id"]

        @property
        def leakage_report(self):
            return self._report

    def issue(record: Mapping, report: object) -> HoldoutAuthorization:
        return HoldoutAuthorization(key, record, report)

    def authorize_holdout(approval_phrase_given: str | None, repo: Path | None = None, *,
                          home: Path | None = None, pre_opening: str | Path | None = None,
                          manifest_sha256: str | None = None) -> HoldoutAuthorization:
        """The single DEC-19 opening (module docstring). ``pre_opening``: the record written by
        ``opening.write_pre_opening_record`` in the producing run; ``manifest_sha256``: the W_HOLD file
        manifest (listings / checksums) to pin."""
        return _authorize(approval_phrase_given, repo, home, pre_opening, manifest_sha256, issue)

    def resume_holdout(resume_phrase_given: str | None, repo: Path | None = None, *,
                       home: Path | None = None, pre_opening: str | Path | None = None,
                       manifest_sha256: str | None = None) -> HoldoutAuthorization:
        """Resume the SAME opening after a crash (R45; module docstring). Every attempt is logged."""
        return _resume_logged(resume_phrase_given, repo, home, pre_opening, manifest_sha256, issue)

    return HoldoutAuthorization, authorize_holdout, resume_holdout


HoldoutAuthorization, authorize_holdout, resume_holdout = _opening_api()
del _opening_api
