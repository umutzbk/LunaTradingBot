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

import pytest

from luna.v7 import guard, leakage, opening, runner_select, selection, windows
from luna.v7.spec import code_repo
from luna.v7.timeutil import D
from tests.v7 import auth_fixtures as af
from tests.v7 import leak_fixtures as lf

HOLD = (windows.W_HOLD[0] + D, windows.W_HOLD[0] + 2 * D)
NOW = windows.W_HOLD[1] + D


def _open(o) -> None:
    guard.check_outcome_window(*HOLD, auth=o.auth, now_ns=NOW)


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
    with af.verify_spec_off():
        return guard.authorize_holdout(kw.pop("phrase", guard.approval_phrase()), repo, home=base / "home",
                                       pre_opening=record, manifest_sha256=kw.pop("manifest", af.MANIFEST))


def _refused(base: Path, record: Path, match: str, **kw) -> None:
    with pytest.raises(guard.HoldoutBlocked, match=match):
        _authorize(base, record, **kw)
    assert not guard.holdout_opened(base / "repo", home=base / "home")
    assert not any((base / "home").rglob("*.json")) if (base / "home").exists() else True


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
    for auth in (forged, object(), None):
        with pytest.raises(guard.HoldoutBlocked):
            guard.check_outcome_window(*HOLD, auth=auth, now_ns=NOW)  # type: ignore[arg-type]
        with pytest.raises(guard.HoldoutBlocked):
            guard.verify_authorization(auth)  # type: ignore[arg-type]


def test_authorization_without_markers_or_pin_is_refused(tmp_path):
    o = af.authorize(tmp_path)
    _open(o)
    markers = guard.marker_paths(o.repo, home=o.home)
    pins = [m.parent / guard.PIN for m in markers]
    original = {p: p.read_bytes() for p in markers + pins}
    for victim in markers + pins:
        victim.unlink()  # marker (or pin) missing: the authorization object alone opens nothing
        with pytest.raises(guard.HoldoutBlocked):
            _open(o)
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
    o = af.authorize(tmp_path)
    assert guard.holdout_opened(o.repo, home=o.home)
    pin = guard.pinned_manifest(o.repo, home=o.home)
    assert pin == guard.verify_authorization(o.auth) == o.auth.record["pin"]
    assert pin["code_commit"] == af.HEAD_COMMIT and pin["manifest_sha256"] == af.MANIFEST
    assert pin["dev_selection_sha256"] == o.dev.sha256
    assert pin["leakage_report_sha256"] == o.report.sha256
    assert pin["final_model_hash"] == o.report.final_model_hash and pin["ml_star"] == ["A2", 72]
    assert pin["dev_ml_run_sha256"] == runner_select.runrecord.run_output_sha256(o.dev.runs[("A2", 72)])
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
    repo = af.frozen_repo(tmp_path / "repo")
    with af.verify_spec_off(), pytest.raises(guard.HoldoutBlocked, match="pre-opening record"):
        guard.preflight(repo)
    rec = af.pre_opening(tmp_path / "pre.json")
    with af.verify_spec_off(), pytest.raises(guard.HoldoutBlocked, match="manifest"):
        guard.preflight(repo, pre_opening=rec)
    with af.verify_spec_off():
        pf = guard.preflight(repo, pre_opening=rec, manifest_sha256=af.MANIFEST)
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
