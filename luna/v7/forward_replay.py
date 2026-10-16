"""§15 forward replay: the recorded W_FWD7 replayed by the frozen engine at L7, and its forward verdict
(spec §15 "Missing data", "Evidence", "Single look", "Verdicts"; DEC-42; DK-99 to DK-105, ruling R39; AMB-24,
AMB-43).

* **Gate:** ``replay`` takes no time, L7, T_F7, R_V6, window or repository argument. L7, T_F7 and W_FWD7 come
  from the canonical, hash-checked records (``luna.v7.release``) of the guards' repository (``guard._roots``,
  the same source as the engine's W_FWD7 refusal) and the trusted clock; before L7 (or while either record
  is absent / does not verify) the replay is refused (``SealedBeforeL7``, K9-logged). The engine runs with
  ``now_ns`` = the trusted clock, so its own W_FWD7 refusal (``guard.check_outcome_window``) opens exactly
  when ``forward.require_l7`` does. The recorded data come from the sealed ``ForwardStore`` (readable only
  from the canonical L7 on); building ``EngineInputs`` from the recorder's raw format is the recorder's
  (not yet specified) loader, so ``inputs`` are passed in.
* **Model** (ruling R54): an ML arm scores only with the frozen forward model: a ``ForwardScoreModel`` over
  ``forward_model.load_forward_model()`` whose hash is the canonical write-once record's.
* **Evaluability** (DEC-42; DK-101): ``evaluable`` maps each quarter start of W_FWD7 to the
  ``forward.evaluable_quarter`` result (a missing / non-True entry is NOT evaluable: fail closed); evaluable
  days = ``forward.count_evaluable_days`` (≥ 87 of 96 quarters, minus the days a > 24 h recorder gap touches).
* **Decision plan** (``decision_plan``, DK-101): a grid decision t (S-8.1) whose quarter is not evaluable
  moves to the first evaluable quarter within 2 h (``forward.decision_time``, same offset inside the
  quarter); otherwise it is skipped: no entries, its exits and risk exits run at the first evaluable quarter
  after t (``forward.skipped_exit_time``) as an exits-only decision (``EngineInputs.decisions`` False; the
  S-7.4 hedge follows the post-decision book, AMB-43 (c)). A skipped decision's exit time that coincides with
  a full decision merges into it (the full decision carries the exits). So a stale quarter is a no-op for
  DECISIONS. The tick-level "no-op for risk" is NOT implemented: which quarter a 15-minute tick belongs to
  and whether a missing leg mark in a stale quarter skips the tick or stays ``INVALID RUN`` (AMB-29 (a)) are
  open normative choices (AMB-43 (d), STOPPED); the engine's ticks are unchanged.
* **Verdict** (``evaluate``): ``forward.forward_evaluate`` on the replay's ML* (and optional A0) books and
  evaluable days, with the canonical source of the guards (DK-99 / DK-100 / DK-102 / DK-103 / DK-104).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace

import numpy as np

from luna.v7 import forward, guard, k9, release, stats, windows
from luna.v7.book import engine
from luna.v7.book.engine import BookRun, EngineInputs, ScoreModel
from luna.v7.forward import ForwardInputs, ForwardVerdict, TerminalHit
from luna.v7.forward_model import ForwardScoreModel, loaded_hash
from luna.v7.forward_store import SealedBeforeL7
from luna.v7.timeutil import MIN, D

Q = forward.Q
FILL_DELAY = engine.FILL_DELAY


@dataclass(frozen=True)
class DecisionPlan:
    """``schedule``: decision time -> entries allowed (False = a skipped decision's exits only); ``shifted``:
    (grid t, decision time) of every moved decision; ``skipped``: (grid t, exits time or None)."""

    schedule: dict[int, bool]
    shifted: tuple[tuple[int, int], ...]
    skipped: tuple[tuple[int, int | None], ...]


def evaluability(evaluable: Mapping[int, bool]) -> Callable[[int], bool]:
    """The quarter predicate of a {quarter start: evaluable} map; absent / non-True -> not evaluable."""
    return lambda q: evaluable.get(int(q)) is True or evaluable.get(int(q)) is np.True_


def decision_plan(window: tuple[int, int], evaluable_fn: Callable[[int], bool]) -> DecisionPlan:
    """The §15 decision schedule of ``window`` (module docstring). Only times decided before the unwind
    (t + 15 min < end - 1 min) are scheduled, as on the grid."""
    lo, end = int(window[0]), int(window[1])
    unwind = end - MIN
    grid = [int(t) for t in windows.decision_times(lo, end) if int(t) + FILL_DELAY < unwind]
    full: dict[int, bool] = {}
    exits: list[int] = []
    shifted: list[tuple[int, int]] = []
    skipped: list[tuple[int, int | None]] = []
    for t in grid:
        d = forward.decision_time(t, evaluable_fn)
        if d is not None:
            if d != t:
                shifted.append((t, d))
            if d + FILL_DELAY < unwind:
                full[d] = True
            continue
        x = forward.skipped_exit_time(t, evaluable_fn, end)
        skipped.append((t, x))
        if x is not None and x + FILL_DELAY < unwind:
            exits.append(x)
    sched = dict(full)
    for x in exits:
        sched.setdefault(x, False)
    return DecisionPlan(dict(sorted(sched.items())), tuple(shifted), tuple(skipped))


def evaluable_days(window: tuple[int, int], evaluable: Mapping[int, bool],
                   gaps: Sequence[tuple[int, int]] = ()) -> int:
    """``forward.count_evaluable_days`` of the window from the quarter map and the recorder gaps."""
    lo, end = int(window[0]), int(window[1])
    ev = evaluability(evaluable)
    days = [[ev(d + k * Q) for k in range(forward.QUARTERS_PER_DAY)] for d in range(lo, end, D)]
    return forward.count_evaluable_days(days, (lo, end), gaps)


@dataclass(frozen=True)
class ForwardReplay:
    run: BookRun
    plan: DecisionPlan
    evaluable_days: int
    l7: int
    extended: bool


def _canonical() -> object:
    return guard._roots()[0]


def _check_model(arm: str, model: ScoreModel | None, h_star: int) -> None:
    """ML arms score with the frozen forward model only (ruling R54): a ``ForwardScoreModel`` over a model
    from ``forward_model.load_forward_model`` whose hash is the canonical record's, at the record's h* and
    ML*."""
    if arm == "A0":
        return
    rec = guard.forward_model_record()
    if rec is None or not isinstance(model, ForwardScoreModel):
        raise guard.HoldoutBlocked("the forward replay of an ML arm needs the frozen forward model "
                                   "(forward_model.load_forward_model)")
    fm = model.models[0]
    if loaded_hash(fm) != rec["forward_model_hash"]:
        raise guard.HoldoutBlocked("the score model is not the frozen forward model of the record")
    if int(h_star) != int(rec["horizon"]) or model.ml_star != rec["ml_star"][0]:
        raise ValueError("the forward replay runs at the record's h* with ML*'s ranker")


def replay(arm: str, inputs: EngineInputs, evaluable: Mapping[int, bool], h_star: int,
           model: ScoreModel | None = None, *, gaps: Sequence[tuple[int, int]] = (),
           extended: bool = False) -> ForwardReplay:
    """Replay one forward book (L-norm, E_T) over the canonical W_FWD7 at / after the canonical L7 (module
    docstring). ``evaluable``: {quarter start: evaluable} over W_FWD7; ``gaps``: recorder gaps [lo, hi) of
    required inputs."""
    k9.require()
    repo = _canonical()
    try:
        l7 = forward.require_l7(extended=extended)
    except SealedBeforeL7 as e:
        k9.log_refusal(e, request={"kind": "forward_replay", "arm": str(arm), "extended": bool(extended)},
                       rule=guard.k9_rule(e))
        raise
    _check_model(arm, model, h_star)
    win = forward.w_fwd7(release.t_f7(repo), extended)  # type: ignore[arg-type]
    plan = decision_plan(win, evaluability(evaluable))
    run_inputs = replace(inputs, decisions=plan.schedule, now_ns=release.trusted_now_ns())
    run = engine.run(arm, run_inputs, win, h_star, model, "E_T", "L-norm")
    return ForwardReplay(run, plan, evaluable_days(win, evaluable, gaps), int(l7), bool(extended))


def evaluate(ml: ForwardReplay, a0: ForwardReplay | None, *, holdout_class: str, integrity: Sequence[str],
             outcome_before_l7: Sequence[str], unrecovered_funding: Sequence[int],
             terminal_events: Sequence[TerminalHit], deploy: Mapping | None = None,
             dec14_verified: bool = False, ic: tuple | None = None, calib: tuple | None = None,
             B: int = stats.B_DEFAULT,  # noqa: N803
             seed: int = stats.SEED) -> ForwardVerdict:
    """The §15 forward verdict (``forward.forward_evaluate``) of the replayed ML* book (and A0, a reference
    only) with the replay's evaluable days, against the guards' canonical records."""
    if a0 is not None and (a0.plan != ml.plan or a0.evaluable_days != ml.evaluable_days
                           or a0.extended != ml.extended):
        raise ValueError("ML* and A0 must be replayed over the same recording (plan, evaluability)")
    x = ForwardInputs(holdout_class=holdout_class, ml=ml.run, evaluable_days=ml.evaluable_days,
                      integrity=integrity, outcome_before_l7=outcome_before_l7,
                      unrecovered_funding=unrecovered_funding, terminal_events=terminal_events,
                      a0=None if a0 is None else a0.run, extended=ml.extended, deploy=deploy,
                      dec14_verified=dec14_verified, ic=ic, calib=calib, B=B, seed=seed)
    return forward.forward_evaluate(x)


__all__ = ["DecisionPlan", "ForwardReplay", "decision_plan", "evaluability", "evaluable_days", "evaluate",
           "replay"]
