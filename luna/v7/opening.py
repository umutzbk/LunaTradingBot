"""The pre-opening record: the persisted DevSelection and LeakageReport the holdout opening is checked
against, and its fail-closed re-verification in a later process (spec DEC-19, S-10.7, L14, §17; SC-05,
red-team M4, Task 23 review I-1 / M-5; AMB-34).

DEC-19: the holdout opens "only after code freeze + all leakage / canary checks passed + the S-10.7
development gate passed + pinned manifest / guards ready + the explicit user approval phrase; one opening
only". ``leakage.verify_report`` and ``runner_select.verify_dev_selection`` are process-local (AMB-33 (e)),
and the opening normally runs in a later process, so the producing run persists both:

* ``write_pre_opening_record(path, dev, report, samples)`` (the producing run): the DevSelection must verify
  (issued by ``runner_select.dev_selection``), the LeakageReport must verify (issued by
  ``leakage.run_leakage_checks`` / ``resolve_k7``); the record is checked exactly as the opening will check it
  (below, except HEAD) and written by exclusive create as canonical JSON:
  ``{kind, run_id, spec_sha256, code_commit, dev_selection, dev_selection_sha256, dev_label_panels,
  leakage_report, leakage_report_sha256, record_sha256}``. ``dev_label_panels`` are the sha256 of the W_DEV
  label panels (``leakage.panel_sha256``) the fold models / canaries used.
* ``load_pre_opening(path, head=...)`` (called by ``guard.preflight`` / ``guard.authorize_holdout``):
  reloads the file; recomputes the DevSelection record hash, the LeakageReport content hash and the record
  hash and checks each against the recorded value; rebuilds the LeakageReport
  (``leakage._load_persisted_report``, private to this flow) and re-derives ``passed`` from the content
  (every check executed and
  passed, every evaluated K7 trigger resolved "explained", clean tree, a final-model hash); checks the S-10.7
  gate from the content (an S-10.6 choice, an EMPTY kill list); the DevSelection <-> report link (ML*, the
  fold-model hashes of ML*'s family at every horizon, the label-panel hashes, M-5); and the code commit of
  the record and of the report == HEAD (the ``v7-code-frozen`` commit, checked by preflight).
  Any failure raises ``guard.HoldoutBlocked``.

A guard against accidental or premature runs, not a cryptographic boundary: a record rewritten together with
all of its hashes passes the hash checks; it must still pass every semantic check above.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from luna.v7 import leakage, runner_select
from luna.v7.guard import RUN_ID, HoldoutBlocked
from luna.v7.runrecord import canonical_json
from luna.v7.spec import SPEC_BLOB_SHA256

KIND = "V7_PRE_OPENING"
FIELDS = ("kind", "run_id", "spec_sha256", "code_commit", "dev_selection", "dev_selection_sha256",
          "dev_label_panels", "leakage_report", "leakage_report_sha256", "record_sha256")
_SHA = re.compile(r"\A[0-9a-f]{64}\Z")


@dataclass(frozen=True)
class PreOpening:
    """A re-verified pre-opening record (``load_pre_opening``)."""

    record_sha256: str
    code_commit: str
    dev_selection: dict  # the persisted DevSelection record (``runner_select.dev_selection_record``)
    dev_selection_sha256: str
    leakage_report: leakage.LeakageReport  # rebuilt and registered in this process (verifies, passed)

    @property
    def ml_star(self) -> tuple[str, int]:
        return self.leakage_report.ml_star

    @property
    def dev_ml_run_sha256(self) -> str:
        key = [self.ml_star[0], int(self.ml_star[1])]
        return next(r["output_sha256"] for r in self.dev_selection["runs"] if r["key"] == key)


def content_sha256(obj: object) -> str:
    """sha256 of the canonical JSON (sorted keys, compact, no NaN) of a JSON-plain object."""
    return hashlib.sha256(canonical_json(obj).encode()).hexdigest()


def _semantics(rec: Mapping, rep: leakage.LeakageReport) -> None:
    """The checks shared by the writer and the loader (ValueError on any failure)."""
    dev = rec["dev_selection"]
    if not isinstance(dev, dict) or dev.get("kind") != "DevSelection":
        raise ValueError("dev_selection: not a DevSelection record")
    res = dev.get("result")
    if not isinstance(res, dict) or res.get("type") != "choice" or dev.get("ml_star") is None:
        raise ValueError("S-10.6: the DevSelection holds no valid choice (no ML*): the holdout stays sealed")
    if dev.get("kill") != []:
        raise ValueError(f"S-10.7 development kill {dev.get('kill')!r}: the holdout is never opened "
                         "(K3, DEC-19)")
    arm, h = dev["ml_star"]
    if [res.get("arm"), res.get("h")] != [arm, h] or rep.ml_star != (arm, int(h)):
        raise ValueError(f"the LeakageReport is for ML* = {rep.ml_star}, the DevSelection's ML* is "
                         f"{(arm, h)}")
    if rep.dirty:
        raise ValueError("the LeakageReport was produced from a dirty working tree")
    if not rep.passed:
        raise ValueError(f"the LeakageReport did not pass (DEC-19): {'; '.join(rep.blocking)}")
    if not isinstance(rep.final_model_hash, str) or not _SHA.match(rep.final_model_hash):
        raise ValueError("the LeakageReport audited no final model")
    if arm not in ("A2", "A3"):
        raise ValueError(f"ML* {arm!r} is not A2 / A3")
    dev_folds = {int(hh): tuple(hs) for a, hh, hs in dev["fold_model_hashes"] if a == arm}
    if dict(rep.fold_model_hashes) != dev_folds:
        raise ValueError("the LeakageReport was not run on this DevSelection's fold models")
    panels = {int(k): v for k, v in rec["dev_label_panels"].items()}
    if dict(rep.panel_sha256s) != panels:
        raise ValueError("the LeakageReport's label-panel hashes are not the DevSelection's (M-5)")
    if rec["code_commit"] != rep.commit:
        raise ValueError("the record's code commit is not the LeakageReport's")
    key = [arm, int(h)]
    if sum(1 for r in dev["runs"] if r.get("key") == key) != 1:
        raise ValueError("the DevSelection record holds no ML* run")


def build_pre_opening_record(dev: runner_select.DevSelection, report: leakage.LeakageReport,
                             samples: Mapping[int, pd.DataFrame]) -> dict:
    """The record ``write_pre_opening_record`` writes (checked; see the module docstring)."""
    if not runner_select.verify_dev_selection(dev):
        raise ValueError("a DevSelection issued by runner_select.dev_selection (unaltered) is required")
    if not isinstance(report, leakage.LeakageReport) or not leakage.verify_report(report):
        raise ValueError("a LeakageReport issued by leakage.run_leakage_checks (unaltered) is required")
    body = {
        "kind": KIND, "run_id": RUN_ID, "spec_sha256": SPEC_BLOB_SHA256, "code_commit": report.commit,
        "dev_selection": runner_select.dev_selection_record(dev), "dev_selection_sha256": dev.sha256,
        "dev_label_panels": {str(int(h)): leakage.panel_sha256(s, int(h))
                             for h, s in sorted(samples.items())},
        "leakage_report": leakage.report_content(report), "leakage_report_sha256": report.sha256,
    }
    _semantics(body, report)
    body["record_sha256"] = content_sha256(body)
    return body


def write_pre_opening_record(path: str | Path, dev: runner_select.DevSelection,
                             report: leakage.LeakageReport, samples: Mapping[int, pd.DataFrame]) -> dict:
    """Persist the pre-opening record (exclusive create, canonical JSON); returns it."""
    rec = build_pre_opening_record(dev, report, samples)
    fd = os.open(Path(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
        f.write(canonical_json(rec) + "\n")
    return rec


def _load(path: str | Path, head: str) -> PreOpening:
    try:
        rec = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise ValueError(f"pre-opening record unreadable: {e}") from None
    if not isinstance(rec, dict) or set(rec) != set(FIELDS):
        raise ValueError("pre-opening record: unexpected fields")
    if rec["kind"] != KIND or rec["run_id"] != RUN_ID or rec["spec_sha256"] != SPEC_BLOB_SHA256:
        raise ValueError("pre-opening record: not this V7 run (kind / run_id / spec)")
    if rec["record_sha256"] != content_sha256({k: v for k, v in rec.items() if k != "record_sha256"}):
        raise ValueError("pre-opening record: record hash mismatch (tampered)")
    if content_sha256(rec["dev_selection"]) != rec["dev_selection_sha256"]:
        raise ValueError("pre-opening record: DevSelection hash mismatch (tampered)")
    if not isinstance(rec["leakage_report"], dict) or \
            content_sha256(rec["leakage_report"]) != rec["leakage_report_sha256"]:
        raise ValueError("pre-opening record: LeakageReport hash mismatch (tampered)")
    if not isinstance(rec["dev_label_panels"], dict):
        raise ValueError("pre-opening record: dev_label_panels")
    rep = leakage._load_persisted_report(rec["leakage_report"], rec["leakage_report_sha256"])
    _semantics(rec, rep)
    if rec["code_commit"] != head or rep.commit != head:
        raise ValueError(f"the records were produced at {rec['code_commit']}, not at HEAD {head} "
                         "(code freeze)")
    return PreOpening(rec["record_sha256"], rec["code_commit"], rec["dev_selection"],
                      rec["dev_selection_sha256"], rep)


def load_pre_opening(path: str | Path | None, *, head: str) -> PreOpening:
    """Reload and re-verify the persisted record (module docstring); ``guard.HoldoutBlocked`` on any
    failure."""
    if path is None:
        raise HoldoutBlocked("holdout opening refused: the pre-opening record (DevSelection + LeakageReport) "
                             "is required (DEC-19)")
    try:
        return _load(path, head)
    except (ValueError, TypeError, KeyError, StopIteration) as e:
        raise HoldoutBlocked(f"holdout opening refused: pre-opening record {path}: {e}") from None


__all__ = ["FIELDS", "KIND", "PreOpening", "build_pre_opening_record", "content_sha256", "load_pre_opening",
           "write_pre_opening_record"]
