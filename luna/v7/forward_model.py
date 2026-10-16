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
  the forward replay gets the model (``ForwardScoreModel``). The W_HOLD panels must together cover exactly
  the canonical W_HOLD decision grid of h* (``windows.decision_times(*W_HOLD)``), with window = W_HOLD, no
  overlap, over the universe ``runner_select.holdout_inputs`` used for this opening (round 3). The record
  carries the library versions (numpy, pandas, pyarrow, scikit-learn, lightgbm, Python).
* **Bridge panel (controller ruling R57; red-team r2 m2; AMB-43 (a) RESOLVED):** the pinned W_DEV panel has
  no label for the W_DEV decisions whose label window crosses the W_DEV end (S-1.5, ``labels.leg_label``:
  t + 15 min + h* >= W_DEV end; ``bridge_decisions``: 3 / 9 / 21 decisions at h* = 24 / 72 / 168 h). The
  forward samples are three parts: the pinned W_DEV panel (unchanged), the BRIDGE panel(s) issued by
  ``runner.label_panel`` under the same opening for exactly those decisions with the label window
  ``BRIDGE_WINDOW`` = the forward T_fit [D0, 2026-01-01) (it extends into W_HOLD; W_HOLD outcomes are
  legitimate after the opening), and the W_HOLD panels. The round-3 coverage check covers the bridge too
  (opening, h*, label window, the holdout universe, no decision issued twice, the union exactly
  ``bridge_decisions(h*)``); no pinned W_DEV sample may lie on a bridge or W_HOLD decision. Together they
  cover exactly the canonical decision grid of [D0, W_HOLD end) at h*, without overlap (the pinned W_DEV
  panel covers the rest of the W_DEV grid by construction: its labels were computed with window = W_DEV).
  The purge is unchanged (``forward_rows``).
* **Runbook:** the panel registry and the holdout universe are per process: run ``label_panel`` (W_HOLD, h*,
  full grid), ``holdout_inputs`` and ``forward_model`` in the SAME process, before ``holdout_verdict``. After
  a crash, resume the same opening (``guard.resume_holdout``, same ``opening_id``), regenerate the panels and
  the holdout inputs, then refit: an identical refit returns the existing record.
"""

from __future__ import annotations

import hashlib
import weakref
from collections.abc import Sequence

import numpy as np
import pandas as pd

from luna.v7 import guard, k9, leakage, modelcodec, models
from luna.v7.arms import families
from luna.v7.constants import FROZEN
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
from luna.v7.runner import FoldScoreModel, LabelPanel, holdout_panel_coverage, holdout_universe
from luna.v7.timeutil import MIN, H, ns_array, to_ns
from luna.v7.viewstore import library_versions
from luna.v7.windows import D0, W_DEV, W_HOLD, Fold, decision_times, final_blocks

T_FIT = (D0, to_ns("2026-01-01"))
T_CAL = (to_ns("2026-01-01"), to_ns("2026-07-01"))
FORWARD_BLOCKS = dict(T_fit=T_FIT, T_cal=T_CAL)
FORWARD_FOLD_ID = -1
# T_fit is one block (no selection block: ``sel`` is empty at the T_cal start); no test span (W_FWD7 is set
# by the canonical T_F7 later, ``ForwardScoreModel`` predicts any decision from the T_cal end on)
FORWARD_FOLD = Fold(FORWARD_FOLD_ID, T_CAL[1], T_CAL[1], T_FIT, (T_FIT[1], T_FIT[1]), T_CAL)
assert T_CAL[1] == W_HOLD[1], "T_cal is the last 6 months of W_HOLD (§15)"
# R57: the label window of the bridge panel (the forward T_fit; it extends into W_HOLD)
BRIDGE_WINDOW = T_FIT


def bridge_decisions(h: int) -> tuple[int, ...]:
    """R57: the canonical W_DEV decisions (S-8.1 grid) without a label in the pinned W_DEV panel of horizon
    ``h`` because their label window crosses the W_DEV end (S-1.5; ``labels.leg_label``: no label when
    t + 15 min + h >= the window end)."""
    fill = FROZEN.decision.fill_delay_min * MIN
    return tuple(int(t) for t in decision_times(*W_DEV) if int(t) + fill + int(h) * H >= W_DEV[1])


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
    """The forward refit's samples (rulings R54 / R57): the canonical concatenation of the W_DEV label panel
    of h* whose sha256 is pinned by the opening's LeakageReport (``panel_sha256s``, equal to the pre-opening
    record's ``dev_label_panels``), the bridge panel(s) and the W_HOLD label panels issued by
    ``runner.label_panel`` under this opening (``holdout_panels`` holds both; each is told apart by its
    issued label window: ``BRIDGE_WINDOW`` or W_HOLD). Any other frame, row subset, alteration, label window
    or coverage is refused (HoldoutBlocked)."""
    h = int(pin["ml_star"][1])
    want = dict(_pinned_report(pin).panel_sha256s).get(h)
    if not isinstance(dev_panel, pd.DataFrame) or leakage.panel_sha256(dev_panel, h) != want:
        raise guard.HoldoutBlocked(f"the W_DEV samples are not the pinned label panel (h = {h})")
    parts: dict[str, list[tuple]] = {"bridge": [], "W_HOLD": []}  # (coverage, panel)
    for p in holdout_panels:
        cov = holdout_panel_coverage(p)
        if cov is None or cov.opening_id != pin["opening_id"]:
            raise guard.HoldoutBlocked("a W_HOLD / bridge panel was not issued by label_panel under this "
                                       "opening")
        if cov.h != h or int(p.h) != h:
            raise guard.HoldoutBlocked(f"a W_HOLD / bridge panel is not for h* = {h}h")
        if cov.window == tuple(W_HOLD):
            parts["W_HOLD"].append((cov, p))
        elif cov.window == tuple(BRIDGE_WINDOW):
            parts["bridge"].append((cov, p))
        else:
            raise guard.HoldoutBlocked(f"a panel's label window {cov.window} is neither W_HOLD {W_HOLD} nor "
                                       f"the bridge window {BRIDGE_WINDOW} (R57)")
    if not parts["W_HOLD"]:
        raise guard.HoldoutBlocked("the forward refit needs the W_HOLD label panels of the opening")
    if not parts["bridge"]:
        raise guard.HoldoutBlocked("the forward refit needs the bridge panel of the opening (R57: the W_DEV "
                                   "decisions whose labels cross the W_DEV end)")
    universe = holdout_universe(pin["opening_id"])
    if universe is None or any(c.universe != universe for v in parts.values() for c, _ in v):
        raise guard.HoldoutBlocked("the W_HOLD / bridge panels are not over the universe holdout_inputs used "
                                   "for this opening")
    seen: set[int] = set()
    for c, _ in parts["bridge"] + parts["W_HOLD"]:
        if seen & set(c.decisions):
            raise guard.HoldoutBlocked("the W_HOLD / bridge panels overlap (a decision time issued twice)")
        seen |= set(c.decisions)
    grids = {"W_HOLD": {int(t) for t in decision_times(*W_HOLD)},  # the S-8.1 grid of dev_selection / books
             "bridge": set(bridge_decisions(h))}
    for part, grid in grids.items():
        got = {int(t) for c, _ in parts[part] for t in c.decisions}
        if got != grid:
            raise guard.HoldoutBlocked(f"the {part} panels cover {len(got & grid)} of the {len(grid)} "
                                       f"canonical {part} decisions ({len(got - grid)} off-grid)")
    dev_ts = set(ns_array(dev_panel["decision_ts"], "decision_ts").tolist()) if len(dev_panel) else set()
    if dev_ts & seen:
        raise guard.HoldoutBlocked("the pinned W_DEV panel overlaps the bridge / W_HOLD decisions")
    frames = [p.samples for _, p in parts["bridge"] + parts["W_HOLD"]]
    return canonical(pd.concat([dev_panel, *frames], ignore_index=True), h)


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
    blob = modelcodec.encode(fm)  # canonical, non-executable (no pickle; Task 26a)
    body = {"kind": guard.FORWARD_KIND, "run_id": guard.RUN_ID, "opening_id": pin["opening_id"],
            "code_commit": pin["code_commit"], "ml_star": pin["ml_star"], "horizon": horizon,
            "forward_model_hash": hashlib.sha256(blob).hexdigest(),
            "samples_sha256": leakage.panel_sha256(s, horizon), "final_model_hash": pin["final_model_hash"],
            "config": _config(fm, reg), "libraries": library_versions()}
    try:
        guard.pin_forward_model(auth, body, blob)
    except guard.HoldoutBlocked as e:
        _refuse(e, req)
        raise
    return fm


def forward_model_hash(fm: FoldModel) -> str:
    """The forward model's identity: sha256 of its canonical ``modelcodec`` bytes, recorded in the write-once
    record (§15)."""
    return modelcodec.model_sha256(fm)


_LOADED: weakref.WeakValueDictionary = weakref.WeakValueDictionary()
_LOADED_SHA: dict[int, str] = {}  # id of a loaded model -> the record's forward_model_hash (its blob sha256)


def load_forward_model() -> FoldModel:
    """The frozen forward model of the canonical opening (ruling R54): the write-once record verified
    (``guard.forward_model_record``: bound to the opening, untampered), the blob hashing to its
    ``forward_model_hash``, the decoded model (``modelcodec``, no unpickling) on ``FORWARD_FOLD`` at the
    record's h*. Works after the W_HOLD verdict (it reads no W_HOLD data). HoldoutBlocked otherwise."""
    rec = guard.forward_model_record()
    if rec is None:
        raise guard.HoldoutBlocked("no frozen forward model for the canonical opening")
    try:
        fm = modelcodec.decode(guard.forward_model_blob(rec))
    except modelcodec.CodecError as e:
        raise guard.HoldoutBlocked(f"the pinned forward-model document does not decode: {e}") from None
    if not isinstance(fm, FoldModel) or fm.fold != FORWARD_FOLD or fm.horizon != int(rec["horizon"]):
        raise guard.HoldoutBlocked("the pinned blob is not a forward model of the record")
    _LOADED[id(fm)] = fm
    _LOADED_SHA[id(fm)] = rec["forward_model_hash"]
    return fm


def loaded(fm: object) -> bool:
    """True iff ``fm`` came from ``load_forward_model`` in this process."""
    return _LOADED.get(id(fm)) is fm


def loaded_hash(fm: object) -> str | None:
    """The record's ``forward_model_hash`` a loaded model was verified against (== ``forward_model_hash`` of
    it: the format is canonical), else None."""
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


__all__ = ["BRIDGE_WINDOW", "FORWARD_BLOCKS", "FORWARD_FOLD", "FORWARD_FOLD_ID", "ForwardScoreModel", "T_CAL",
           "T_FIT", "bound_samples", "bridge_decisions", "forward_model", "forward_model_hash",
           "forward_rows", "load_forward_model", "loaded", "loaded_hash"]
