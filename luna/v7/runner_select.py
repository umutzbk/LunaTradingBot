"""Research runner, part 2: the S-10.6 / S-10.7 driver and the holdout-input assembly (spec S-10.6, S-10.7,
S-13.2 / S-13.4 inputs, K3, K8, K11, EC-2, DEC-19; AMB-27). Orchestration only (see ``luna.v7.runner``).

* ``dev_selection``: A0 at 24 / 72 / 168 h and A2 / A3 at every horizon, ONE continuous E_T / L-norm book
  each over the concatenated outer test quarters (each decision scored by its own quarter's fold model,
  ``runner.FoldScoreModel``; positions carry across quarter boundaries; the terminal unwind is the window
  end - 1 min, before the W_DEV end). Then ``selection.choose`` (validity precondition, EC-2) and, only when
  it holds, ``selection.dev_kill`` on ML* with the per-quarter nets; the secondary E_M runs of ML* and A0
  at h* follow.
* ``holdout_inputs``: ``verdict.HoldoutInputs`` from already-executed runs, the final model's predictions on
  the holdout label panel and the W_DEV facts. No data access; refuses without a
  ``guard.HoldoutAuthorization`` and after an S-10.7 kill (DEC-19).
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import numpy as np

from luna.v7 import cv, guard, selection, stats
from luna.v7.accounting import account, mean_daily_net, period_net
from luna.v7.arms import families
from luna.v7.book.engine import BookRun
from luna.v7.constants import FROZEN
from luna.v7.runner import (
    HORIZONS,
    DecisionProvider,
    FoldScoreModel,
    LabelPanel,
    ResearchUniverse,
    fold_convergence,
    run_book,
)
from luna.v7.verdict import HoldoutInputs, K8Rates
from luna.v7.windows import W_DEV


@dataclass(frozen=True)
class DevSelection:
    """S-10.6 / S-10.7 result. ``result``: ``selection.Choice`` or ``selection.Invalid``. ``kill`` and
    ``fold_nets`` are None when the validity precondition failed (S-10.7 runs only after it holds)."""

    window: tuple[int, int]
    quarter_edges: tuple[int, ...]
    runs: dict[tuple[str, int], BookRun]
    models: dict[tuple[str, int], FoldScoreModel]
    result: selection.Choice | selection.Invalid
    kill: tuple[str, ...] | None
    fold_nets: tuple[float, ...] | None
    em_runs: dict[str, BookRun]
    convergence: tuple[dict, ...]

    @property
    def ml_star(self) -> tuple[str, int] | None:
        r = self.result
        return (r.arm, r.h) if isinstance(r, selection.Choice) else None


def quarters(fold_models: Mapping[int, Sequence]) -> tuple[tuple[int, int], ...]:
    """The outer test quarters, identical for every horizon and consecutive (one continuous book)."""
    if set(fold_models) != set(HORIZONS):
        raise ValueError(f"fold models are required for every horizon {HORIZONS}")
    qs = {h: tuple(sorted((int(m.fold.test_lo), int(m.fold.test_hi)) for m in fold_models[h]))
          for h in HORIZONS}
    q = qs[HORIZONS[0]]
    if not q or any(v != q for v in qs.values()):
        raise ValueError("every horizon needs the same outer test quarters")
    for (_, a_hi), (b_lo, _) in zip(q, q[1:], strict=False):
        if a_hi != b_lo:
            raise ValueError("the outer test quarters must be consecutive (one continuous book, S-10.6)")
    return q


def dev_selection(u: ResearchUniverse, provider: DecisionProvider, fold_models: Mapping[int, Sequence],
                  now_ns: int, *, reference_runs: Mapping[tuple[str, int], BookRun] | None = None,
                  B: int = FROZEN.stats.B, seed: int = stats.SEED, em: bool = True) -> DevSelection:
    """S-10.6 then S-10.7 (see module docstring). Reference arms (A0-V5, TS) are not engine arms: runs the
    caller supplies are passed through to ``selection.choose`` (reported only; AMB-27 (a))."""
    q = quarters(fold_models)
    window = (q[0][0], q[-1][1])
    if not (W_DEV[0] <= window[0] and window[1] <= W_DEV[1]):
        raise ValueError("S-10.6 runs on W_DEV only (terminal unwind before the W_DEV end)")
    ref = dict(reference_runs or {})
    bad = [k for k in ref if k[0] not in selection.REFERENCE_ARMS]
    if bad:
        raise ValueError(f"reference runs must be of {selection.REFERENCE_ARMS}: {bad}")
    edges = tuple([lo for lo, _ in q] + [window[1]])
    models = {(a, h): FoldScoreModel(fold_models[h], a, h) for a in selection.ML_ARMS for h in HORIZONS}
    runs: dict[tuple[str, int], BookRun] = {}
    for h in HORIZONS:
        runs[("A0", h)] = run_book("A0", u, provider, window, h, now_ns)
    for a in selection.ML_ARMS:
        for h in HORIZONS:
            runs[(a, h)] = run_book(a, u, provider, window, h, now_ns, model=models[(a, h)])
    has_conv = all(hasattr(m, "convergence") for s in fold_models.values() for m in s)
    conv = fold_convergence(fold_models) if has_conv else ()
    res = selection.choose({**runs, **ref}, B=B, seed=seed)
    if isinstance(res, selection.Invalid):
        return DevSelection(window, edges, {**runs, **ref}, models, res, None, None, {}, conv)
    key = (res.arm, res.h)
    led = account(runs[key])
    nets = tuple(period_net(led, list(edges)))
    kill = tuple(selection.dev_kill(led, nets))
    em_runs: dict[str, BookRun] = {}
    if em:
        em_runs["ML*"] = run_book(res.arm, u, provider, window, res.h, now_ns, model=models[key],
                                  execution="E_M")
        em_runs["A0"] = run_book("A0", u, provider, window, res.h, now_ns, execution="E_M")
    return DevSelection(window, edges, {**runs, **ref}, models, res, kill, nets, em_runs, conv)


def missing_funding_rate(runs: Sequence[BookRun]) -> float:
    """K8 missing-funding share of the given runs (AMB-27 (b)): settlements of held positions whose funding
    was not booked (``integrity`` entries ``funding_invalid:<pid>@<ts>`` / ``mark_missing:<pid>@<ts>``, one
    per settlement) plus closed episodes with a DEC-29 missing settlement (``funding_missing:<pid>``, once
    each: a lower bound), over the booked settlements (``funding_flows`` rows) plus those. 0 when nothing
    was due."""
    miss = booked = 0
    for r in runs:
        booked += len(r.funding_flows)
        for s in r.integrity:
            per_settlement = s.startswith(("funding_invalid:", "mark_missing:")) and "@" in s
            if per_settlement or s.startswith("funding_missing:"):
                miss += 1
    den = booked + miss
    return miss / den if den else 0.0


def holdout_inputs(*, auth: guard.HoldoutAuthorization, ml_T: BookRun, a0_T: BookRun,  # noqa: N803
                   ml_M: BookRun, a0_M: BookRun, quarter_edges: Sequence[int],  # noqa: N803
                   panel: LabelPanel, final_model: object, leakage: Sequence[str], integrity: Sequence[str],
                   dev: DevSelection | None = None, dev_ml_run: BookRun | None = None,
                   dev_kill: Sequence[str] | None = None, secondary: Mapping | None = None,
                   B: int = stats.B_DEFAULT, seed: int = stats.SEED) -> HoldoutInputs:
    """``verdict.HoldoutInputs`` (see module docstring). W_DEV facts come from ``dev`` or, for plumbing on
    synthetic windows, from ``dev_ml_run`` + ``dev_kill``. ``final_model`` (``cv.final_model``, or any model
    with ``predict(df)``) predicts the canonical panel: P7 ``ic`` = (ŷ, y_reg, dates), P8 ``calib`` = (ŷ_cal,
    y_reg, dates), the A1 calibration = (P̂_cal, y_pos, dates); K8 = (the panel's invalid-label rate,
    ``missing_funding_rate`` of the required E_T runs)."""
    if not isinstance(auth, guard.HoldoutAuthorization):
        raise guard.HoldoutBlocked("holdout inputs need the user's HoldoutAuthorization (DEC-19)")
    if dev is not None:
        if dev.ml_star is None or dev.kill is None:
            raise ValueError("the S-10.6 validity precondition failed: no ML* (the holdout stays sealed)")
        arm, h = dev.ml_star
        dev_run, kill = dev.runs[dev.ml_star], dev.kill
    else:
        if dev_ml_run is None or dev_kill is None:
            raise ValueError("W_DEV facts missing: pass dev or (dev_ml_run, dev_kill)")
        arm, h, dev_run, kill = dev_ml_run.arm, dev_ml_run.h_star, dev_ml_run, tuple(dev_kill)
    if kill:
        raise ValueError("S-10.7 development kill: the holdout is never opened (K3, DEC-19)")
    for r, a, e in ((ml_T, arm, "E_T"), (a0_T, "A0", "E_T"), (ml_M, arm, "E_M"), (a0_M, "A0", "E_M")):
        if r.arm != a or r.execution != e or r.h_star != h:
            raise ValueError(f"run {r.arm}/{r.execution}/{r.h_star}h is not {a}/{e} at h* = {h}h")
    reg, clf = families(arm)
    s = cv.canonical(panel.samples, h)
    if len(s) == 0:
        raise ValueError("no valid holdout labels for P7 / P8")
    pred = final_model.predict(s)  # type: ignore[attr-defined]
    need = [f"yhat_{reg}", f"yhat_cal_{reg}", f"p_{clf}_cal"]
    if any(c not in pred.columns for c in need):
        raise ValueError(f"final model output lacks {need}")
    dates = cv.decision_dates(s["decision_ts"])
    y = s["y_reg"].to_numpy(np.float64)
    ic = (pred[need[0]].to_numpy(np.float64), y, dates)
    calib = (pred[need[1]].to_numpy(np.float64), y, dates)
    a1 = (pred[need[2]].to_numpy(np.float64), s["y_pos"].to_numpy(np.float64), dates)
    if not math.isfinite(panel.invalid_rate):
        raise ValueError("K8: the invalid-label rate is undefined (no labels)")
    k8 = K8Rates(float(panel.invalid_rate), missing_funding_rate([ml_T, a0_T]))
    return HoldoutInputs(ml_T, a0_T, ml_M, a0_M, tuple(int(x) for x in quarter_edges), ic, calib,
                         mean_daily_net(account(dev_run)), tuple(kill), tuple(leakage), tuple(integrity), k8,
                         secondary, a1, B, seed)


__all__ = ["DevSelection", "dev_selection", "holdout_inputs", "missing_funding_rate", "quarters"]
