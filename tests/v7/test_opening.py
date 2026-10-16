"""SC-05 (red-team M4 / m4) and the Task 23 carry-overs I-1 / M-5: ``authorize_holdout`` enforces the
DEC-19 prerequisites from a persisted, hash-checked pre-opening record; ``check_outcome_window`` re-checks the
markers and the pin for every W_HOLD window; the authorization cannot be forged (spec DEC-19, L14, §17,
S-10.7; AMB-34). Synthetic only: a deterministic frozen git repo, an injected ``home``, the real leakage
producers on the tiny ``leak_fixtures`` panels and a DevSelection from the module-private issuer (test hook,
documented in ``auth_fixtures``)."""

from __future__ import annotations

import json
import re
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from luna.v7 import guard, leakage, opening, runner_select, runstore, selection, windows
from luna.v7 import manifest as wman
from luna.v7.spec import code_repo
from luna.v7.timeutil import D
from tests.v7 import auth_fixtures as af
from tests.v7 import leak_fixtures as lf

HOLD = (windows.W_HOLD[0] + D, windows.W_HOLD[0] + 2 * D)
NOW = windows.W_HOLD[1] + D


_MP: list = []  # this test's monkeypatch (autouse fixture): the guard's location seam is set through it


@pytest.fixture(autouse=True)
def _canonical_location(tmp_path, monkeypatch):
    """Every test runs with the guard's single location seam (``guard._roots``) at tmp_path/repo, home."""
    af.roots(monkeypatch, tmp_path / "repo", tmp_path / "home")
    _MP[:] = [monkeypatch]
    yield
    _MP.clear()


def _opened(base: Path, **kw):
    return af.authorize(base, _MP[0], **kw)


def _open(o, fresh: bool = True) -> None:
    guard.check_outcome_window(*HOLD, auth=o.auth, now_ns=NOW, fresh=fresh)


def _verify(o, auth=None) -> dict:
    return guard.verify_authorization(o.auth if auth is None else auth)


def _rehash(path: Path, mutate) -> None:
    """Mutate the persisted record and recompute every content hash: the record is self-consistent, so only
    the semantic checks can refuse it."""
    rec = json.loads(path.read_text(encoding="utf-8"))
    mutate(rec)
    rec["dev_selection_sha256"] = opening.content_sha256(rec["dev_selection"])
    rec["leakage_report_sha256"] = opening.content_sha256(rec["leakage_report"])
    rec["record_sha256"] = opening.content_sha256({k: v for k, v in rec.items() if k != "record_sha256"})
    path.write_text(json.dumps(rec), encoding="utf-8")


def _authorize(base: Path, record: Path, **kw):
    repo = base / "repo"
    if not repo.exists():
        af.frozen_repo(repo)
    af.roots(_MP[0], repo, base / "home")
    with af.verify_spec_off():
        return guard.authorize_holdout(kw.pop("phrase", guard.approval_phrase()), pre_opening=record,
                                       manifest_sha256=kw.pop("manifest", af.MANIFEST))


def _refused(base: Path, record: Path, match: str, **kw) -> None:
    with pytest.raises(guard.HoldoutBlocked, match=match):
        _authorize(base, record, **kw)
    assert not guard.holdout_opened()
    if (base / "home").exists():  # nothing but the attempt log
        assert {p.name for p in (base / "home").rglob("*") if p.is_file()} <= {guard.OPENING_LOG, wman.FILE}


# --------------------------------------------------------------------------------------------- forgery


def test_forged_authorization_is_refused(tmp_path):
    """Red-team M4 probe: ``HoldoutAuthorization(HoldoutAuthorization._secret, {})`` allowed a W_HOLD window
    while ``holdout_opened()`` was False."""
    assert not hasattr(guard.HoldoutAuthorization, "_secret")
    with pytest.raises(AttributeError):
        guard.HoldoutAuthorization(guard.HoldoutAuthorization._secret, {})  # type: ignore[attr-defined]
    for key in (object(), None, guard):
        with pytest.raises(guard.HoldoutBlocked):
            guard.HoldoutAuthorization(key, {})
    forged = object.__new__(guard.HoldoutAuthorization)  # any analogue built around the constructor
    af.frozen_repo(tmp_path / "repo")
    for auth in (forged, object(), None):
        with pytest.raises(guard.HoldoutBlocked):
            guard.check_outcome_window(*HOLD, auth=auth, now_ns=NOW)  # type: ignore[arg-type]
        with pytest.raises(guard.HoldoutBlocked):
            guard.verify_authorization(auth)


def test_authorization_without_markers_or_pin_is_refused(tmp_path):
    o = _opened(tmp_path)
    _open(o)
    markers = guard.marker_paths()
    pins = [m.parent / guard.PIN for m in markers]
    original = {p: p.read_bytes() for p in markers + pins}
    for victim in markers + pins:
        victim.unlink()  # marker (or pin) missing: the authorization object alone opens nothing
        with pytest.raises(guard.HoldoutBlocked):
            _open(o)  # a fresh check (engine.run start, holdout_inputs) re-reads the canonical files
        with pytest.raises(guard.HoldoutBlocked):
            _open(o, fresh=False)  # the failed check evicted it from the verified cache
        victim.write_bytes(original[victim])
        _open(o)
    for victim in markers + pins:  # present but altered (other run, other content)
        rec = json.loads(original[victim])
        rec["run_id"] = "v7-0000000000000000"
        victim.write_text(json.dumps(rec), encoding="utf-8")
        with pytest.raises(guard.HoldoutBlocked):
            _open(o)
        victim.write_bytes(original[victim])
    _open(o)


# ------------------------------------------------------------------------------------------- happy path


def test_opening_happy_path_pins_the_dev_selection_and_the_report(tmp_path):
    o = _opened(tmp_path)
    assert guard.holdout_opened()
    pin = guard.pinned_manifest()
    assert pin == _verify(o) == o.auth.record["pin"]
    assert pin["code_commit"] == af.HEAD_COMMIT and pin["manifest_sha256"] == af.MANIFEST
    assert pin["dev_selection_sha256"] == o.dev.sha256
    assert pin["leakage_report_sha256"] == o.report.sha256
    assert pin["final_model_hash"] == o.report.final_model_hash and pin["ml_star"] == ["A2", 72]
    assert pin["dev_ml_run_sha256"] == runner_select.runrecord.run_output_sha256(o.dev.runs[("A2", 72)])
    assert pin["dev_ml_run_file_sha256"] == runstore.run_file_sha256(o.dev.runs[("A2", 72)])
    assert pin["pre_opening_sha256"] == json.loads(o.record.read_text(encoding="utf-8"))["record_sha256"]
    # the reloaded report is the pinned one and verifies in this process
    assert o.auth.leakage_report.sha256 == o.report.sha256 and leakage.verify_report(o.auth.leakage_report)
    _open(o)
    with pytest.raises(guard.HoldoutBlocked, match="already opened"):  # one opening only
        _authorize(tmp_path, o.record)


def test_cross_process_reverification(tmp_path, monkeypatch):
    """I-1: the opening runs in a later process: nothing issued in this process is needed; the persisted
    records are reloaded, re-hashed and re-checked."""
    rec = af.pre_opening(tmp_path / "pre.json")
    monkeypatch.setattr(leakage, "_ISSUED", set())  # a fresh process: no report issued here
    monkeypatch.setattr(runner_select, "_DEV_ISSUED", set())
    auth = _authorize(tmp_path, rec)
    rep = auth.leakage_report
    assert leakage.verify_report(rep) and rep.passed and rep.ml_star == ("A2", 72)


def test_preflight_requires_every_prerequisite(tmp_path):
    af.frozen_repo(tmp_path / "repo")
    with af.verify_spec_off(), pytest.raises(guard.HoldoutBlocked, match="pre-opening record"):
        guard.preflight()
    rec = af.pre_opening(tmp_path / "pre.json")
    with af.verify_spec_off(), pytest.raises(guard.HoldoutBlocked, match="manifest"):
        guard.preflight(pre_opening=rec)
    with af.verify_spec_off():
        pf = guard.preflight(pre_opening=rec, manifest_sha256=af.MANIFEST)
    assert pf["head"] == af.HEAD_COMMIT and pf["pre_opening"].leakage_report.passed


# ---------------------------------------------------------------------------------- missing preconditions


def test_missing_preconditions_are_refused(tmp_path):
    rec = af.pre_opening(tmp_path / "pre.json")
    _refused(tmp_path, rec, "approval", phrase="yes")
    _refused(tmp_path, rec, "approval", phrase=None)
    _refused(tmp_path, None, "pre-opening record")  # no DevSelection / LeakageReport at all
    _refused(tmp_path, tmp_path / "missing.json", "pre-opening record")
    for bad in (None, "", "xyz", "C" * 64):
        _refused(tmp_path, rec, "manifest", manifest=bad)
    _authorize(tmp_path, rec)  # all present: opens


def test_writer_refuses_what_the_opening_would_refuse(tmp_path):
    good = af.dev_selection()
    rep = lf.passed_report("A2", 72)
    p = tmp_path / "x.json"
    cases = [
        (af.dev_selection(kill=("F_net <= 0",)), rep, "kill"),
        (af.dev_selection(kill=None, result=selection.Invalid("INVALID RUN", ("A0@24h: x",))), rep, "choice"),
        (replace(good, fold_nets=(9.0,)), rep, "DevSelection"),  # copied-and-altered: no longer verifies
        (good, replace(rep, commit="d" * 40), "LeakageReport"),  # forged report
        (good, lf.run_checks("A2", 72, samples={}, fold_models={}, final_model=None), "did not pass"),
        (good, lf.passed_report("A2", 24), "ML"),  # a report for a different DevSelection (ML*)
    ]
    for dev, report, match in cases:
        with pytest.raises(ValueError, match=match):
            opening.write_pre_opening_record(p, dev, report, af.samples())
        assert not p.exists()
    with pytest.raises(ValueError, match="panel"):  # M-5: the report's panels are the DevSelection's
        opening.write_pre_opening_record(p, good, rep, {**af.samples(), 72: lf.panel(72, seed=5)})
    with pytest.raises(TypeError):  # a hand-built DevSelection cannot exist
        runner_select.DevSelection(**{f: getattr(good, f) for f in (
            "window", "quarter_edges", "runs", "models", "result", "kill", "fold_nets", "em_runs",
            "convergence")})
    assert runner_select.verify_dev_selection(good) and not runner_select.verify_dev_selection(object())


def _drop_l9a(r):
    r["leakage_report"]["l9a"] = r["leakage_report"]["l9a"][1:]


def _fail_l9a(r):
    r["leakage_report"]["l9a"][0]["passed"] = False


def _open_k7(r):
    k = r["leakage_report"]["k7"][0]
    k.update(evaluated=True, trigger=True, resolution=None)
    r["leakage_report"]["k7_expected"].append([k["horizon"], k["model"], k["family"]])


def _unexplained_k7(r):
    _open_k7(r)
    r["leakage_report"]["k7"][0].update(resolution="unexplained", resolution_text="audit: leak")


def _other_dev(r):
    r["dev_selection"]["fold_model_hashes"] = [x if x[0] != "A2" else [x[0], x[1], ["0" * 64]]
                                               for x in r["dev_selection"]["fold_model_hashes"]]


def _kill(r):
    r["dev_selection"]["kill"] = ["F_net <= 0"]


def _commit(r):
    r["code_commit"] = "d" * 40
    r["leakage_report"]["commit"] = "d" * 40


def _panels(r):
    r["dev_label_panels"]["72"] = "0" * 64


def _ml_star(r):
    r["dev_selection"]["ml_star"] = ["A3", 72]
    r["dev_selection"]["result"]["arm"] = "A3"


def _dirty(r):
    r["leakage_report"]["dirty"] = True


def _no_final(r):
    r["leakage_report"]["final_model_hash"] = None


@pytest.mark.parametrize("mutate,match", [
    (_drop_l9a, "not executed"), (_fail_l9a, "L9a"), (_open_k7, "audit pending"), (_unexplained_k7, "K1"),
    (_other_dev, "fold models"), (_kill, "kill"), (_commit, "HEAD"), (_panels, "panel"),
    (_ml_star, "ML"), (_dirty, "dirty"), (_no_final, "final model"),
], ids=lambda x: getattr(x, "__name__", x))
def test_persisted_record_semantics_are_rechecked(tmp_path, mutate, match):
    """I-1: every check executed and passed, K7 triggers resolved "explained", the DevSelection <-> report
    link, the code commit == HEAD, the panels (M-5): re-derived from the persisted content."""
    rec = af.pre_opening(tmp_path / "pre.json")
    _rehash(rec, mutate)
    _refused(tmp_path, rec, match)


def test_explained_k7_trigger_is_accepted(tmp_path):
    rec = af.pre_opening(tmp_path / "pre.json")

    def explained(r):
        _open_k7(r)
        r["leakage_report"]["k7"][0].update(resolution="explained", resolution_text="audit: benign")

    _rehash(rec, explained)
    auth = _authorize(tmp_path, rec)
    assert auth.leakage_report.passed and auth.leakage_report.k7[0].resolution == "explained"


def test_tampered_or_rehashed_records_are_refused(tmp_path):
    rec = af.pre_opening(tmp_path / "pre.json")
    raw = json.loads(rec.read_text(encoding="utf-8"))

    def put(r):
        rec.write_text(json.dumps(r), encoding="utf-8")

    # tampered content without the hashes: refused
    t = json.loads(json.dumps(raw))
    t["dev_selection"]["kill"] = ["F_net <= 0"]
    put(t)
    _refused(tmp_path, rec, "hash")
    t = json.loads(json.dumps(raw))
    t["leakage_report"]["l9a"][0]["ic"] = 0.5
    put(t)
    _refused(tmp_path, rec, "hash")
    # a mismatched recorded hash: refused
    for field in ("dev_selection_sha256", "leakage_report_sha256", "record_sha256"):
        t = json.loads(json.dumps(raw))
        t[field] = "0" * 64
        put(t)
        _refused(tmp_path, rec, "hash")
    for bad in ("not json", "[]", json.dumps({**raw, "extra": 1})):
        rec.write_text(bad, encoding="utf-8")
        _refused(tmp_path, rec, "pre-opening record")
    put(raw)
    _authorize(tmp_path, rec)


def test_dev_issuer_is_only_used_by_dev_selection():
    """The DevSelection issuer is the module-private producer hook: no production module but runner_select
    references it, and there only ``dev_selection`` (and its definition) uses it."""
    root = Path(code_repo()) / "luna"
    users = [p for p in root.rglob("*.py") if "_issue_dev" in p.read_text(encoding="utf-8")]
    assert users == [root / "v7" / "runner_select.py"]
    src = users[0].read_text(encoding="utf-8")
    assert len(re.findall(r"_issue_dev\(", src)) == 3  # the definition and the two returns of dev_selection


# --------------------------------------------- rounds 1-2: forgery, location, cache, verdict; R45 resume


def _consistent_state(code_commit: str = "0" * 40) -> tuple[dict, dict]:
    """A SELF-CONSISTENT forged opening (valid opening_id and marker_sha256, review round 2 probe P1): only
    the location of its files can tell it from a real one."""
    z = "a" * 64
    pin = {"run_id": guard.RUN_ID, "spec_sha256": guard.SPEC_BLOB_SHA256, "code_commit": code_commit,
           "manifest_sha256": z, "pre_opening_sha256": z, "dev_selection_sha256": z,
           "leakage_report_sha256": z, "ml_star": ["A2", 72], "fold_model_hashes": {}, "final_model_hash": z,
           "dev_ml_run_sha256": z, "dev_ml_run_file_sha256": z, "opened_at": "x"}
    pin["opening_id"] = guard._opening_id(pin)
    marker = {"head": code_commit, "spec_sha256": guard.SPEC_BLOB_SHA256, "run_id": guard.RUN_ID,
              "approved": True, "opened_at": "x", "opening_id": pin["opening_id"]}
    pin["marker_sha256"] = guard._sha(marker)
    return marker, pin


def _forged_records(base: Path) -> dict:
    """The consistent forged state written to self-chosen tmp files, and a record pointing at them."""
    marker, pin = _consistent_state()
    base.mkdir(parents=True, exist_ok=True)
    ms, ps = [base / "m1.json", base / "m2.json"], [base / "p1.json", base / "p2.json"]
    for m in ms:
        m.write_text(json.dumps(marker), encoding="utf-8")
    for p in ps:
        p.write_text(json.dumps(pin), encoding="utf-8")
    return {"repo": str(base), "home": str(base), "markers": [str(m) for m in ms],
            "pins": [str(p) for p in ps], "marker": marker, "pin": pin, "opening_id": pin["opening_id"]}


def _closure_key():
    cells = [c.cell_contents for f in (guard.HoldoutAuthorization.__init__, guard.authorize_holdout,
                                       guard.resume_holdout) for c in (f.__closure__ or ())]
    return next(c for c in cells if type(c) is object)


def _forgeries(rec: dict) -> list:
    slots = object.__new__(guard.HoldoutAuthorization)  # route 1: around the constructor, writable slots
    slots._record = json.dumps(rec)
    slots._report = None
    keyed = guard.HoldoutAuthorization(_closure_key(), rec)  # route 2: the key read from a closure
    return [slots, keyed]


def _refused_window(auth) -> None:
    with pytest.raises(guard.HoldoutBlocked):
        guard.verify_authorization(auth)
    for fresh in (True, False):
        with pytest.raises(guard.HoldoutBlocked):
            guard.check_outcome_window(*HOLD, auth=auth, now_ns=NOW, fresh=fresh)


def test_forged_authorization_cannot_open_w_hold(tmp_path):
    """Required test 1: the three forgery routes, with a self-consistent forged opening at self-chosen
    paths. Route 3 (a module-level issuer) no longer exists; routes 1 and 2 are refused because the guard
    reads only the canonical files (``_roots``), where no opening exists."""
    assert not hasattr(guard, "_issue_authorization") and not hasattr(guard, "_authorization_type")
    assert not hasattr(guard.HoldoutAuthorization, "_secret")
    af.frozen_repo(tmp_path / "repo")
    for forged in _forgeries(_forged_records(tmp_path / "fake")):
        _refused_window(forged)
    assert not guard.holdout_opened()


def test_alternate_temp_marker_pin_files_cannot_open_w_hold(tmp_path):
    """Required test 2: self-written, self-consistent marker / pin files elsewhere never count, even when a
    real opening exists at the canonical location; the record's paths are never used."""
    o = _opened(tmp_path)
    for forged in _forgeries(_forged_records(tmp_path / "fake")):
        _refused_window(forged)
    _open(o)
    # the same consistent forged state at the canonical paths of ANOTHER repository / home (probe P1) does
    # not make that location canonical: the guard's location is the seam, here still the real opening's
    other_repo, other_home = af.frozen_repo(tmp_path / "other" / "repo"), tmp_path / "other" / "home"
    marker, pin = _consistent_state()
    af.roots(_MP[0], other_repo, other_home)
    for d in guard._dirs():
        d.mkdir(parents=True, exist_ok=True)
        (d / guard.PIN).write_text(json.dumps(pin), encoding="utf-8")
        (d / guard.MARKER).write_text(json.dumps(marker), encoding="utf-8")
    keyed = guard.HoldoutAuthorization(_closure_key(), {"marker": marker, "pin": pin})
    guard.verify_authorization(keyed)  # it IS canonical there (the seam points at it): a cached entry ...
    af.roots(_MP[0], o.repo, o.home)
    _refused_window(keyed)  # ... is worth nothing at the real opening's canonical location


def test_cache_is_bound_to_location_and_state(tmp_path):
    """Review round 2, I-A: a verified authorization is cached with its location and the stat signature of the
    canonical files; another location, or any later change of the pin / markers / verdict marker, re-reads the
    files on the NON-fresh path too (the label path)."""
    o = _opened(tmp_path)
    _open(o)
    af.roots(_MP[0], af.frozen_repo(tmp_path / "y" / "repo"), tmp_path / "y" / "home")
    with pytest.raises(guard.HoldoutBlocked):
        _open(o, fresh=False)  # verified at X, used at Y (no opening there)
    af.roots(_MP[0], o.repo, o.home)
    _open(o, fresh=False)
    pin = guard.marker_paths()[0].parent / guard.PIN
    orig = pin.read_bytes()
    rec = json.loads(orig)
    rec["manifest_sha256"] = "e" * 64
    pin.write_text(json.dumps(rec), encoding="utf-8")
    with pytest.raises(guard.HoldoutBlocked):
        _open(o, fresh=False)  # pin changed after verification
    pin.write_bytes(orig)
    _open(o, fresh=False)
    guard._VERIFIED[o.auth] = ("forged",)  # a tampered cache entry never matches the signature
    _open(o, fresh=False)  # re-verified from the files (still a real opening)


def test_verified_authorization_is_cached_until_a_fresh_check(tmp_path, monkeypatch):
    """Review m1: labels (many calls per span) do not re-read the files; a fresh check does."""
    o = _opened(tmp_path)
    _open(o)
    calls = []
    real = guard._read
    monkeypatch.setattr(guard, "_read", lambda p: calls.append(p) or real(p))
    for _ in range(50):
        _open(o, fresh=False)
    assert calls == []
    _open(o)
    assert len(calls) >= 4


def test_no_caller_supplied_opening_location():
    """Review round 2, I-B: no opening, resume, verification, verdict or summary entry point takes a
    repository / home / path, so a relocated resume (a clone with copied markers) or a diverted verdict
    marker cannot be expressed."""
    import inspect

    from luna.v7 import runner_select as rs

    for fn in (guard.authorize_holdout, guard.resume_holdout, guard.verify_authorization, guard.preflight,
               guard.mark_verdict_produced, guard.opening_summary, guard.marker_paths, guard.holdout_opened,
               guard.pinned_manifest, guard.verdict_produced, guard.resume_records, guard.opening_records,
               rs.holdout_inputs, rs.holdout_verdict):
        params = set(inspect.signature(fn).parameters)
        assert not params & {"repo", "home", "path", "root", "markers"}, (fn.__name__, params)
    with pytest.raises(TypeError):
        guard.resume_holdout(guard.resume_phrase(), Path("clone"), home=Path("h"))  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        guard.mark_verdict_produced(None, "f" * 64, repo=Path("x"), home=Path("y"))  # type: ignore[call-arg]


def test_verdict_closes_the_opening(tmp_path):
    """Review round 2, I-C: after the verdict marker, the same authorization (or one rebuilt from the
    canonical files) opens no W_HOLD window, fresh or cached; the summary stays readable."""
    o = _opened(tmp_path)
    _open(o)
    m = guard.marker_paths()[0]
    rebuilt = guard.HoldoutAuthorization(_closure_key(), {"marker": json.loads(m.read_text(encoding="utf-8")),
                                                          "pin": json.loads((m.parent / guard.PIN).read_text(
                                                              encoding="utf-8"))})
    _open(o, fresh=False)
    guard.mark_verdict_produced(o.auth, "f" * 64)
    for auth in (o.auth, rebuilt):
        _refused_window(auth)
    s = guard.opening_summary(o.auth)
    assert s["verdict_produced"]["verdict_sha256"] == "f" * 64 and s["opening_id"] == o.auth.opening_id


def test_every_attempt_is_logged_including_unexpected_errors(tmp_path, monkeypatch):
    """Opening attempts are logged (OPENING_ATTEMPTS.jsonl); resume attempts refused by ANY exception are
    logged too (RESUME_ATTEMPTS.jsonl)."""
    rec = af.pre_opening(tmp_path / "pre.json")
    with pytest.raises(guard.HoldoutBlocked):
        _authorize(tmp_path, rec, phrase="no")
    o = SimpleNamespace(repo=tmp_path / "repo", home=tmp_path / "home", record=rec)
    o.auth = _authorize(tmp_path, rec)
    assert [x["result"] for x in guard.opening_records()] == ["refused", "opened"]
    assert guard.opening_records()[1]["opening_id"] == o.auth.opening_id

    def boom(**kw):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(guard, "preflight", boom)
    with pytest.raises(RuntimeError):
        _resume(o)
    log = guard.resume_records()
    assert log[-1]["result"] == "refused" and "RuntimeError: disk on fire" in log[-1]["reason"]


def _resume(o, **kw):
    with af.verify_spec_off():
        return guard.resume_holdout(kw.pop("phrase", guard.resume_phrase()),
                                    pre_opening=kw.pop("pre_opening", o.record),
                                    manifest_sha256=kw.pop("manifest", af.MANIFEST))


def _state(o) -> list:
    """Every file of the canonical opening (contents), the attempt logs excluded."""
    out = []
    for m in guard.marker_paths():
        for p in sorted(m.parent.iterdir()):
            if p.name not in (guard.RESUME_LOG, guard.OPENING_LOG):
                out.append((str(p), p.read_bytes()))
    return out


def _refuse_resume(o, match, **kw):
    before, n = _state(o), len(guard.resume_records())
    with pytest.raises(guard.HoldoutBlocked, match=match):
        _resume(o, **kw)
    assert _state(o) == before  # nothing written but the log
    log = guard.resume_records()
    assert len(log) == n + 1 and log[-1]["result"] == "refused" and log[-1]["reason"] and log[-1]["utc"]


def test_changed_commit_cannot_resume(tmp_path):
    """Required test 3: a new commit (HEAD moved), or the code tag moved with it: refused, sealed."""
    o = _opened(tmp_path)
    (o.repo / "b.txt").write_text("y", encoding="utf-8")
    _refuse_resume(o, "not clean")
    af.git(o.repo, "add", "b.txt")
    af.git(o.repo, "commit", "-q", "-m", "moved")
    _refuse_resume(o, "HEAD is not the")
    af.git(o.repo, "tag", "-f", "-a", guard.CODE_TAG, "-m", "moved")  # tmp fixture repo only
    _refuse_resume(o, "code commit|not at HEAD")


def _rewrite_canonical(marker_edit=None, pin_edit=None) -> None:
    """Rewrite the canonical opening SELF-CONSISTENTLY (opening_id, marker_sha256 recomputed) so only the
    resume's pinned-diff rows can refuse it."""
    d = guard.marker_paths()[0].parent
    marker, pin = (json.loads((d / n).read_text(encoding="utf-8")) for n in (guard.MARKER, guard.PIN))
    (marker_edit or (lambda m: None))(marker)
    (pin_edit or (lambda p: None))(pin)
    pin["opening_id"] = guard._opening_id(pin)
    marker["opening_id"] = pin["opening_id"]
    pin["marker_sha256"] = guard._sha(marker)
    for dd in guard._dirs():
        (dd / guard.PIN).write_text(json.dumps(pin), encoding="utf-8")
        (dd / guard.MARKER).write_text(json.dumps(marker), encoding="utf-8")


def test_changed_commit_rows_are_checked_independently(tmp_path):
    """Required test 3 (review round 2): the pinned ``code commit`` and the marker ``head`` are each compared
    with HEAD on their own (a consistent canonical state differing only there is refused)."""
    o = _opened(tmp_path)
    good = [(p, p.read_bytes()) for d in guard._dirs() for p in (d / guard.PIN, d / guard.MARKER)]
    _rewrite_canonical(pin_edit=lambda p: p.update(code_commit="d" * 40))
    _refuse_resume(o, r"code commit\)")
    for p, data in good:
        p.write_bytes(data)
    _rewrite_canonical(marker_edit=lambda m: m.update(head="d" * 40))
    _refuse_resume(o, r"\(marker head\)")
    for p, data in good:
        p.write_bytes(data)
    _resume(o)


def test_changed_manifest_cannot_resume(tmp_path):
    """Required test 4."""
    o = _opened(tmp_path)
    _refuse_resume(o, "manifest", manifest="d" * 64)
    _refuse_resume(o, "manifest", manifest=None)


def test_changed_leakage_report_or_dev_selection_cannot_resume(tmp_path):
    """Required test 5: another valid pre-opening record (other LeakageReport content, or another
    DevSelection) is not the pinned one."""
    o = _opened(tmp_path)
    raw = json.loads(o.record.read_text(encoding="utf-8"))

    def explained(r):  # another, still passing, LeakageReport
        k = r["leakage_report"]["k7"][0]
        k.update(evaluated=True, trigger=True, resolution="explained", resolution_text="benign")
        r["leakage_report"]["k7_expected"].append([k["horizon"], k["model"], k["family"]])

    def other_dev(r):  # another DevSelection with the same ML* and fold models
        r["dev_selection"]["fold_nets"] = [9.0]

    for name, mutate, match in (("rep", explained, "LeakageReport"), ("dev", other_dev, "DevSelection")):
        p = tmp_path / f"{name}.json"
        p.write_text(json.dumps(raw), encoding="utf-8")
        _rehash(p, mutate)
        _refuse_resume(o, match, pre_opening=p)
    _refuse_resume(o, "pre-opening record", pre_opening=None)


def test_resume_after_verdict_is_refused(tmp_path):
    """Required test 6: once the verdict marker exists, resume is permanently refused (no second look)."""
    o = _opened(tmp_path)
    rec = guard.mark_verdict_produced(o.auth, "f" * 64)
    assert rec["opening_id"] == o.auth.opening_id
    _refuse_resume(o, "verdict")
    _refuse_resume(o, "verdict")
    with pytest.raises(guard.HoldoutBlocked, match="verdict"):
        guard.mark_verdict_produced(o.auth, "e" * 64)
    summary = guard.opening_summary(o.auth)
    assert summary["verdict_produced"]["verdict_sha256"] == "f" * 64
    with pytest.raises(guard.HoldoutBlocked, match="already opened"):  # nor a new opening
        _authorize(tmp_path, o.record)


def test_crash_with_unchanged_state_can_resume(tmp_path):
    """Required test 7: the opening process is gone (its authorization object lost); the unchanged state
    resumes the SAME opening and W_HOLD windows pass again."""
    o = _opened(tmp_path)
    oid = o.auth.opening_id
    before = _state(o)
    del o.auth  # the crashed process's authorization is gone
    a = _resume(o)
    assert a.opening_id == oid and a.record["resumed"] is True and a.leakage_report.sha256 == o.report.sha256
    guard.check_outcome_window(*HOLD, auth=a, now_ns=NOW, fresh=True)
    assert _state(o) == before  # no new marker, pin or opening id
    log = guard.resume_records()
    assert [x["result"] for x in log] == ["resumed"] and log[0]["opening_id"] == oid
    assert log[0]["head"] == af.HEAD_COMMIT and log[0]["pid"] > 0 and log[0]["utc"]


def test_repeated_resume_is_idempotent_same_opening(tmp_path):
    """Required test 8: repeated resumes (and refused attempts in between) stay the same opening: same
    opening_id, no new opening marker, every attempt logged in both canonical directories."""
    o = _opened(tmp_path)
    oid, before = o.auth.opening_id, _state(o)
    ids = [_resume(o).opening_id for _ in range(3)]
    _refuse_resume(o, "resume phrase", phrase=guard.approval_phrase())  # the opening phrase is not it
    _refuse_resume(o, "resume phrase", phrase=None)
    ids.append(_resume(o).opening_id)
    assert ids == [oid] * 4 and _state(o) == before and guard.holdout_opened()
    log = guard.resume_records()
    assert [x["result"] for x in log] == ["resumed"] * 3 + ["refused"] * 2 + ["resumed"]
    logs = [(m.parent / guard.RESUME_LOG).read_text(encoding="utf-8")
            for m in guard.marker_paths()]
    assert logs[0] == logs[1] and len(logs[0].splitlines()) == 6
    summary = guard.opening_summary(o.auth)
    assert summary["opening_id"] == oid and summary["resumes"] == log and summary["verdict_produced"] is None
    # the log never authorizes anything: an attempt log alone (no opening) leaves the holdout sealed
    assert guard.resume_phrase() != guard.approval_phrase()


def test_resume_needs_an_opening_and_logs_the_refusal(tmp_path):
    af.frozen_repo(tmp_path / "repo")
    rec = af.pre_opening(tmp_path / "pre.json")
    with af.verify_spec_off(), pytest.raises(guard.HoldoutBlocked, match="not opened"):
        guard.resume_holdout(guard.resume_phrase(), pre_opening=rec,
                             manifest_sha256=af.MANIFEST)
    assert [x["result"] for x in guard.resume_records()] == ["refused"]
    assert not guard.holdout_opened()  # the log directory opens nothing
    _authorize(tmp_path, rec)  # a first opening is still possible after a logged refusal


def test_partial_or_altered_state_is_refused_for_governance(tmp_path):
    o = _opened(tmp_path)
    markers = guard.marker_paths()
    pins = [m.parent / guard.PIN for m in markers]
    for victim in markers + pins:
        data = victim.read_bytes()
        victim.unlink()
        _refuse_resume(o, "governance")
        victim.write_bytes(data)
        rec = json.loads(data)
        key = "opened_at" if victim.name == guard.MARKER else "manifest_sha256"
        rec[key] = "y" if key == "opened_at" else "e" * 64
        victim.write_text(json.dumps(rec), encoding="utf-8")
        _refuse_resume(o, "governance")
        victim.write_bytes(data)
    _resume(o)  # intact again: resumable


def test_hard_kill_states_are_left_to_governance(tmp_path, monkeypatch):
    """Pins are written before the markers: an exception rolls everything back; a hard kill part-way leaves
    pins without markers, which neither a new opening nor a resume accepts."""
    rec = af.pre_opening(tmp_path / "pre.json")
    real = guard._create
    n = {"k": 0}

    def dying(p, text):
        n["k"] += 1
        if n["k"] == 3:  # both pins written, the first marker not
            raise KeyboardInterrupt
        return real(p, text)

    monkeypatch.setattr(guard, "_create", dying)
    with pytest.raises(KeyboardInterrupt):
        _authorize(tmp_path, rec)
    home = tmp_path / "home"
    # rolled back (the pre-opening W_HOLD manifest stays)
    assert not [p for p in home.rglob("*.json") if p.name != wman.FILE] if home.exists() else True
    n["k"] = 0
    rollback = guard._rollback
    monkeypatch.setattr(guard, "_rollback", lambda written: None)  # a hard kill: no rollback runs
    with pytest.raises(KeyboardInterrupt):
        _authorize(tmp_path, rec)
    monkeypatch.setattr(guard, "_create", real)
    monkeypatch.setattr(guard, "_rollback", rollback)
    assert not guard.holdout_opened()
    with pytest.raises(guard.HoldoutBlocked, match="already opened"):
        _authorize(tmp_path, rec)
    with af.verify_spec_off(), pytest.raises(guard.HoldoutBlocked, match="governance"):
        guard.resume_holdout(guard.resume_phrase(), pre_opening=rec,
                             manifest_sha256=af.MANIFEST)


def test_persisted_report_loader_is_private_to_the_opening():
    """Review m4: only the opening flow rebuilds persisted reports."""
    assert not hasattr(leakage, "load_persisted_report")  # no public loader
    root = Path(code_repo()) / "luna"
    users = sorted(p.name for p in root.rglob("*.py")
                   if "_load_persisted_report" in p.read_text(encoding="utf-8"))
    assert users == ["leakage.py", "opening.py"]


# ---------------------------------------------------------------------------------------- round 3 minors


def test_same_size_in_place_rewrite_is_detected_on_the_cached_path(tmp_path):
    """Round 3 m-b: a same-size rewrite of a pin with its mtime restored still invalidates the cached
    verification (the signature has ctime and a digest of the raw bytes: on Windows ctime is the creation
    time, so stat alone cannot see it)."""
    import os

    o = _opened(tmp_path)
    _open(o)
    pin = guard.marker_paths()[0].parent / guard.PIN
    st = os.stat(pin)
    orig = pin.read_bytes()
    forged = orig.replace(af.MANIFEST.encode(), b"d" * 64)
    assert len(forged) == len(orig) and forged != orig
    with open(pin, "r+b") as f:  # in place, same size
        f.write(forged)
    os.utime(pin, ns=(st.st_atime_ns, st.st_mtime_ns))
    assert os.stat(pin).st_mtime_ns == st.st_mtime_ns and os.stat(pin).st_size == st.st_size
    with pytest.raises(guard.HoldoutBlocked):
        _open(o, fresh=False)
    with open(pin, "r+b") as f:
        f.write(orig)
    _open(o, fresh=False)


def test_attempt_log_failure_fails_closed_and_is_reported(tmp_path, monkeypatch):
    """Round 3 m-d: a refusal whose attempt cannot be logged still raises, with the logging failure noted; a
    success whose attempt cannot be logged is refused."""
    o = _opened(tmp_path)

    def broken(log, entry):
        raise OSError("log disk full")

    monkeypatch.setattr(guard, "_log_attempt", broken)
    with pytest.raises(guard.HoldoutBlocked) as e:
        _resume(o, phrase="wrong")
    assert "resume phrase" in str(e.value) and any("log disk full" in n for n in e.value.__notes__)
    with pytest.raises(guard.HoldoutBlocked, match="could not be logged"):
        _resume(o)


def test_log_attempt_writes_every_directory_and_fails_if_any_fails(tmp_path, monkeypatch):
    """Round 3 m-d: both canonical directories are attempted; any failure is raised (never swallowed)."""
    o = _opened(tmp_path)
    dirs = guard._log_dirs()
    real_open = open
    bad = str(dirs[0])

    def flaky(path, *a, **k):
        if str(path).startswith(bad) and str(path).endswith(guard.RESUME_LOG):
            raise OSError("home log unwritable")
        return real_open(path, *a, **k)

    monkeypatch.setattr("builtins.open", flaky)
    with pytest.raises(OSError, match="home log unwritable"):
        guard._log_attempt(guard.RESUME_LOG, {"x": 1})
    monkeypatch.setattr("builtins.open", real_open)
    assert (dirs[1] / guard.RESUME_LOG).read_text(encoding="utf-8").strip() == '{"x": 1}'
    assert o.auth is not None
