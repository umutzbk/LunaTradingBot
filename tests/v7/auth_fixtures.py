"""Test-only fixtures for the DEC-19 opening and the canonical R_V6 / T_F7 records (Task 24, SC-05 / SC-06,
AMB-34). No real data, never the real home directory or the real repository's git markers.

* ``frozen_repo(path)``: a hermetic git repository (fixed author / committer identity and dates, no global or
  system git config) with one commit carrying the annotated ``v7-ml-spec-frozen`` and ``v7-code-frozen``
  tags. Its HEAD is the constant ``HEAD_COMMIT`` (asserted), which ``leak_fixtures.COMMIT`` uses, so the
  cached passed LeakageReports carry the code commit the opening compares with HEAD.
* ``dev_selection(...)``: a DevSelection issued through ``runner_select._issue_dev``, the module-private
  issuer. TEST HOOK: production code calls it only inside ``runner_select.dev_selection`` (asserted by
  ``test_opening.test_dev_issuer_is_only_used_by_dev_selection``). The happy paths use it because a real
  S-10.6 run over a full outer quarter is too slow for the test gate; the LeakageReport is always the real
  ``leakage.run_leakage_checks`` output on the tiny synthetic panels of ``leak_fixtures``.
* ``authorize(...)``: writes the pre-opening record and opens a synthetic holdout at a tmp location
  (``roots`` monkeypatches the guard's single location seam ``guard._roots``; ``guard.verify_spec`` patched
  exactly as in the existing guard tests).
* ``release_repo(...)``: a repository with the V6 artefacts at ``v6-code-frozen`` (bytes copied from the real
  tag, so their hashes verify) and optional R_V6 release / forward review-pass records under their annotated
  tags (``luna.v7.release``).
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from luna.v7 import guard, opening, release, runner_select, selection, spec
from luna.v7.runner import FoldScoreModel
from tests.v7 import leak_fixtures as lf
from tests.v7.run_fixtures import funding_run

HEAD_COMMIT = lf.COMMIT  # the deterministic HEAD of ``frozen_repo`` (fixed content, identity and dates)
DATE = "2026-01-01T00:00:00+0000"
MANIFEST = "c" * 64  # the W_HOLD file manifest pinned at the opening (synthetic)


def git(repo: Path, *args: str) -> str:
    cfg = Path(repo).parent / "empty.gitconfig"
    cfg.touch()
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t", "GIT_AUTHOR_DATE": DATE, "GIT_COMMITTER_DATE": DATE,
           "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": str(cfg)}
    r = subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, env=env)
    return r.stdout.decode().strip()


def _init(path: Path) -> Path:
    path.mkdir(parents=True)
    git(path, "init", "-q")
    git(path, "config", "core.autocrlf", "false")
    return path


def frozen_repo(path: Path) -> Path:
    """The deterministic code-frozen repository (HEAD == HEAD_COMMIT, both V7 tags annotated on HEAD)."""
    r = _init(Path(path))
    (r / "a.txt").write_text("x", encoding="utf-8")
    _v6_files(r)  # the canonical M6 source of this repository (``check_outcome_window(repo=...)``)
    git(r, "add", "-A")
    git(r, "commit", "-q", "-m", "c")
    git(r, "tag", "-a", spec.V6_TAG, "-m", "v6")
    git(r, "tag", "-a", guard.SPEC_TAG, "-m", "spec")
    git(r, "tag", "-a", guard.CODE_TAG, "-m", "code")
    head = git(r, "rev-parse", "HEAD")
    assert head == HEAD_COMMIT, f"frozen_repo HEAD {head} != HEAD_COMMIT (update leak_fixtures.COMMIT)"
    return r


# ------------------------------------------------------------------------------------------ DevSelection


def fold_score_models() -> dict:
    fms = lf.fold_models()
    return {(a, h): FoldScoreModel(fms[h], a, h) for a in ("A2", "A3") for h in lf.HORIZONS}


def dev_selection(arm: str = "A2", h: int = 72, *, kill=(), runs=None, em_runs=None, result=None,
                  models=None, window=(lf.FOLD.test_lo, lf.FOLD.test_hi)):
    """A DevSelection through the module-private issuer (TEST HOOK, see the module docstring)."""
    if runs is None:
        runs = {(arm, h): funding_run([1e-3, 2e-3, 1e-3], arm=arm, h=h),
                ("A0", h): funding_run([5e-4] * 3, arm="A0", h=h)}
    if result is None:
        result = selection.Choice(arm, h, {}, 14, 200, 7)
    return runner_select._issue_dev(
        window=tuple(window), quarter_edges=(window[0], window[1]), runs=dict(runs),
        models=fold_score_models() if models is None else models, result=result,
        kill=None if kill is None else tuple(kill), fold_nets=None if kill is None else (1e-3,),
        em_runs=dict(em_runs or {}), convergence=())


def samples() -> dict:
    """The W_DEV label panels the cached passed reports were run on (``leak_fixtures.run_checks``)."""
    return lf.panels(0, None, lf.FOLD.test_hi)


def pre_opening(path: Path, dev=None, report=None, panels=None) -> Path:
    dev = dev if dev is not None else dev_selection()
    report = report if report is not None else lf.passed_report(*dev.ml_star)
    opening.write_pre_opening_record(path, dev, report, samples() if panels is None else panels)
    return Path(path)


def verify_spec_off():
    return mock.patch.object(guard, "verify_spec", lambda repo=None: None)


def roots(monkeypatch, repo: Path, home: Path) -> None:
    """Point the guard's single location seam (``guard._roots``) at a tmp repository / home (tests only: no
    public opening function takes a location)."""
    monkeypatch.setattr(guard, "_roots", lambda: (Path(repo), Path(home)))


def authorize(base: Path, monkeypatch, *, dev=None, report=None, manifest: str = MANIFEST) -> SimpleNamespace:
    """A synthetic opening in ``base`` (frozen repo, tmp home as the canonical location, real pre-opening
    record). ``monkeypatch`` keeps ``guard._roots`` pointed at it."""
    base = Path(base)
    repo = frozen_repo(base / "repo")
    home = base / "home"
    roots(monkeypatch, repo, home)
    dev = dev if dev is not None else dev_selection()
    report = report if report is not None else lf.passed_report(*dev.ml_star)
    rec = pre_opening(base / "pre_opening.json", dev, report)
    with verify_spec_off():
        auth = guard.authorize_holdout(guard.approval_phrase(), pre_opening=rec, manifest_sha256=manifest)
    return SimpleNamespace(auth=auth, repo=repo, home=home, record=rec, dev=dev, report=report)


# -------------------------------------------------------------------------------- R_V6 / T_F7 records


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _sealed(body: dict, tamper: bool = False) -> bytes:
    body = dict(body)
    body["record_sha256"] = release.record_sha256(body)
    if tamper:
        body["record_sha256"] = "0" * 64
    return json.dumps(body, indent=1, sort_keys=True).encode()


def _v6_files(r: Path) -> None:
    for rel in (spec.V6_CONSTANTS_REL, spec.V6_RECORD_REL):
        p = r / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(spec._git_show(spec.code_repo(), spec.V6_TAG, rel))


def release_repo(path: Path) -> Path:
    """A repository with the hash-checked V6 artefacts at ``v6-code-frozen`` and no release record yet."""
    r = _init(Path(path))
    _v6_files(r)
    git(r, "add", "-A")
    git(r, "commit", "-q", "-m", "v6")
    git(r, "tag", "-a", spec.V6_TAG, "-m", "v6")
    return r


def _commit_tag(repo: Path, files: dict[str, bytes], tag: str, *, annotated: bool = True) -> None:
    for rel, data in files.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", tag)
    if annotated:
        git(repo, "tag", "-a", tag, "-m", tag)
    else:
        git(repo, "tag", tag)


def add_release(repo: Path, r_v6: str, *, basis: str = "a", status: str = "opened_and_reported",
                tamper_record: bool = False, tamper_evidence: bool = False, tamper_holdout: bool = False,
                annotated: bool = True, evidence_at: str | None = None,
                holdout_at: str | None = None) -> None:
    """Commit an R_V6 release record (S-1.2) with its evidence documents and tag it ``release.RV6_TAG``.
    ``evidence_at`` / ``holdout_at``: the documented event times (default: ``r_v6``)."""
    ev, hold = b"V6 look report (synthetic)\n", b"V6 holdout report (synthetic)\n"
    body = {"kind": release.RV6_KIND, "basis": basis, "r_v6": r_v6,
            "evidence": {"at": evidence_at or r_v6, "path": "docs/results/v6-look-report.md",
                         "sha256": _sha(ev)},
            "v6_holdout": {"status": status, "at": holdout_at or r_v6,
                           "path": "docs/results/v6-holdout-report.md", "sha256": _sha(hold)}}
    files = {release.RV6_RECORD_REL: _sealed(body, tamper_record),
             "docs/results/v6-look-report.md": ev + (b"edited" if tamper_evidence else b""),
             "docs/results/v6-holdout-report.md": hold + (b"edited" if tamper_holdout else b"")}
    _commit_tag(repo, files, release.RV6_TAG, annotated=annotated)


def add_review(repo: Path, review_pass: str, *, tamper_record: bool = False) -> None:
    """Commit the forward review-pass record (§15 T_F7) and tag it ``release.TF7_TAG``."""
    ev = b"independent review PASS of the forward recorder and replay code (synthetic)\n"
    body = {"kind": release.TF7_KIND, "review_pass": review_pass,
            "evidence": {"path": "docs/results/v7-forward-review.md", "sha256": _sha(ev)}}
    files = {release.TF7_RECORD_REL: _sealed(body, tamper_record), "docs/results/v7-forward-review.md": ev}
    _commit_tag(repo, files, release.TF7_TAG)


def canonical_repo(path: Path, *, r_v6: str | None = "2027-10-14T00:00:00Z",
                   review_pass: str | None = "2026-10-15T00:00:00Z", **kw) -> Path:
    """A release repository with the given canonical records (None = absent)."""
    r = release_repo(path)
    if r_v6 is not None:
        add_release(r, r_v6, **kw)
    if review_pass is not None:
        add_review(r, review_pass)
    release.cache_clear()
    return r


def clock(monkeypatch, ns: int) -> None:
    """Pin the guards' trusted wall clock (tests only; no API takes a date)."""
    monkeypatch.setattr(release, "trusted_now_ns", lambda: int(ns))
