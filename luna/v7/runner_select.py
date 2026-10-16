"""Research runner, part 2: the S-10.6 / S-10.7 driver and the holdout-input assembly (spec S-10.6, S-10.7,
S-13.2 / S-13.4 inputs, K3, K8, K11, EC-2, DEC-19; AMB-27). Orchestration only (see ``luna.v7.runner``).

* ``dev_selection`` (SC-01 part 2, fix round 1 I3: the books share ONE decision universe: a provider that
  cannot hold every decision of the window in memory must carry a ``viewstore.ViewStore``, else refused):
  A0 at 24 / 72 / 168 h and A2 / A3 at every horizon, ONE continuous E_T / L-norm book
  each over the concatenated outer test quarters (each decision scored by its own quarter's fold model,
  ``runner.FoldScoreModel``; positions carry across quarter boundaries; the terminal unwind is the window
  end - 1 min, before the W_DEV end). Then ``selection.choose`` (validity precondition, EC-2) and, only when
  it holds, ``selection.dev_kill`` on ML* with the per-quarter nets; the secondary E_M runs of ML* and A0
  at h* follow.
* ``holdout_inputs``: ``verdict.HoldoutInputs`` from already-executed runs, the final model's predictions on
  the holdout label panel and the W_DEV facts. No data access; refuses without a
  ``guard.HoldoutAuthorization`` and after an S-10.7 kill (DEC-19); refuses runs not produced by the research
  entry point ``runner.run_book`` (``BookRun.research``, DK-114) and a final model whose regressor or
  classifier family is not ML*'s (A2: ``en`` / ``logit_en``, A3: ``lgb_reg`` / ``lgb_clf``).
* Every book of ``dev_selection`` comes from ``run_book`` and is therefore a research run: its DT4 record is
  always a research record (``runrecord.run_record``), refused from a dirty working tree (DK-114 / m10).
* Leakage (SC-02, AMB-33): ``dev_leakage_checks`` runs ``leakage.run_leakage_checks`` on the S-10.6 fold
  models of a DevSelection (ML*, the fold-model hashes). ``holdout_inputs`` takes that report (it must verify,
  be passed, be for ML* at h* and, with ``dev``, carry dev's fold-model hashes) and builds the holdout-phase
  report itself (``leakage.run_holdout_checks`` over the four holdout runs with the caller's decision
  ``records``, e.g. ``DecisionProvider.record``); no free leakage tuple exists any more.
* Verifiable DevSelection (SC-05, AMB-34): a DevSelection is built only by ``dev_selection`` (a module-private
  issuer checked in ``__post_init__``) and carries the sha256 of its canonical record
  (``dev_selection_record``: windows, the S-10.6 result, ML*, the S-10.7 kill, fold nets, the identity and
  output sha256 of every book, the fold-model hashes, convergence); ``verify_dev_selection`` recomputes it and
  requires it to have been issued in this process. Persisted with the LeakageReport in the pre-opening record
  (``luna.v7.opening``), re-verified across processes by ``guard.authorize_holdout``.
* ``holdout_inputs`` is bound to the opening (SC-05, I-2): the authorization must verify (markers and pin,
  ``guard.verify_authorization``); the LeakageReport must be the pinned one (sha256), the DevSelection (or the
  W_DEV ML* run) the pinned one, the fold-model hashes the pinned ones on every path, and the final model the
  one the report audited (``leakage.model_hash(final_model) == report.final_model_hash``).
"""

from __future__ import annotations

import hashlib
import json
import math
import weakref
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from luna.v7 import cv, guard, leakage, models, runner, runrecord, selection, stats
from luna.v7 import verdict as verdict_mod
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
from luna.v7.windows import W_DEV, decision_times

_DEV_ISSUER = object()  # module-private: only dev_selection builds a DevSelection
# HoldoutInputs issued by holdout_inputs (by id, weakly held) and the opening they were issued for
_ISSUED_INPUTS: weakref.WeakValueDictionary = weakref.WeakValueDictionary()
_INPUT_OPENING: dict[int, str] = {}
_DEV_ISSUED: set[str] = set()  # record sha256 of every DevSelection issued here (this process)


@dataclass(frozen=True)
class DevSelection:
    """S-10.6 / S-10.7 result. ``result``: ``selection.Choice`` or ``selection.Invalid``. ``kill`` and
    ``fold_nets`` are None when the validity precondition failed (S-10.7 runs only after it holds). Built only
    by ``dev_selection``; ``sha256`` is the sha256 of ``dev_selection_record`` (``verify_dev_selection``)."""

    window: tuple[int, int]
    quarter_edges: tuple[int, ...]
    runs: dict[tuple[str, int], BookRun]
    models: dict[tuple[str, int], FoldScoreModel]
    result: selection.Choice | selection.Invalid
    kill: tuple[str, ...] | None
    fold_nets: tuple[float, ...] | None
    em_runs: dict[str, BookRun]
    convergence: tuple[dict, ...]
    sha256: str = ""
    _issuer: object = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._issuer is not _DEV_ISSUER:
            raise TypeError("a DevSelection is built only by runner_select.dev_selection")

    @property
    def ml_star(self) -> tuple[str, int] | None:
        r = self.result
        return (r.arm, r.h) if isinstance(r, selection.Choice) else None


def _plain(o: object) -> object:
    """JSON-plain form: tuples -> lists, numpy scalars -> Python, non-finite floats -> None."""
    if isinstance(o, bool) or o is None or isinstance(o, str):
        return o
    if hasattr(o, "item") and not isinstance(o, list | tuple | dict):
        o = o.item()  # type: ignore[attr-defined]
    if isinstance(o, float):
        return o if math.isfinite(o) else None
    if isinstance(o, int):
        return o
    if isinstance(o, Mapping):
        return {str(k): _plain(v) for k, v in o.items()}
    if isinstance(o, list | tuple):
        return [_plain(v) for v in o]
    raise TypeError(f"not JSON-plain in a DevSelection record: {type(o).__name__}")


def _result_record(res: selection.Choice | selection.Invalid) -> dict:
    if isinstance(res, selection.Choice):
        return {"type": "choice", "arm": res.arm, "h": int(res.h),
                "lbs": [[a, int(h), float(v)] for (a, h), v in sorted(res.lbs.items())],
                "ell": int(res.ell), "B": int(res.B), "seed": int(res.seed),
                "reference_invalid": list(res.reference_invalid)}
    if isinstance(res, selection.Invalid):
        return {"type": "invalid", "reason": res.reason, "runs": list(res.runs),
                "reference_invalid": list(res.reference_invalid)}
    raise ValueError("DevSelection.result must be selection.Choice or selection.Invalid")


def dev_selection_record(dev: DevSelection) -> dict:
    """The canonical JSON record of a DevSelection (what its sha256 covers): windows, the S-10.6 result, ML*,
    the S-10.7 kill (None = not run), the fold nets, the DT4 identity and output sha256 of every book
    (``runrecord.run_identity``), the fold-model hashes per (arm, h) and the convergence rows."""
    body = {
        "kind": "DevSelection",
        "window": [int(dev.window[0]), int(dev.window[1])],
        "quarter_edges": [int(x) for x in dev.quarter_edges],
        "result": _result_record(dev.result),
        "ml_star": None if dev.ml_star is None else [dev.ml_star[0], int(dev.ml_star[1])],
        "kill": None if dev.kill is None else [str(k) for k in dev.kill],
        "fold_nets": None if dev.fold_nets is None else [float(x) for x in dev.fold_nets],
        "runs": [{"key": [a, int(h)], **runrecord.run_identity(r)} for (a, h), r in sorted(dev.runs.items())],
        "em_runs": {str(k): runrecord.run_identity(r) for k, r in sorted(dev.em_runs.items())},
        "fold_model_hashes": [[a, int(h), list(m.hashes)] for (a, h), m in sorted(dev.models.items())],
        "convergence": _plain(list(dev.convergence)),
    }
    return json.loads(runrecord.canonical_json(_plain(body)))


def _record_sha(dev: DevSelection) -> str:
    return hashlib.sha256(runrecord.canonical_json(dev_selection_record(dev)).encode()).hexdigest()


def _issue_dev(**fields) -> DevSelection:
    """The module-private producer of DevSelection (``dev_selection`` only; the tests' documented hook)."""
    sha = _record_sha(DevSelection(**fields, _issuer=_DEV_ISSUER))
    _DEV_ISSUED.add(sha)
    return DevSelection(**fields, sha256=sha, _issuer=_DEV_ISSUER)


def verify_dev_selection(dev: object) -> bool:
    """True iff ``dev`` is a DevSelection issued by ``dev_selection`` (in this process) whose record still
    hashes to its ``sha256`` (a copied-and-altered one never verifies)."""
    if type(dev) is not DevSelection or dev._issuer is not _DEV_ISSUER:
        return False
    try:
        sha = _record_sha(dev)
    except (TypeError, ValueError, AttributeError):
        return False
    return sha == dev.sha256 and sha in _DEV_ISSUED


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
    if not provider.with_features:
        raise ValueError("the ML candidates score the §9 features: DecisionProvider(with_features=True)")
    n_dec = len(decision_times(*window))
    if provider.store is None and provider.max_cached is not None and n_dec > provider.max_cached:
        # the 11 books replay the same decisions: without a shared store every book would recompute every
        # view (SC-01 part 2, fix round 1 I3)
        raise ValueError(f"S-10.6 replays {n_dec} decisions in {len(selection.REQUIRED) + 2 * em} books: the "
                         f"provider keeps {provider.max_cached} views in memory; pass "
                         "DecisionProvider(store=viewstore.ViewStore(...)) (one decision universe shared by "
                         "every book; filled once, e.g. by runner.decision_pass)")
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
    conv = fold_convergence(fold_models, allow_unavailable=True)
    res = selection.choose({**runs, **ref}, B=B, seed=seed)
    if isinstance(res, selection.Invalid):
        return _issue_dev(window=window, quarter_edges=edges, runs={**runs, **ref}, models=models, result=res,
                          kill=None, fold_nets=None, em_runs={}, convergence=conv)
    key = (res.arm, res.h)
    led = account(runs[key])
    nets = tuple(period_net(led, list(edges)))
    kill = tuple(selection.dev_kill(led, nets))
    em_runs: dict[str, BookRun] = {}
    if em:
        em_runs["ML*"] = run_book(res.arm, u, provider, window, res.h, now_ns, model=models[key],
                                  execution="E_M")
        em_runs["A0"] = run_book("A0", u, provider, window, res.h, now_ns, execution="E_M")
    return _issue_dev(window=window, quarter_edges=edges, runs={**runs, **ref}, models=models, result=res,
                      kill=kill, fold_nets=nets, em_runs=em_runs, convergence=conv)


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


def dev_leakage_checks(dev: DevSelection, samples: Mapping[int, pd.DataFrame], final_model: object, *,
                       commit: str, dirty: bool, data_manifests: Mapping[str, str],
                       seed: int = models.SEED) -> leakage.LeakageReport:
    """``leakage.run_leakage_checks`` on a DevSelection: its ML* and the fold models its books scored with
    (``FoldScoreModel.models`` per horizon), ``samples`` = the W_DEV label panels of those fold models."""
    if dev.ml_star is None:
        raise ValueError("the S-10.6 validity precondition failed: no ML* (no leakage report, the holdout "
                         "stays sealed)")
    arm, _ = dev.ml_star
    fms = {h: dev.models[(arm, h)].models for h in HORIZONS if (arm, h) in dev.models}
    return leakage.run_leakage_checks(samples=samples, fold_models=fms, final_model=final_model,
                                      ml_star=dev.ml_star, commit=commit, dirty=dirty,
                                      data_manifests=data_manifests, seed=seed)


def holdout_inputs(*, auth: guard.HoldoutAuthorization, ml_T: BookRun, a0_T: BookRun,  # noqa: N803
                   ml_M: BookRun, a0_M: BookRun, quarter_edges: Sequence[int],  # noqa: N803
                   panel: LabelPanel, final_model: object, leakage_report: leakage.LeakageReport,
                   records: Callable[[int], object], integrity: Sequence[str],
                   dev: DevSelection | None = None, dev_ml_run: BookRun | None = None,
                   dev_kill: Sequence[str] | None = None, dev_run_store: object = None,
                   secondary: Mapping | None = None, kraken: object = None,
                   B: int = stats.B_DEFAULT, seed: int = stats.SEED) -> HoldoutInputs:
    """``verdict.HoldoutInputs`` (see module docstring). W_DEV facts come from ``dev`` or from ``dev_ml_run``
    + ``dev_kill``; in a later (opening) process ``dev_run_store`` (a ``runstore.RunStore``) supplies the
    W_DEV ML* run, loaded with both hashes of the verified pin (``RunStore.load_pinned``). ``final_model``
    (``cv.final_model``, or any model with ``predict(df)``) predicts the canonical panel: P7 ``ic`` = (ŷ,
    y_reg, dates), P8 ``calib`` = (ŷ_cal, y_reg, dates), the A1 calibration = (P̂_cal, y_pos, dates); K8 =
    (the panel's invalid-label rate, ``missing_funding_rate`` of the required E_T runs). ``leakage_report``:
    the passed pre-opening ``leakage.LeakageReport`` of ML*; ``records``: the decision records of the holdout
    runs (L1 re-check, ``leakage.run_holdout_checks``). Everything is bound to the opening's pin (module
    docstring). ``secondary``: the S-13.3 family; comparison #5 (ML* Binance vs Kraken) enters ONLY as
    ``kraken`` = ``kraken.comparison(...)`` of this opening for this ML* E_T run (``_kraken_secondary``)."""
    # HoldoutBlocked: forged, the canonical opening missing / not this one, or the verdict already produced
    pin = guard.verify_authorization(auth)
    if dev is not None:
        if dev.ml_star is None or dev.kill is None:
            raise ValueError("the S-10.6 validity precondition failed: no ML* (the holdout stays sealed)")
        if not verify_dev_selection(dev) or dev.sha256 != pin["dev_selection_sha256"]:
            raise ValueError("the DevSelection is not the one pinned at the opening (dev_selection_sha256)")
        arm, h = dev.ml_star
        dev_run, kill = dev.runs[dev.ml_star], dev.kill
    else:
        if dev_ml_run is None and dev_run_store is not None:
            dev_ml_run = dev_run_store.load_pinned(auth)  # type: ignore[attr-defined]
        if dev_ml_run is None or dev_kill is None:
            raise ValueError("W_DEV facts missing: pass dev or (dev_ml_run, dev_kill)")
        arm, h, dev_run, kill = dev_ml_run.arm, dev_ml_run.h_star, dev_ml_run, tuple(dev_kill)
        if runrecord.run_output_sha256(dev_ml_run) != pin["dev_ml_run_sha256"]:
            raise ValueError("dev_ml_run is not the W_DEV ML* run of the DevSelection pinned at the opening")
    if kill:
        raise ValueError("S-10.7 development kill: the holdout is never opened (K3, DEC-19)")
    for r, a, e in ((ml_T, arm, "E_T"), (a0_T, "A0", "E_T"), (ml_M, arm, "E_M"), (a0_M, "A0", "E_M")):
        if r.arm != a or r.execution != e or r.h_star != h:
            raise ValueError(f"run {r.arm}/{r.execution}/{r.h_star}h is not {a}/{e} at h* = {h}h")
    for name, r in (("ml_T", ml_T), ("a0_T", a0_T), ("ml_M", ml_M), ("a0_M", a0_M), ("W_DEV ML*", dev_run)):
        if not r.research:  # DK-114 / m10: only runs of the research entry point (runner.run_book)
            raise ValueError(f"{name}: not a research run (runner.run_book); its DT4 record would not be one")
    rep = leakage_report
    if not isinstance(rep, leakage.LeakageReport) or not leakage.verify_report(rep):
        raise ValueError("leakage_report: a LeakageReport issued by leakage.run_leakage_checks is required")
    if not rep.passed:
        raise ValueError(f"the pre-opening LeakageReport did not pass: the holdout is never opened (DEC-19): "
                         f"{rep.blocking}")
    if rep.ml_star != (arm, int(h)):
        raise ValueError(f"the LeakageReport is for ML* = {rep.ml_star}, not {(arm, int(h))}")
    if rep.sha256 != pin["leakage_report_sha256"]:
        raise ValueError("the LeakageReport is not the one pinned at the opening (leakage_report_sha256)")
    pinned_folds = {int(k): tuple(v) for k, v in pin["fold_model_hashes"].items()}
    if dict(rep.fold_model_hashes) != pinned_folds:  # I-2: fold models bound on every path
        raise ValueError("the LeakageReport's fold models are not the DevSelection's pinned at the opening")
    if dev is not None:
        dev_hashes = {hh: tuple(m.hashes) for (a, hh), m in dev.models.items() if a == arm}
        if dict(rep.fold_model_hashes) != dev_hashes:
            raise ValueError("the LeakageReport was not run on this DevSelection's fold models")
    reg, clf = families(arm)
    if int(getattr(final_model, "horizon", -1)) != int(h):
        raise ValueError(f"the final model is not for h* = {h}h")
    fams = getattr(final_model, "reg", None)
    if fams is not None and reg not in fams:
        raise ValueError(f"the final model has no {reg} regressor (ML* = {arm})")
    cfams = getattr(final_model, "clf", None)  # deferred 19b minor: the A1 / P8 classifier of ML*'s family
    if (fams is None) != (cfams is None) or (cfams is not None and clf not in cfams):
        raise ValueError(f"the final model has no {clf} classifier (ML* = {arm}: {reg} / {clf})")
    final_hash = leakage.model_hash(final_model)  # ValueError if unhashable: no provenance, fail closed
    if final_hash != rep.final_model_hash or final_hash != pin["final_model_hash"]:  # None never matches
        raise ValueError("the final model scored is not the one the pinned LeakageReport audited (I-2)")
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
    secondary = _kraken_secondary(secondary, kraken, pin, ml_T)
    hold = leakage.run_holdout_checks(rep, runs=[ml_T, a0_T, ml_M, a0_M], records=records)
    x = HoldoutInputs(ml_T=ml_T, a0_T=a0_T, ml_M=ml_M, a0_M=a0_M,
                         quarter_edges=tuple(int(x) for x in quarter_edges), ic=ic, calib=calib,
                         dev_mean_daily_net=mean_daily_net(account(dev_run)), dev_kill=tuple(kill),
                         leakage=rep, holdout_leakage=hold, integrity=tuple(integrity), k8=k8,
                         secondary=secondary, a1_calib=a1, B=B, seed=seed)
    runner._note_holdout_universe(panel, auth)  # the W_HOLD universe the forward refit must match (R54)
    _ISSUED_INPUTS[id(x)] = x  # the inputs holdout_verdict accepts (issued here, for this opening)
    _INPUT_OPENING[id(x)] = pin["opening_id"]
    return x


def _kraken_secondary(secondary: Mapping | None, kraken: object, pin: Mapping, ml_T: BookRun  # noqa: N803
                      ) -> dict | None:
    """S-13.3 #5 wiring (S-1.6, L11; AMB-41): a caller-supplied Kraken entry other than None is refused (no
    Kraken pair bypasses the L11 guard); ``kraken`` must be a ``kraken.KrakenComparison`` issued at THIS
    opening (``opening_id``) for THIS ML* E_T run (output sha256); its ``pair`` (an exploratory, labelled
    entry; here ``verdict.Unavailable``: STOPPED) becomes member #5. Absent: the member stays "not
    supplied" (m = 6)."""
    from luna.v7 import kraken as kr  # local: kraken imports this package's guard / verdict stack

    if secondary is not None and secondary.get(verdict_mod.KRAKEN) is not None:
        raise ValueError("comparison #5 (ML* Binance vs Kraken) enters only as kraken=kraken.comparison(...) "
                         "(L11 Kraken outcome guard)")
    if kraken is None:
        return None if secondary is None else dict(secondary)
    if secondary is None:
        raise ValueError("kraken given without the secondary family (S-13.3)")
    if not kr.verify_comparison(kraken):
        raise ValueError("kraken: not a KrakenComparison issued by kraken.comparison")
    if kraken.opening_id != pin["opening_id"]:  # type: ignore[attr-defined]
        raise ValueError("kraken: the comparison was issued for another opening")
    if kraken.ml_run_sha256 != runrecord.run_output_sha256(ml_T):  # type: ignore[attr-defined]
        raise ValueError("kraken: the comparison is not for this ML* E_T run")
    return {**secondary, verdict_mod.KRAKEN: kraken.pair}  # type: ignore[attr-defined]


def holdout_verdict(x: HoldoutInputs, *, auth: guard.HoldoutAuthorization) -> verdict_mod.Verdict:
    """The W_HOLD verdict of the opening (S-13.4): ``verdict.holdout`` on inputs bound to a verified
    authorization, then the verdict marker is written once (``guard.mark_verdict_produced``, exclusive
    create); the verdict is returned only after the marker exists, so it can never be produced twice and the
    opening can no longer be resumed (ruling R45: no second look). ``verdict.holdout`` itself stays pure."""
    pin = guard.verify_authorization(auth)  # refused once a verdict exists
    if _ISSUED_INPUTS.get(id(x)) is not x or _INPUT_OPENING.get(id(x)) != pin["opening_id"]:
        raise ValueError("holdout_verdict accepts only inputs issued by holdout_inputs for this opening")
    v = verdict_mod._holdout(x)
    body = {"cls": v.cls, "reasons": list(v.reasons), "leakage": x.leakage.sha256,
            "holdout_leakage": x.holdout_leakage.sha256}
    sha = hashlib.sha256(runrecord.canonical_json(body).encode()).hexdigest()
    guard.mark_verdict_produced(auth, sha)
    return v


__all__ = ["DevSelection", "dev_leakage_checks", "dev_selection", "dev_selection_record", "holdout_inputs",
           "holdout_verdict", "missing_funding_rate", "quarters", "verify_dev_selection"]
