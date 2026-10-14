"""V7 forward protocol: start time, single look L7, evaluability, missing data, EC-11 and the forward verdict
(spec §15, DEC-10, DEC-42, DEC-45, EC-8, EC-11, K11, S-1.2, S-1.3, S-13.1, S-14.1; AMB-24).

Pure: no I/O and no wall-clock (``now_ns`` is always injected); the replay itself is ``book.engine.run`` over
the recorded data, whose own guard refuses M6 before R_V6.

* **Precondition:** the `W_HOLD` verdict is ``V7 HOLDOUT ADVANCE``; otherwise there is no forward test.
* **Forward model:** mechanical refit, no re-selection, T_fit = [D0, 2026-01-01), T_cal = [2026-01-01,
  2026-07-01) (``FORWARD_BLOCKS``), hashed and frozen before T_F7.
* **T_F7** = the first 00:00 UTC ≥ 24 h after the independent review PASS; W_FWD7 = [T_F7, T_F7 + 180 d)
  (+ 90 d after the single extension).
* **L7** = max(T_F7 + 180 d, R_V6) + 1 d (the extended end replaces T_F7 + 180 d). R_V6 undetermined → L7 is
  None and every forward outcome computation is refused (``SealedBeforeL7``), as is any call before L7.
* **Evaluability (DEC-42):** a quarter is evaluable when every PRIMARY replay input is present and fresh
  (book-top / depth / ticker are diagnostic and ignored; the exchange-filter snapshot is required only for
  the D1–D3 deployment evaluation); a day is evaluable when ≥ 90 % of its 96 quarters are; a recorder gap
  > 24 h in a required input makes the days it touches non-evaluable. Evidence: ≥ 144 evaluable days and P4
  scaled to 30 active days, 50 closed legs, 10 clusters.
* **Missing data:** a decision whose quarter is not evaluable moves to the first evaluable quarter within 2 h
  after t, else it is skipped (no entries; exits / risk exits at the first evaluable quarter). Funding missing
  in the recorder must be backfilled from the official REST history (hashed) after the period, else INVALID
  RUN. Prices are never backfilled.
* **EC-11 (forward only):** an actually-held B / C terminal event of ML* → INVALID RUN; one of A0 only → A0
  is unavailable from that time, the ML* − A0 comparison is "unavailable/incomplete", no INVALID RUN.
* **Verdict:** INVALID RUN (integrity, K10, an outcome computed before L7, unrecovered funding, ML* B / C) >
  NO-GO (H_F p_abs ≥ 0.05, P2, P3 incl. the drawdown kill, P6 or P11 fails) > INCONCLUSIVE (evidence) > GO.
  A scientific GO is "GO" only with D1–D3 on forward point-in-time filters (exact S-14.1 evidence) and the
  DEC-14 verification; otherwise "GO — not deployable at 265 USD". P7 / P8 and ML* − A0 are reported only.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import numpy as np

from luna.v7 import stats, verdict
from luna.v7.accounting import account, daily_ok, k10_ok, mean_daily_net, p11_ok, stressed
from luna.v7.book.engine import BookRun
from luna.v7.book.execution import INVALID_TERMINAL
from luna.v7.constants import FROZEN
from luna.v7.forward_store import SealedBeforeL7
from luna.v7.spec import v6_artifacts
from luna.v7.timeutil import MIN, D, H, to_ns
from luna.v7.windows import D0

FW = FROZEN.forward
ACC = FROZEN.accept
Q = 15 * MIN
QUARTERS_PER_DAY = 96
FORWARD_BLOCKS = dict(T_fit=(D0, to_ns("2026-01-01")), T_cal=(to_ns("2026-01-01"), to_ns("2026-07-01")))

INVALID = "INVALID RUN"
NOGO = "NO-GO"
INCONCLUSIVE = "INCONCLUSIVE"
GO = "GO"
GO_NOT_DEPLOYABLE = "GO — not deployable at 265 USD"
Verdict = Literal["INVALID RUN", "NO-GO", "INCONCLUSIVE", "GO", "GO — not deployable at 265 USD"]
COMPLETE, INCOMPLETE = "complete", "unavailable/incomplete"

PRIMARY_INPUTS = ("bars_1m", "bars_15m", "mark_index_premium", "funding", "metadata", "announcement_poll",
                  "reference_prices")  # DEC-42 (R3, R5; R4 / S-4.6 / F5; R2 / U7; R1; R11; other references)
DIAGNOSTIC_INPUTS = ("book_top", "depth", "ticker")  # never make a quarter non-evaluable
DEPLOY_INPUTS = ("exchange_filters",)  # D1–D3 only
ARMS = ("A0", "A1", "A2", "A3")  # S-7.2 arm ids (ML* is A2 or A3, the selected one)


class NoForwardTest(RuntimeError):
    """§15 precondition: the W_HOLD verdict is not V7 HOLDOUT ADVANCE."""


# --- times -------------------------------------------------------------------------------------------------
def t_f7(review_pass_ns: int) -> int:
    """The first 00:00 UTC ≥ 24 h after the independent review PASS."""
    x = to_ns(review_pass_ns) + FW.tf7_delay_h * H
    return -(-x // D) * D


def _check_tf7(t: int) -> int:
    t = to_ns(t)
    if t % D:
        raise ValueError("T_F7 must be a 00:00 UTC timestamp")
    return t


def w_fwd7(t_f7_ns: int, extended: bool = False) -> tuple[int, int]:
    """W_FWD7 = [T_F7, T_F7 + 180 d); after the single extension [T_F7, T_F7 + 270 d)."""
    t = _check_tf7(t_f7_ns)
    return t, t + (FW.days + (FW.extension_days if extended else 0)) * D


def l7(t_f7_ns: int, r_v6: int | None, extended: bool = False) -> int | None:
    """L7 = max(T_F7 + 180 d, R_V6) + 1 d (the extended end after the extension); None while R_V6 is
    undetermined (the look is refused)."""
    end = w_fwd7(t_f7_ns, extended)[1]
    if r_v6 is None:
        return None
    return max(end, to_ns(r_v6)) + FW.l7_pad_days * D


def r_v6_strict_default(repo: Path | None = None) -> int:
    """The date of S-1.2 (a), V6's day-365 look (2027-10-14T00:00Z, from the hash-checked V6 artefacts): the
    strict default under which §15 states L7 ≥ 2027-10-14. It is NOT a lower bound of R_V6: R_V6 is the
    EARLIEST of (a), (b) and (c), so a terminal V6 look (b) or a written release (c) can make it earlier;
    and (a) itself needs the look's completion and report, so R_V6 is an input, never derived here."""
    return v6_artifacts(repo).looks[2]


def require_l7(now_ns: int, l7_ns: int | None) -> None:
    """Refuse every forward outcome computation before L7 (or while L7 is undetermined)."""
    if l7_ns is None or to_ns(now_ns) < l7_ns:
        raise SealedBeforeL7("no forward outcome is computed before L7 or while R_V6 is undetermined (§15)")


# --- evaluability ------------------------------------------------------------------------------------------
def evaluable_quarter(inputs_present: Mapping[str, bool], *, deployment: bool = False) -> bool:
    """DEC-42: every primary replay input present and fresh (``True``); diagnostic feeds are ignored; the
    exchange-filter snapshot is required only when ``deployment`` (D1–D3). A missing / non-True primary entry
    is not evaluable (fail closed); an unknown key is refused."""
    known = set(PRIMARY_INPUTS) | set(DIAGNOSTIC_INPUTS) | set(DEPLOY_INPUTS)
    unknown = set(inputs_present) - known
    if unknown:
        raise ValueError(f"unknown replay inputs {sorted(unknown)}")
    need = PRIMARY_INPUTS + (DEPLOY_INPUTS if deployment else ())
    return all(inputs_present.get(k) is True or inputs_present.get(k) is np.True_ for k in need)


def evaluable_day(quarters: Sequence[bool]) -> bool:
    """≥ 90 % of the day's 96 quarter-hours evaluable (87 of 96)."""
    q = list(quarters)
    if len(q) != QUARTERS_PER_DAY:
        raise ValueError("a UTC day has exactly 96 quarter-hours")
    n = sum(1 for x in q if x is True or x is np.True_)
    return n >= FW.eval_quarter_share * QUARTERS_PER_DAY


def gap_days(gaps: Iterable[tuple[int, int]], window: tuple[int, int]) -> set[int]:
    """Day indices (0 = the window's first day) made non-evaluable by a recorder gap > 24 h [lo, hi) in a
    required input: every day the gap overlaps."""
    lo_w, hi_w = int(window[0]), int(window[1])
    out: set[int] = set()
    for lo, hi in gaps:
        lo, hi = int(lo), int(hi)
        if hi <= lo:
            raise ValueError("empty gap")
        if hi - lo <= FW.gap_h * H:
            continue
        a, b = max(lo, lo_w), min(hi, hi_w)
        if b > a:
            out.update(range((a - lo_w) // D, (b - 1 - lo_w) // D + 1))
    return out


def count_evaluable_days(day_quarters: Sequence[Sequence[bool]], window: tuple[int, int],
                         gaps: Iterable[tuple[int, int]] = ()) -> int:
    """Evaluable days of the window: one 96-quarter list per day, minus the > 24 h gap days."""
    n_days = (int(window[1]) - int(window[0])) // D
    if len(day_quarters) != n_days:
        raise ValueError(f"need one quarter list per window day ({n_days})")
    bad = gap_days(gaps, window)
    return sum(1 for i, q in enumerate(day_quarters) if i not in bad and evaluable_day(q))


def decision_time(grid_t: int, evaluable_fn: Callable[[int], bool]) -> int | None:
    """The decision of grid time t: at t if its quarter is evaluable, else at the first evaluable quarter
    within 2 h after t (same offset inside the quarter: t + k · 15 min, k = 1..8); None = skipped."""
    t = int(grid_t)
    q0 = t - t % Q
    for k in range(0, FW.shift_max_h * 4 + 1):
        if evaluable_fn(q0 + k * Q):
            return t + k * Q
    return None


def skipped_exit_time(grid_t: int, evaluable_fn: Callable[[int], bool], end: int) -> int | None:
    """A skipped decision's exits and risk exits: the first evaluable quarter after t (before ``end``)."""
    t = int(grid_t)
    q0 = t - t % Q
    k = 1
    while q0 + k * Q < int(end):
        if evaluable_fn(q0 + k * Q):
            return t + k * Q
        k += 1
    return None


_SHA = re.compile(r"[0-9a-f]{64}")


def unrecovered_funding(missing_ts: Iterable[int], backfilled: Mapping[int, str]) -> list[int]:
    """Funding settlements missing in the recorder and not backfilled from the official REST history with a
    SHA-256 (never interpolated); any → INVALID RUN."""
    return sorted(int(t) for t in missing_ts
                  if not (isinstance(backfilled.get(int(t)), str) and _SHA.fullmatch(backfilled[int(t)])))


# --- EC-11 -------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class TerminalHit:
    """A terminal event (S-5.4 class A / B / C) reaching a position of a forward book."""

    ts: int
    arm: str  # S-7.2 arm id of the book: "A0", "A1", "A2" or "A3"
    klass: str
    held: bool  # the book actually held the contract

    def __post_init__(self) -> None:
        if self.arm not in ARMS:
            raise ValueError(f"unknown arm {self.arm!r} (one of {ARMS})")
        if self.klass not in ("A", "B", "C"):
            raise ValueError(f"unknown terminal class {self.klass!r}")


def _bc(e: TerminalHit) -> bool:
    if not isinstance(e, TerminalHit):
        raise ValueError("terminal events must be TerminalHit")
    return bool(e.held) and e.klass in ("B", "C")


def a0_availability(events: Iterable[TerminalHit]) -> tuple[int | None, str]:
    """EC-11: (the first time A0 is unavailable, or None, and the ML* − A0 comparison status). Only actually
    held B / C events of A0 count; ML* events are ``ml_terminal_invalid``'s."""
    hits = [int(e.ts) for e in events if e.arm == "A0" and _bc(e)]
    return (min(hits), INCOMPLETE) if hits else (None, COMPLETE)


def ml_terminal_invalid(events: Iterable[TerminalHit], ml_arm: str) -> bool:
    """EC-11 / K11: an actually-held B / C event of the selected ML* arm (``ml_arm``: A2 or A3) → INVALID RUN.
    Events of other books (the non-selected ML family, A1, A0) never invalidate the forward run."""
    if ml_arm not in ("A2", "A3"):
        raise ValueError("ML* is A2 or A3")
    return any(_bc(e) and e.arm == ml_arm for e in events)


def extension_allowed(inconclusive_reason: str | Iterable[str], *, already_extended: bool = False) -> bool:
    """One 90-day extension, only when the look is INCONCLUSIVE because of coverage alone."""
    r = {inconclusive_reason} if isinstance(inconclusive_reason, str) else set(inconclusive_reason)
    return bool(not already_extended and r == {"coverage"})


# --- verdict -----------------------------------------------------------------------------------------------
def p4_counts(run: BookRun, led) -> dict:
    """P4 counts of the forward book (verdict conventions, AMB-22 (h))."""
    return dict(active_days=verdict.active_days(run.marks), closed_legs=verdict.closed_legs(led),
                clusters=led.n_clusters)


def evidence(evaluable_days: int, counts: Mapping[str, int]) -> tuple[str, ...]:
    """The unmet evidence rules: "coverage" (< 144 evaluable days) and "p4" (P4 scaled to 180 / 365)."""
    out = []
    if int(evaluable_days) < FW.min_eval_days:
        out.append("coverage")
    if any(int(counts[k]) < FW.p4[k] for k in ("active_days", "closed_legs", "clusters")):
        out.append("p4")
    return tuple(out)


def classify(*, invalid: bool, criteria: Mapping[str, bool], evidence_missing: Sequence[str],
             deployable: bool) -> str:
    """§15 table, most severe first: INVALID RUN > NO-GO > INCONCLUSIVE > GO (AMB-24 (f))."""
    if invalid:
        return INVALID
    if set(criteria) != {"H_F", "P2", "P3", "P6", "P11"}:
        raise ValueError("criteria must hold exactly H_F, P2, P3, P6, P11")
    if not all(criteria.values()):
        return NOGO
    if evidence_missing:
        return INCONCLUSIVE
    return GO if deployable else GO_NOT_DEPLOYABLE


@dataclass(frozen=True)
class ForwardInputs:
    holdout_class: str  # the W_HOLD verdict (must be V7 HOLDOUT ADVANCE)
    t_f7: int
    r_v6: int | None
    now_ns: int
    ml: BookRun  # ML* replay, L-norm, E_T, over W_FWD7
    evaluable_days: int
    integrity: Sequence[str]  # integrity / guard findings (any → INVALID RUN)
    outcome_before_l7: Sequence[str]  # audit findings of outcomes computed before L7 (any → INVALID RUN)
    unrecovered_funding: Sequence[int]  # ``unrecovered_funding`` (any → INVALID RUN)
    terminal_events: Sequence[TerminalHit]  # held / not-held B / C events of the forward books (EC-11)
    a0: BookRun | None = None  # reference only
    extended: bool = False
    deploy: Mapping | None = None  # ``deploy.d_gates`` on the forward L-265 run
    dec14_verified: bool = False
    ic: tuple | None = None  # P7 report (pred, y, dates)
    calib: tuple | None = None  # P8 report (ŷ_cal, y, dates)
    B: int = stats.B_DEFAULT
    seed: int = stats.SEED


@dataclass(frozen=True)
class ForwardVerdict:
    cls: str
    reasons: tuple[str, ...]
    criteria: dict[str, bool] = field(default_factory=dict)
    evidence_missing: tuple[str, ...] = ()
    extension_allowed: bool = False
    a0_unavailable_from: int | None = None
    comparison: str = COMPLETE
    details: dict = field(default_factory=dict)


def _seq(v, what: str) -> tuple:
    if v is None or isinstance(v, (str, bytes)) or not isinstance(v, Sequence):
        raise ValueError(f"{what} must be a sequence (empty = checked, nothing found)")
    return tuple(v)


def _ledger_ok(run: BookRun):
    try:
        led = account(run)
    except ValueError as e:
        return None, f"not accountable: {e}"
    return (led, None) if k10_ok(led) and daily_ok(led) else (None, verdict.INTEGRITY_K10)


def forward_evaluate(x: ForwardInputs) -> ForwardVerdict:
    """The single forward look (§15). Raises before L7 / with R_V6 undetermined (nothing is computed)."""
    if x.holdout_class != verdict.ADVANCE:
        raise NoForwardTest(f"W_HOLD verdict {x.holdout_class!r}: no forward test (§15 precondition)")
    require_l7(x.now_ns, l7(x.t_f7, x.r_v6, x.extended))
    win = w_fwd7(x.t_f7, x.extended)
    for name, r in (("ML*", x.ml), ("A0", x.a0)):
        if r is None:
            continue
        if tuple(r.window) != win or r.layer != "L-norm" or r.execution != "E_T" or r.h_star != x.ml.h_star:
            raise ValueError(f"{name}: the forward replay is L-norm, E_T, over W_FWD7 {win} at ML*'s h*")
    if x.ml.arm not in ("A2", "A3") or (x.a0 is not None and x.a0.arm != "A0"):
        raise ValueError("the forward books are ML* (A2 / A3) and A0")
    integrity = _seq(x.integrity, "integrity")
    before = _seq(x.outcome_before_l7, "outcome_before_l7")
    unrec = _seq(x.unrecovered_funding, "unrecovered_funding")
    events = _seq(x.terminal_events, "terminal_events")
    bad = [f"integrity: {s}" for s in integrity] + [f"outcome computed before L7: {s}" for s in before]
    if unrec:
        bad.append(f"funding missing and not backfilled from the REST history: {len(unrec)} settlements")
    if ml_terminal_invalid(events, x.ml.arm):
        bad.append(f"ML*: {INVALID_TERMINAL}")
    if not x.ml.valid:
        bad.append(f"ML*: {x.ml.invalid}" + (f" ({x.ml.invalid_detail})" if x.ml.invalid_detail else ""))
    led = None
    if not bad:
        led, why = _ledger_ok(x.ml)
        if why is not None:
            bad.append(f"ML*: {why}")
    # EC-11: A0 is a reference only
    a0_from, comp = a0_availability(events)
    det: dict = {}
    if x.a0 is None:
        comp = INCOMPLETE
    elif not x.a0.valid:
        comp = INCOMPLETE
        if x.a0.invalid == INVALID_TERMINAL and x.a0.invalid_ts is not None:  # structured event time
            a0_from = min(t for t in (a0_from, int(x.a0.invalid_ts)) if t is not None)
    if bad:
        return ForwardVerdict(INVALID, tuple(bad), a0_unavailable_from=a0_from, comparison=comp)

    r = stats.ledger_returns(led)
    p_abs = stats.p_max_over_blocks(r, stats.BLOCKS, x.B, x.seed)
    crit = {"H_F": bool(p_abs < stats.ALPHA)}
    ratio = led.C_exec / led.F_net if led.F_net > 0 else float("inf")
    crit["P2"] = bool(led.F_net > 0 and ratio <= ACC.p2_ratio)
    mdd, killed = verdict.max_drawdown(x.ml, led), verdict.drawdown_killed(x.ml)
    crit["P3"] = bool(mdd <= ACC.p3_mdd and not killed)
    st = stressed(x.ml, **ACC.p6)
    crit["P6"] = bool(mean_daily_net(st) >= 0)
    crit["P11"] = p11_ok(led)
    counts = p4_counts(x.ml, led)
    missing = evidence(x.evaluable_days, counts)
    dep = x.deploy
    deployable = bool(dep is not None and dep.get("exact") is True and dep.get("D1") is True
                      and dep.get("D2") is True and dep.get("D3") is True and x.dec14_verified is True)
    det.update(p_abs=p_abs, mean_daily_return=float(np.mean(r)), F_net=led.F_net, C_exec_over_F_net=ratio,
               mdd=mdd, killed=killed, stressed_mean_daily_net=mean_daily_net(st), p4=counts,
               evaluable_days=int(x.evaluable_days), deploy=dep, dec14_verified=bool(x.dec14_verified))
    if comp == COMPLETE:  # report only (§15): ML* − A0
        a0led, why = _ledger_ok(x.a0)  # type: ignore[arg-type]
        if why is not None:
            comp = INCOMPLETE
        else:
            try:
                _, xd = stats.paired(led, a0led)
                det.update(ml_minus_a0_mean=float(np.mean(xd)),
                           ml_minus_a0_p=stats.p_max_over_blocks(xd, stats.BLOCKS, x.B, x.seed))
            except ValueError:
                comp = INCOMPLETE
    if x.ic is not None:  # P7 / P8 reported, never gates
        det["P7"] = verdict.p7(*x.ic, x.B, x.seed)
    if x.calib is not None:
        s = verdict.p8_slope(*x.calib)
        det["P8"] = (verdict.p8_ok(s), s)
    cls = classify(invalid=False, criteria=crit, evidence_missing=missing, deployable=deployable)
    reasons = [f"{k} fails" for k, v in crit.items() if not v] + [f"evidence: {m}" for m in missing]
    if cls == GO_NOT_DEPLOYABLE:
        reasons.append("deployment: D1–D3 on forward point-in-time filters and DEC-14 not all established")
    ext = cls == INCONCLUSIVE and extension_allowed(missing, already_extended=x.extended)
    return ForwardVerdict(cls, tuple(reasons), crit, missing, ext, a0_from, comp, det)


def forward_verdict(x: ForwardInputs) -> Verdict:
    """The §15 verdict string of ``forward_evaluate``."""
    return forward_evaluate(x).cls  # type: ignore[return-value]


__all__ = ["COMPLETE", "DEPLOY_INPUTS", "DIAGNOSTIC_INPUTS", "FORWARD_BLOCKS", "GO", "GO_NOT_DEPLOYABLE",
           "INCOMPLETE", "INCONCLUSIVE", "INVALID", "NOGO", "PRIMARY_INPUTS", "ForwardInputs",
           "ForwardVerdict", "NoForwardTest", "TerminalHit", "a0_availability", "classify",
           "count_evaluable_days",
           "decision_time", "evaluable_day", "evaluable_quarter", "evidence", "extension_allowed",
           "forward_evaluate", "forward_verdict", "gap_days", "l7", "ml_terminal_invalid", "p4_counts",
           "r_v6_strict_default", "require_l7", "skipped_exit_time", "t_f7", "unrecovered_funding", "w_fwd7"]
