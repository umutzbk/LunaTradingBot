"""The §15 forward model: a mechanical refit of ML* with NO re-selection, gated by the opening (spec §15
"Forward model", S-10.3, S-10.4, S-1.3, S-1.7, K9; DK-105, ruling R39; AMB-43).

* **What is reused** (§15): "the frozen ML* hyperparameters, and for LightGBM the frozen tree count ... the
  same calibrators as S-10.4". The input is the FINAL model of S-10.4 step 6 (``cv.final_model``) whose hash
  is pinned at the opening (``pin["final_model_hash"]``, ``leakage.model_hash``) for ML* (``pin["ml_star"]``):
  the regressor config and its early-stopped tree count (``Selection.cfg`` / ``best_iter``), every fixed
  classifier config with its tree count (``lgb_clf``: the T_sel early-stopped count of the final model), the
  linear calibrator of the regressor and the isotonic calibrator of every classifier. Nothing is selected or
  early-stopped here (``select_hparams`` / ``models.fit`` are never called; ``models.refit`` only).
* **Blocks** (``FORWARD_BLOCKS``): T_fit = [D0, 2026-01-01), T_cal = [2026-01-01, 2026-07-01) (the last 6
  months of W_HOLD). S-10.3 purge at the two forward block boundaries: a T_fit row needs
  ``label_known_at <= 2026-01-01 - 24 h``, a T_cal row ``label_known_at <= 2026-07-01 - 24 h`` (the T_cal end
  is the W_HOLD end and the M6 start; spec line "a decision whose label window would cross its window's end
  has no label"). §15 names no T_tr / T_sel split, so no internal boundary purges T_fit (AMB-43 (a)).
* **Gate:** these rows include W_HOLD rows: the verified hardened authorization of the canonical opening
  (``guard.verified_opening``, re-verified, never a boolean) is required, else ``HoldoutBlocked`` K9-logged.
  ``guard.verify_authorization`` refuses once the W_HOLD verdict marker exists ("after the verdict nothing
  opens"), so the forward refit is made inside the opening, before the verdict marker, and used only when the
  verdict is ``V7 HOLDOUT ADVANCE`` (``forward.forward_evaluate`` precondition; AMB-43 (b)). The data span of
  the input rows goes through ``guard.check_data_span``: M6 (= [2026-07-01, R_V6)) rows / labels are refused
  before the canonical R_V6 (``EmbargoBlocked``, K9-logged), W_FWD7 rows before L7.
* **Freeze (controller ruling R54, part of the W_HOLD freeze):** the samples are BOUND (``bound_samples``:
  the pinned W_DEV label panel of h* and the W_HOLD panels ``runner.label_panel`` issued under this opening);
  the refit is pinned once per opening by ``guard.pin_forward_model`` (write-once record + model blob next to
  the opening markers; an identical re-refit returns the record, a different one is refused, K9-logged);
  ``load_forward_model`` verifies the record and the blob (also after the verdict marker) and is the only way
  the forward replay gets the model (``ForwardScoreModel``).
"""

from __future__ import annotations

import hashlib
import pickle
import weakref
from collections.abc import Sequence

import numpy as np
import pandas as pd

from luna.v7 import guard, k9, leakage, models
from luna.v7.arms import families
from luna.v7.cv import (
    PURGE_NS,
    FoldModel,
    IsotonicCalibrator,
    LinearCalibrator,
    _Clf,
    _design,
    canonical,
    decision_dates,
)
from luna.v7.features import Preprocessor, features_for_horizon
from luna.v7.runner import FoldScoreModel, LabelPanel, holdout_panel_issued
from luna.v7.timeutil import ns_array, to_ns
from luna.v7.windows import D0, W_HOLD, Fold, final_blocks

T_FIT = (D0, to_ns("2026-01-01"))
T_CAL = (to_ns("2026-01-01"), to_ns("2026-07-01"))
FORWARD_BLOCKS = dict(T_fit=T_FIT, T_cal=T_CAL)
FORWARD_FOLD_ID = -1
# T_fit is one block (no selection block: ``sel`` is empty at the T_cal start); no test span (W_FWD7 is set
# by the canonical T_F7 later, ``ForwardScoreModel`` predicts any decision from the T_cal end on)
FORWARD_FOLD = Fold(FORWARD_FOLD_ID, T_CAL[1], T_CAL[1], T_FIT, (T_FIT[1], T_FIT[1]), T_CAL)
assert T_CAL[1] == W_HOLD[1], "T_cal is the last 6 months of W_HOLD (§15)"


def _refuse(exc: BaseException, request: dict) -> None:
    k9.log_refusal(exc, request=request, rule=guard.k9_rule(exc))


def forward_rows(samples: pd.DataFrame) -> dict[str, np.ndarray]:
    """Positional indices (into the canonical samples) of the purged forward blocks ``fit`` / ``cal``."""
    ts = ns_array(samples["decision_ts"], "decision_ts")
    lka = ns_array(samples["label_known_at"], "label_known_at")
    out = {}
    for k, (lo, hi) in (("fit", T_FIT), ("cal", T_CAL)):
        out[k] = np.flatnonzero((ts >= lo) & (ts < hi) & (lka <= hi - PURGE_NS))
    return out


def _frozen(final: object, pin: dict, horizon: int) -> tuple[str, str]:
    arm, h = pin["ml_star"][0], int(pin["ml_star"][1])
    reg, clf = families(arm)
    if int(horizon) != h or int(getattr(final, "horizon", -1)) != h:
        raise ValueError(f"the forward model is ML*'s at h* = {h}h (the pinned ML*)")
    if not isinstance(final, FoldModel) or final.fold != final_blocks():
        raise ValueError("the forward refit reuses the S-10.4 step-6 final model (cv.final_model)")
    if set(final.reg) != {reg} or reg not in final.selection or clf not in final.clf:
        raise ValueError(f"the final model is not ML*'s ({arm}: {reg} / {clf})")
    if leakage.model_hash(final) != pin["final_model_hash"]:
        raise ValueError("the final model is not the one pinned at the opening (final_model_hash): no "
                         "re-selection, no other configuration")
    s, r = final.selection[reg], final.reg[reg]
    if r.cfg != s.cfg or r.best_iteration != s.best_iter:
        raise ValueError("the final model's regressor is not its selected configuration")
    return reg, clf


def bound_samples(dev_panel: pd.DataFrame, holdout_panels: Sequence[LabelPanel], pin: dict) -> pd.DataFrame:
    """The forward refit's samples (ruling R54): the canonical concatenation of the W_DEV label panel of h*
    whose sha256 is pinned by the opening's LeakageReport (``panel_sha256s``, equal to the pre-opening
    record's ``dev_label_panels``) and the W_HOLD label panels issued by ``runner.label_panel`` under this
    opening. Any other frame, row subset or alteration is refused (HoldoutBlocked)."""
    h = int(pin["ml_star"][1])
    want = dict(_pinned_report(pin).panel_sha256s).get(h)
    if not isinstance(dev_panel, pd.DataFrame) or leakage.panel_sha256(dev_panel, h) != want:
        raise guard.HoldoutBlocked(f"the W_DEV samples are not the pinned label panel (h = {h})")
    hold = list(holdout_panels)
    if not hold:
        raise guard.HoldoutBlocked("the forward refit needs the W_HOLD label panels of the opening")
    for p in hold:
        if not holdout_panel_issued(p, pin["opening_id"]) or int(p.h) != h:
            raise guard.HoldoutBlocked("a W_HOLD panel was not issued by label_panel under this opening")
    return canonical(pd.concat([dev_panel, *(p.samples for p in hold)], ignore_index=True), h)


_AUTH_REPORT: dict[str, object] = {}


def _pinned_report(pin: dict):
    rep = _AUTH_REPORT.get(pin["opening_id"])
    if rep is None or getattr(rep, "sha256", None) != pin["leakage_report_sha256"]:
        raise guard.HoldoutBlocked("the opening's pinned LeakageReport is not available (authorization)")
    return rep


def _config(fm: FoldModel, reg: str) -> dict:
    def one(f) -> dict:
        return {"family": f.cfg.family, "params": sorted([k, v] for k, v in f.cfg.params.items()),
                "best_iteration": f.best_iteration}

    return {"regressor": one(fm.reg[reg]), "classifiers": {k: one(fm.clf[k].fitted) for k in sorted(fm.clf)}}


def forward_model(dev_panel: pd.DataFrame, holdout_panels: Sequence[LabelPanel], final: FoldModel, *,
                  auth: guard.HoldoutAuthorization | None) -> FoldModel:
    """The §15 forward refit of the pinned final model ``final`` on the purged T_fit / T_cal rows of the
    bound samples (``bound_samples``; h* of the pinned ML*), pinned once per opening
    (``guard.pin_forward_model``: a different second refit is refused, an identical one returns the existing
    record). Refused (K9-logged) without the verified opening, after the verdict marker, for unbound samples,
    M6 rows before R_V6 and W_FWD7 rows before L7. Returns the refitted ``cv.FoldModel`` on ``FORWARD_FOLD``
    (the replay loads it only through ``load_forward_model``)."""
    k9.require()
    req = {"kind": "forward_refit", "what": "the §15 forward refit (opening required, ruling R54)"}
    try:
        pin = guard.verified_opening(auth)
        _AUTH_REPORT[pin["opening_id"]] = auth.leakage_report  # type: ignore[union-attr]
        horizon = int(pin["ml_star"][1])
        reg, _ = _frozen(final, pin, horizon)
        s = bound_samples(dev_panel, holdout_panels, pin)
    except guard.HoldoutBlocked as e:
        _refuse(e, req)
        raise
    ts = ns_array(s["decision_ts"], "decision_ts")
    lka = ns_array(s["label_known_at"], "label_known_at")
    # the rows' data span (K9-logged refusals): M6 before R_V6, W_FWD7 before L7, W_HOLD with the opening
    guard.check_data_span(int(ts.min()), int(max(ts.max(), lka.max())) + 1, auth=auth,
                          what="forward refit rows (§15)")
    idx = forward_rows(s)
    if len(idx["fit"]) == 0 or len(idx["cal"]) == 0:
        raise ValueError("empty T_fit or T_cal after the purge")
    cols = features_for_horizon(horizon)
    fit_df, cal = s.iloc[idx["fit"]], s.iloc[idx["cal"]]
    pre = Preprocessor(cols).fit(fit_df, ns_array(fit_df["decision_ts"], "decision_ts"), FORWARD_FOLD)
    cal_dates = decision_dates(cal["decision_ts"])

    sel = final.selection[reg]
    r = models.refit(sel.cfg, sel.best_iter, _design(sel.cfg, fit_df, cols, pre), fit_df["y_reg"].to_numpy())
    lin = LinearCalibrator().fit(r.predict(_design(sel.cfg, cal, cols, pre)), cal["y_reg"], cal_dates)
    clfs: dict[str, _Clf] = {}
    for fam in sorted(final.clf):
        c = final.clf[fam].fitted
        rc = models.refit(c.cfg, c.best_iteration if fam == "lgb_clf" else None,
                          _design(c.cfg, fit_df, cols, pre), fit_df["y_pos"].to_numpy())
        iso = IsotonicCalibrator().fit(rc.predict(_design(c.cfg, cal, cols, pre)), cal["y_pos"], cal_dates)
        clfs[fam] = _Clf(rc, iso)
    fm = FoldModel(FORWARD_FOLD, horizon, {reg: sel}, {reg: r}, {reg: lin}, clfs, pre, cols)
    blob = pickle.dumps(fm, protocol=4)  # the serialisation of leakage.model_hash
    body = {"kind": guard.FORWARD_KIND, "run_id": guard.RUN_ID, "opening_id": pin["opening_id"],
            "code_commit": pin["code_commit"], "ml_star": pin["ml_star"], "horizon": horizon,
            "forward_model_hash": hashlib.sha256(blob).hexdigest(),
            "samples_sha256": leakage.panel_sha256(s, horizon), "final_model_hash": pin["final_model_hash"],
            "config": _config(fm, reg)}
    try:
        guard.pin_forward_model(auth, body, blob)
    except guard.HoldoutBlocked as e:
        _refuse(e, req)
        raise
    return fm


def forward_model_hash(fm: FoldModel) -> str:
    """The forward model's identity (``leakage.model_hash``), recorded in the write-once record (§15)."""
    return leakage.model_hash(fm)


_LOADED: weakref.WeakValueDictionary = weakref.WeakValueDictionary()
_LOADED_SHA: dict[int, str] = {}  # id of a loaded model -> the record's forward_model_hash (its blob sha256)


def load_forward_model() -> FoldModel:
    """The frozen forward model of the canonical opening (ruling R54): the write-once record verified
    (``guard.forward_model_record``: bound to the opening, untampered), the blob hashing to its
    ``forward_model_hash``, the unpickled model on ``FORWARD_FOLD`` at the record's h*. Works after the W_HOLD
    verdict (it reads no W_HOLD data). HoldoutBlocked otherwise."""
    rec = guard.forward_model_record()
    if rec is None:
        raise guard.HoldoutBlocked("no frozen forward model for the canonical opening")
    fm = pickle.loads(guard.forward_model_blob(rec))  # own pinned, hash-checked blob
    if not isinstance(fm, FoldModel) or fm.fold != FORWARD_FOLD or fm.horizon != int(rec["horizon"]):
        raise guard.HoldoutBlocked("the pinned blob is not a forward model of the record")
    _LOADED[id(fm)] = fm
    _LOADED_SHA[id(fm)] = rec["forward_model_hash"]
    return fm


def loaded(fm: object) -> bool:
    """True iff ``fm`` came from ``load_forward_model`` in this process."""
    return _LOADED.get(id(fm)) is fm


def loaded_hash(fm: object) -> str | None:
    """The record's ``forward_model_hash`` a loaded model was verified against (its blob's sha256; a
    re-pickle of an unpickled model need not be byte-identical), else None."""
    return _LOADED_SHA.get(id(fm)) if loaded(fm) else None


class ForwardScoreModel(FoldScoreModel):
    """``engine.ScoreModel`` of the forward replay: the ONE frozen forward model (``load_forward_model`` only)
    predicts every decision from the T_cal end on (W_FWD7 lies after it); earlier decisions are refused."""

    def __init__(self, fm: FoldModel, ml_star: str) -> None:
        if not loaded(fm):
            raise ValueError("the forward replay takes the model only through load_forward_model (R54)")
        super().__init__([fm], ml_star, fm.horizon)

    def fold_index(self, t: int) -> int:
        if int(t) < FORWARD_FOLD.test_lo:
            raise ValueError(f"decision {t} precedes the forward model's validity (T_cal end)")
        return 0


__all__ = ["FORWARD_BLOCKS", "FORWARD_FOLD", "FORWARD_FOLD_ID", "ForwardScoreModel", "T_CAL", "T_FIT",
           "bound_samples", "forward_model", "forward_model_hash", "forward_rows", "load_forward_model",
           "loaded", "loaded_hash"]
