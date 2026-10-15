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
* **Hash:** ``forward_model_hash`` (= ``leakage.model_hash``) is the identity to freeze before T_F7.
"""

from __future__ import annotations

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
from luna.v7.runner import FoldScoreModel
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


def forward_model(samples: pd.DataFrame, final: FoldModel, *,
                  auth: guard.HoldoutAuthorization | None) -> FoldModel:
    """The §15 forward refit of the pinned final model ``final`` on the purged T_fit / T_cal rows of
    ``samples`` (horizon h* of the pinned ML*). Refused (K9-logged) without the verified opening, after the
    verdict marker, for M6 rows before R_V6 and W_FWD7 rows before L7. Returns a ``cv.FoldModel`` on
    ``FORWARD_FOLD`` with the final model's ``selection`` (provenance) and refitted models / calibrators."""
    k9.require()
    try:
        pin = guard.verified_opening(auth)
    except guard.HoldoutBlocked as e:
        _refuse(e, {"kind": "forward_refit", "what": "W_HOLD rows of the forward refit (opening required)"})
        raise
    horizon = int(pin["ml_star"][1])
    reg, clf = _frozen(final, pin, horizon)
    s = canonical(samples, horizon)
    if len(s) == 0:
        raise ValueError("no samples for the forward refit")
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
    return FoldModel(FORWARD_FOLD, horizon, {reg: sel}, {reg: r}, {reg: lin}, clfs, pre, cols)


def forward_model_hash(fm: FoldModel) -> str:
    """The forward model's identity (``leakage.model_hash``): hashed and frozen before T_F7 (§15)."""
    return leakage.model_hash(fm)


class ForwardScoreModel(FoldScoreModel):
    """``engine.ScoreModel`` of the forward replay: the ONE forward model predicts every decision from the
    T_cal end on (W_FWD7 lies after it); earlier decisions are refused."""

    def __init__(self, fm: FoldModel, ml_star: str) -> None:
        if fm.fold != FORWARD_FOLD:
            raise ValueError("not a forward model (forward_model)")
        super().__init__([fm], ml_star, fm.horizon)

    def fold_index(self, t: int) -> int:
        if int(t) < FORWARD_FOLD.test_lo:
            raise ValueError(f"decision {t} precedes the forward model's validity (T_cal end)")
        return 0


__all__ = ["FORWARD_BLOCKS", "FORWARD_FOLD", "FORWARD_FOLD_ID", "ForwardScoreModel", "T_CAL", "T_FIT",
           "forward_model", "forward_model_hash", "forward_rows"]
