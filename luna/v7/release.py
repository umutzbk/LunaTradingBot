"""The canonical R_V6 release and T_F7 review-pass records, and the trusted clock of the seal guards (spec
S-1.2, S-1.3, S-1.7 M3-3, DEC-1, DEC-45, §15 L7 / T_F7, §17, L10; SC-06, red-team M5; AMB-34).

ONE source, shared by ``windows.m6`` / ``m6_cached`` (M6 end), ``forward.l7`` / ``require_l7`` (L7), the
forward store (``ForwardStore.read`` / ``iter_records``), ``netguard.partition_m6_keys`` (through
``m6_cached``) and the outcome guard (``guard.check_outcome_window``, hence the engine's W_FWD7 replay
refusal). No caller value can set R_V6, T_F7 or L7.

* **R_V6 (S-1.2)** is defined by events, never by a caller value: a committed JSON release record
  (``RV6_RECORD_REL``) read through git from the annotated tag ``RV6_TAG`` (the V6 artefact pattern of
  ``spec.v6_artifacts``). It names the basis -- (a) V6's day-365 look completed and reported, (b) a V6 look
  after which every slot is CLOSED, (c) the user's written release -- the release time ``r_v6`` (UTC), the
  evidence document (the V6 look report, or the written release) and the V6-holdout document ("opened and
  reported" or "released in writing", S-1.2 last paragraph), each by repository path and sha256. Checked:
  the tag is annotated, the record's own ``record_sha256`` (canonical JSON of the other fields), both
  documents' sha256 at the same tag, the exact field set, the basis / status values, and ``r_v6`` not before
  the event it claims (basis (a): >= the day-365 look date; (b): >= the day-90 look date; (c): >= the V6
  holdout start), with the V6 dates from the hash-checked V6 artefacts.
* **T_F7 (§15)** = the first 00:00 UTC >= 24 h after the independent review PASS: a committed review-pass
  record (``TF7_RECORD_REL`` at the annotated tag ``TF7_TAG``) with the PASS time and the review document's
  sha256, checked the same way.
* **L7** = max(T_F7 + 180 d, R_V6) + 1 d (extended: T_F7 + 270 d); None while either record is absent.
* A record that does not verify is treated as ABSENT (sealed); nothing is raised and nothing is guessed.
* Records are read once per process and resolved repository (like ``windows.m6_cached``); ``cache_clear``
  drops the cache. The repository is injectable (``repo``) exactly like the existing guard / V6 artefacts.
* **Trusted clock:** ``trusted_now_ns()`` reads the wall clock inside the guard (``time.time_ns``). It is the
  only clock that can open the forward store or lift M6 / the W_FWD7 seal; a caller ``now_ns`` can only keep
  them closed. Tests pin it by monkeypatching this function, never through an API argument.

A guard against accidental or premature runs inside this codebase, not a security boundary (as in V5 / V6).
"""

from __future__ import annotations

import functools
import hashlib
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path

from luna.v7.constants import FROZEN
from luna.v7.spec import SpecMismatch, _git, _git_show, code_repo, v6_artifacts
from luna.v7.timeutil import D, H, to_ns

RV6_TAG = "v7-r-v6-release"
RV6_RECORD_REL = "docs/results/v7-impl/R_V6-release-record.json"
RV6_KIND = "V7_R_V6_RELEASE"
TF7_TAG = "v7-forward-review-pass"
TF7_RECORD_REL = "docs/results/v7-impl/forward-review-pass-record.json"
TF7_KIND = "V7_FORWARD_REVIEW_PASS"
BASES = ("a", "b", "c")  # S-1.2 (a) day-365 look, (b) all slots CLOSED after a look, (c) written release
HOLDOUT_STATUS = ("opened_and_reported", "released_in_writing")  # S-1.2: "opened and reported, or released"

FW = FROZEN.forward
_SHA = re.compile(r"\A[0-9a-f]{64}\Z")
_ISO = re.compile(r"\A\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?Z\Z")


class _Invalid(ValueError):
    pass


@dataclass(frozen=True)
class RV6Release:
    r_v6: int
    basis: str
    evidence_sha256: str
    v6_holdout_status: str
    record_sha256: str


@dataclass(frozen=True)
class ReviewPass:
    review_pass: int
    t_f7: int
    evidence_sha256: str
    record_sha256: str


def trusted_now_ns() -> int:
    """The guards' wall clock (int ns UTC). Never an argument of any guarded API."""
    return time.time_ns()


def record_sha256(record: dict) -> str:
    """sha256 of the canonical JSON (sorted keys, compact) of every field except ``record_sha256``."""
    body = {k: v for k, v in record.items() if k != "record_sha256"}
    text = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def tf7_from_review(review_pass_ns: int) -> int:
    """§15: the first 00:00 UTC >= 24 h after the independent review PASS."""
    x = to_ns(review_pass_ns) + FW.tf7_delay_h * H
    return -(-x // D) * D


def _l7(t_f7: int, r_v6: int, extended: bool) -> int:
    end = int(t_f7) + (FW.days + (FW.extension_days if extended else 0)) * D
    return max(end, int(r_v6)) + FW.l7_pad_days * D


# --- reading ---------------------------------------------------------------------------------------------


def _ts(v: object, what: str) -> int:
    if not isinstance(v, str) or not _ISO.match(v):
        raise _Invalid(f"{what} must be an ISO-8601 UTC timestamp ending in Z")
    return to_ns(v)


def _doc(repo: Path, tag: str, ref: object, what: str) -> str:
    if not isinstance(ref, dict) or set(ref) != {"path", "sha256"} or not isinstance(ref["path"], str):
        raise _Invalid(f"{what}: needs exactly path and sha256")
    sha = ref["sha256"]
    if not isinstance(sha, str) or not _SHA.match(sha):
        raise _Invalid(f"{what}: not a sha256")
    if hashlib.sha256(_git_show(repo, tag, ref["path"])).hexdigest() != sha:
        raise _Invalid(f"{what}: {ref['path']} at {tag} does not hash to {sha}")
    return sha


def _record(repo: Path, tag: str, rel: str, kind: str, keys: set[str]) -> dict:
    if _git(repo, "cat-file", "-t", tag).stdout.decode().strip() != "tag":
        raise _Invalid(f"{tag} missing or not an annotated tag")
    try:
        rec = json.loads(_git_show(repo, tag, rel))
    except ValueError as e:
        raise _Invalid(f"{rel} at {tag} is not JSON: {e}") from None
    if not isinstance(rec, dict) or set(rec) != keys | {"kind", "record_sha256"} or rec["kind"] != kind:
        raise _Invalid(f"{rel} at {tag}: unexpected fields or kind")
    if rec["record_sha256"] != record_sha256(rec):
        raise _Invalid(f"{rel} at {tag}: record_sha256 does not verify")
    return rec


def _load_rv6(repo: Path) -> RV6Release:
    rec = _record(repo, RV6_TAG, RV6_RECORD_REL, RV6_KIND, {"basis", "r_v6", "evidence", "v6_holdout"})
    basis = rec["basis"]
    if basis not in BASES:
        raise _Invalid(f"unknown R_V6 basis {basis!r}")
    r_v6 = _ts(rec["r_v6"], "r_v6")
    ev = _doc(repo, RV6_TAG, rec["evidence"], "evidence")
    hold = rec["v6_holdout"]
    if not isinstance(hold, dict) or hold.get("status") not in HOLDOUT_STATUS:
        raise _Invalid("v6_holdout.status must be one of " + ", ".join(HOLDOUT_STATUS))
    _doc(repo, RV6_TAG, {k: v for k, v in hold.items() if k != "status"}, "v6_holdout")
    v6 = v6_artifacts(repo)
    floor = {"a": v6.looks[2], "b": v6.looks[0], "c": v6.holdout_start}[basis]
    if r_v6 < floor:
        raise _Invalid(f"r_v6 precedes the event of basis ({basis})")
    return RV6Release(r_v6, basis, ev, hold["status"], rec["record_sha256"])


def _load_review(repo: Path) -> ReviewPass:
    rec = _record(repo, TF7_TAG, TF7_RECORD_REL, TF7_KIND, {"review_pass", "evidence"})
    t = _ts(rec["review_pass"], "review_pass")
    ev = _doc(repo, TF7_TAG, rec["evidence"], "evidence")
    return ReviewPass(t, tf7_from_review(t), ev, rec["record_sha256"])


def _absent_on_failure(fn, repo: Path):
    try:
        return fn(repo)
    except (_Invalid, SpecMismatch, ValueError, TypeError, KeyError):
        return None


@functools.cache
def _canonical(repo_key: str) -> tuple[RV6Release | None, ReviewPass | None]:
    repo = Path(repo_key)
    return _absent_on_failure(_load_rv6, repo), _absent_on_failure(_load_review, repo)


def _key(repo: Path | None) -> str:
    return str(Path(repo or code_repo()).resolve())


def cache_clear() -> None:
    _canonical.cache_clear()


def rv6_release(repo: Path | None = None) -> RV6Release | None:
    """The verified R_V6 release record, else None (absent or not verifying: M6 stays sealed)."""
    return _canonical(_key(repo))[0]


def review_pass(repo: Path | None = None) -> ReviewPass | None:
    """The verified forward review-pass record, else None (T_F7 undefined)."""
    return _canonical(_key(repo))[1]


def r_v6(repo: Path | None = None) -> int | None:
    rel = rv6_release(repo)
    return None if rel is None else rel.r_v6


def t_f7(repo: Path | None = None) -> int | None:
    rp = review_pass(repo)
    return None if rp is None else rp.t_f7


def l7(repo: Path | None = None, *, extended: bool = False) -> int | None:
    """L7 = max(T_F7 + 180 d, R_V6) + 1 d (T_F7 + 270 d after the extension); None unless both canonical
    records verify."""
    rv, tf = r_v6(repo), t_f7(repo)
    if rv is None or tf is None:
        return None
    return _l7(tf, rv, bool(extended))


__all__ = ["BASES", "HOLDOUT_STATUS", "RV6Release", "RV6_KIND", "RV6_RECORD_REL", "RV6_TAG", "ReviewPass",
           "TF7_KIND", "TF7_RECORD_REL", "TF7_TAG", "cache_clear", "l7", "r_v6", "record_sha256",
           "review_pass", "rv6_release", "t_f7", "tf7_from_review", "trusted_now_ns"]
