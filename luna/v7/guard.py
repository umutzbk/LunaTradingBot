"""V7 guards: holdout seal, V6 embargo (M6), forward seal (W_FWD7 before L7), historical-field whitelist (spec
S-1.2, S-1.3, S-1.7, DEC-19, DEC-41, DEC-46, L10, L14, K9, §15, §17; SC-05 / SC-06, AMB-34).

A guard against accidental or premature runs inside this codebase, not a security boundary (as in V5 / V6).

- ``W_HOLD`` outcome windows need a ``HoldoutAuthorization``, produced only by ``authorize_holdout`` and
  re-checked at EVERY window (``verify_authorization``): both markers present, approved, keyed by ``RUN_ID``
  and equal to the authorization's; both pins present and equal to the authorization's pinned manifest. The
  class cannot be instantiated outside this module (the key lives in a closure, red-team M4 / m4); an object
  built around the constructor carries no record and is refused, as is a real one whose markers or pin are
  gone or altered.
- ``authorize_holdout`` (DEC-19) requires, through ``preflight``: the spec tag and the annotated
  ``v7-code-frozen`` tag, HEAD == the code tag, a clean tree, the frozen spec verified; the persisted
  pre-opening record (``luna.v7.opening``: a hash-verified DevSelection with a valid S-10.6 choice and an
  empty S-10.7 kill, and its passed LeakageReport, linked by ML*, fold-model and panel hashes, produced at
  HEAD); the W_HOLD file manifest sha256; then the explicit approval phrase and no previous opening. It
  writes two exclusive markers and two pins (all or nothing). The pin records the code commit, the manifest,
  the pre-opening record, DevSelection and LeakageReport sha256s, ML*, the fold-model and final-model hashes,
  the W_DEV ML* run's output sha256 and the marker's sha256.
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

import hashlib
import json
import os
import re
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
RUN_ID = "v7-" + SPEC_BLOB_SHA256[:16]  # canonical run identity of the single opening

# E8 (S-1.7): the only exchangeInfo fields that may establish history (immutable identity / schema, DEC-31).
IDENTITY_FIELDS = frozenset({"baseAsset", "contractType", "underlyingType", "multiplier"})
_SHA = re.compile(r"\A[0-9a-f]{64}\Z")


class GuardFailure(RuntimeError):
    """A guard condition failed (e.g. N6 redirect / timestamp substitution, E8 field misuse)."""


class HoldoutBlocked(RuntimeError):
    pass


class EmbargoBlocked(RuntimeError):
    """An outcome window overlaps M6 before R_V6 (S-1.3, K9)."""


def _authorization_type():
    """The authorization class and its issuer; the key exists only in this closure (no class attribute)."""
    key = object()

    class HoldoutAuthorization:
        """Produced only by ``authorize_holdout``; ``record`` is a copy of the opening's markers / pin (paths
        and contents), ``leakage_report`` the pinned pre-opening LeakageReport reloaded in this process."""

        __slots__ = ("_record", "_report")

        def __init__(self, k: object, record: Mapping, report: object = None) -> None:
            if k is not key:
                raise HoldoutBlocked("a HoldoutAuthorization can only come from guard.authorize_holdout")
            self._record = json.dumps(dict(record), sort_keys=True)
            self._report = report

        @property
        def record(self) -> dict:
            return json.loads(self._record)

        @property
        def leakage_report(self):
            return self._report

    def issue(record: Mapping, report: object) -> HoldoutAuthorization:
        return HoldoutAuthorization(key, record, report)

    return HoldoutAuthorization, issue


HoldoutAuthorization, _issue_authorization = _authorization_type()
del _authorization_type


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
                         repo: Path | None = None, m6_span: tuple[int, int | None] | None = None) -> None:
    """Refuse an outcome window [lo, hi): M6 overlap before R_V6 (EmbargoBlocked, even with auth), W_FWD7
    overlap before L7 (SealedBeforeL7), then W_HOLD overlap without a verified authorisation (HoldoutBlocked).

    M6, R_V6, T_F7 and L7 always come from the canonical, hash-checked sources (``windows.m6_cached`` /
    ``luna.v7.release``, read once per process). A supplied ``m6_span`` is only a cross-check: anything
    differing from the canonical span raises EmbargoBlocked. ``now_ns`` can only keep a seal closed: lifting
    needs the trusted clock as well."""
    if hi <= lo:
        raise ValueError(f"empty outcome window [{lo}, {hi})")
    span = windows.m6_cached(repo)
    if m6_span is not None and tuple(m6_span) != tuple(span):
        raise EmbargoBlocked(f"m6_span {m6_span} differs from the canonical M6 {span} (S-1.2, S-1.3)")
    if windows.overlaps(lo, hi, span) and not _released(span[1], now_ns):
        raise EmbargoBlocked(f"outcome window [{lo}, {hi}) overlaps M6 {span} before R_V6 (S-1.3)")
    _check_forward_seal(lo, hi, now_ns, repo)
    if windows.overlaps(lo, hi, windows.W_HOLD):
        verify_authorization(auth)


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


# --- markers and pin (V6 pattern) ---------------------------------------------------------------------


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
    (every worktree). Deleting or switching a checkout cannot re-open the holdout. The pins sit next to
    them (``PIN``)."""
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


def _marker_ok(rec: dict | None) -> bool:
    return (isinstance(rec, dict) and rec.get("approved") is True
            and rec.get("spec_sha256") == SPEC_BLOB_SHA256 and rec.get("run_id") == RUN_ID)


def _pin_ok(rec: dict | None) -> bool:
    if not isinstance(rec, dict) or rec.get("run_id") != RUN_ID or rec.get("spec_sha256") != SPEC_BLOB_SHA256:
        return False
    shas = ("manifest_sha256", "pre_opening_sha256", "dev_selection_sha256", "leakage_report_sha256",
            "final_model_hash", "dev_ml_run_sha256", "marker_sha256")
    return all(isinstance(rec.get(k), str) and _SHA.match(rec[k]) for k in shas)


def holdout_opened(repo: Path | None = None, *, home: Path | None = None) -> bool:
    """Both markers present, approved, for this spec and ``RUN_ID``."""
    return all(_marker_ok(_read(m)) for m in marker_paths(repo, home=home))


def pinned_manifest(repo: Path | None = None, *, home: Path | None = None) -> dict | None:
    """The pinned opening manifest when the holdout is opened and both pins exist, agree, belong to this run
    and to the marker, else None."""
    markers = marker_paths(repo, home=home)
    recs = [_read(m) for m in markers]
    if not all(_marker_ok(r) for r in recs) or recs[0] != recs[1]:
        return None
    pins = [_read(m.parent / PIN) for m in markers]
    if not all(_pin_ok(p) for p in pins) or pins[0] != pins[1] or pins[0]["marker_sha256"] != _sha(recs[0]):
        return None
    return pins[0]


def verify_authorization(auth: object) -> dict:
    """Re-check an authorization against the files of the opening (every call): the object comes from
    ``authorize_holdout``; both markers and both pins exist, are valid for this run and equal the ones the
    authorization recorded. Returns the pin record; HoldoutBlocked otherwise."""
    if not isinstance(auth, HoldoutAuthorization):
        raise HoldoutBlocked("the sealed W_HOLD needs the user's explicit approval (authorize_holdout)")
    try:
        rec = auth.record
        markers, pins = [Path(p) for p in rec["markers"]], [Path(p) for p in rec["pins"]]
        marker, pin = rec["marker"], rec["pin"]
    except (AttributeError, KeyError, TypeError, ValueError):
        raise HoldoutBlocked("not an authorization issued by authorize_holdout") from None
    if len(markers) != 2 or len(pins) != 2 or not _marker_ok(marker) or not _pin_ok(pin):
        raise HoldoutBlocked("malformed authorization record")
    if pin["marker_sha256"] != _sha(marker):
        raise HoldoutBlocked("the pinned manifest does not belong to this opening's marker")
    for m in markers:
        if _read(m) != marker:
            raise HoldoutBlocked(f"holdout marker {m} missing or altered: the holdout is not opened")
    for p in pins:
        if _read(p) != pin:
            raise HoldoutBlocked(f"pinned manifest {p} missing or altered (DEC-19)")
    return dict(pin)


def approval_phrase() -> str:
    return f"I approve the single V7 holdout opening under spec {SPEC_BLOB_SHA256[:16]}"


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


def _write_exclusive(paths_and_text: list[tuple[Path, str]]) -> None:
    written: list[Path] = []
    try:
        for p, text in paths_and_text:
            p.parent.mkdir(parents=True, exist_ok=True)
            try:
                fd = os.open(p, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                raise HoldoutBlocked(f"holdout opening refused: {p} appeared concurrently") from None
            written.append(p)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
    except BaseException:
        for p in written:
            try:
                os.remove(p)
            except OSError:
                pass
        raise


def authorize_holdout(approval_phrase_given: str | None, repo: Path | None = None, *,
                      home: Path | None = None, pre_opening: str | Path | None = None,
                      manifest_sha256: str | None = None) -> HoldoutAuthorization:
    """The single DEC-19 opening (module docstring). ``pre_opening``: the record written by
    ``opening.write_pre_opening_record`` in the producing run; ``manifest_sha256``: the W_HOLD file manifest
    (listings / checksums) to pin."""
    repo = Path(repo or code_repo())
    if approval_phrase_given != approval_phrase():
        raise HoldoutBlocked("holdout opening refused: the user's explicit approval phrase was not given")
    markers = marker_paths(repo, home=home)
    if len({os.path.normcase(os.path.abspath(m)) for m in markers}) != len(markers):
        raise HoldoutBlocked("holdout opening refused: the marker paths are not distinct")
    done = [str(m) for m in markers if m.exists() or (m.parent / PIN).exists()]
    if done:
        raise HoldoutBlocked(f"holdout opening refused: the holdout was already opened ({done})")
    pf = preflight(repo, pre_opening=pre_opening, manifest_sha256=manifest_sha256)
    pre = pf["pre_opening"]
    rep = pre.leakage_report
    marker = {"head": pf["head"], "spec_sha256": SPEC_BLOB_SHA256, "run_id": RUN_ID, "approved": True,
              "opened_at": pd.Timestamp.now(tz="UTC").isoformat()}
    pin = {"run_id": RUN_ID, "spec_sha256": SPEC_BLOB_SHA256, "code_commit": pf["head"],
           "manifest_sha256": manifest_sha256, "pre_opening_sha256": pre.record_sha256,
           "dev_selection_sha256": pre.dev_selection_sha256, "leakage_report_sha256": rep.sha256,
           "ml_star": [rep.ml_star[0], int(rep.ml_star[1])],
           "fold_model_hashes": {str(h): list(hs) for h, hs in rep.fold_model_hashes},
           "final_model_hash": rep.final_model_hash, "dev_ml_run_sha256": pre.dev_ml_run_sha256,
           "marker_sha256": _sha(marker)}
    pins = [m.parent / PIN for m in markers]
    _write_exclusive([(m, json.dumps(marker, indent=1)) for m in markers]
                     + [(p, json.dumps(pin, indent=1)) for p in pins])
    rec = {"repo": str(repo.resolve()), "home": str(markers[0].parents[3]),
           "markers": [str(m) for m in markers], "pins": [str(p) for p in pins], "marker": marker, "pin": pin}
    return _issue_authorization(rec, rep)
