"""Choosing ML* and h* on ``W_DEV``; the development kill (spec S-10.6, S-10.7, EC-2, K3, K10, K11; AMB-22).

* **Validity precondition (S-10.6, DEC-43, EC-2, K11):** all six candidates (A2 / A3 × 24 / 72 / 168 h) and
  A0 at each of 24 / 72 / 168 h must be valid runs. Any ``BookRun.invalid`` (an actually-held B / C terminal
  event or a halted held position without an executable exit, S-5.4) stops V7 before selection with
  ``INVALID RUN — historical terminal execution not reconstructible``; nothing is dropped, replaced or re-run.
  A required run whose §8 accounting fails K10 or the daily-table identity (``accounting.k10_ok`` /
  ``daily_ok``) is an S-13.4 integrity failure: ``INVALID RUN — K10 / accounting integrity failure``
  (AMB-22 (e)).
  Reference arms (A0-V5, TS) are only reported (valid / invalid) and never tested or required.
* **LB (S-10.6 step 2):** LB = r̄ − q₀.₉₅(r̄* − r̄) of the S-13.1 daily returns, CBB with ℓ = 14, B = 10,000.
* **Choice (steps 3–5, AMB-22 (f)):** the A3 family is chosen only if its best LB ≥ the best A2 LB + 1 bp/day;
  within the chosen family the highest LB wins, and every candidate within 0.1 bp/day of that highest LB is
  tied, the longest horizon among them wins. LBs are daily returns, 1 bp/day = 1e-4.
* **Development kill (S-10.7):** on the ML* out-of-fold ledger: mean daily net ≤ 0 (``accounting.
  mean_daily_net``, AMB-21 (d)); F_net ≤ 0; C_exec / F_net > 1.0; CAC ≤ 0; fewer than 30 % of outer folds with
  positive net.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from luna.v7 import stats
from luna.v7.accounting import Ledger, account, daily_ok, k10_ok, mean_daily_net
from luna.v7.book.engine import BookRun
from luna.v7.constants import FROZEN

HORIZONS = tuple(FROZEN.horizons_h)
ML_ARMS = ("A2", "A3")
CANDIDATES: tuple[tuple[str, int], ...] = tuple((a, h) for a in ML_ARMS for h in HORIZONS)
REQUIRED: tuple[tuple[str, int], ...] = CANDIDATES + tuple(("A0", h) for h in HORIZONS)
REFERENCE_ARMS = ("A0-V5", "TS")
INVALID_DEV = "INVALID RUN — historical terminal execution not reconstructible"  # S-10.6 exact text
INVALID_K10 = "INVALID RUN — K10 / accounting integrity failure"  # S-13.4 integrity / K10 (AMB-22 (e))
BP = 1e-4


@dataclass(frozen=True)
class Choice:
    arm: str
    h: int
    lbs: dict[tuple[str, int], float]
    ell: int
    B: int
    seed: int
    reference_invalid: tuple[str, ...] = ()


@dataclass(frozen=True)
class Invalid:
    reason: str
    runs: tuple[str, ...]  # "<arm>@<h>h: <detail>"
    reference_invalid: tuple[str, ...] = field(default=())


def _detail(run: BookRun) -> str:
    return str(run.invalid) + (f" ({run.invalid_detail})" if run.invalid_detail else "")


def _ledger_of(obj: BookRun | Ledger) -> Ledger:
    return obj if isinstance(obj, Ledger) else account(obj)


def choose_from_lbs(lbs: Mapping[tuple[str, int], float], *, ell: int = FROZEN.stats.sel_block,
                    B: int = FROZEN.stats.B, seed: int = stats.SEED) -> Choice:
    """S-10.6 steps 3–5 on the six candidate LBs (daily-return units)."""
    if set(lbs) != set(CANDIDATES):
        raise ValueError(f"LBs must cover exactly the six candidates {CANDIDATES}")
    for k, v in lbs.items():
        if not math.isfinite(v):
            raise ValueError(f"non-finite LB for {k}")
    best = {a: max(lbs[(a, h)] for h in HORIZONS) for a in ML_ARMS}
    fam = "A3" if (best["A3"] - best["A2"]) / BP >= FROZEN.select.a3_margin_bpd else "A2"
    tied = [h for h in HORIZONS if (best[fam] - lbs[(fam, h)]) / BP <= FROZEN.select.tie_bpd]
    return Choice(fam, max(tied), dict(lbs), int(ell), int(B), int(seed))


def choose(runs: Mapping[tuple[str, int], BookRun | Ledger], *, B: int = FROZEN.stats.B,
           seed: int = stats.SEED) -> Choice | Invalid:
    """S-10.6: validity precondition, LB (ℓ = 14), simplicity rule and tie-break."""
    missing = [k for k in REQUIRED if k not in runs]
    if missing:
        raise ValueError(f"required W_DEV runs missing: {missing}")
    unknown = [k for k in runs if k not in REQUIRED and k[0] not in REFERENCE_ARMS]
    if unknown:
        raise ValueError(f"unknown runs: {unknown}")
    ref_invalid = tuple(f"{a}@{h}h: {_detail(r)}" for (a, h), r in sorted(runs.items())
                        if a in REFERENCE_ARMS and isinstance(r, BookRun) and not r.valid)
    bad = [f"{a}@{h}h: {_detail(runs[(a, h)])}" for a, h in REQUIRED
           if isinstance(runs[(a, h)], BookRun) and not runs[(a, h)].valid]
    if bad:
        return Invalid(INVALID_DEV, tuple(bad), ref_invalid)
    leds = {k: _ledger_of(runs[k]) for k in REQUIRED}
    broken = [f"{a}@{h}h" for (a, h), led in leds.items() if not (k10_ok(led) and daily_ok(led))]
    if broken:
        return Invalid(INVALID_K10, tuple(broken), ref_invalid)
    ell = FROZEN.stats.sel_block
    lbs = {k: stats.lower_bound(stats.ledger_returns(leds[k]), (ell,), B, seed) for k in CANDIDATES}
    c = choose_from_lbs(lbs, ell=ell, B=B, seed=seed)
    return Choice(c.arm, c.h, c.lbs, c.ell, c.B, c.seed, ref_invalid)


KILL_MEAN = "mean daily net <= 0"
KILL_FNET = "F_net <= 0"
KILL_RATIO = "C_exec / F_net > 1.0"
KILL_CAC = "CAC <= 0"
KILL_FOLDS = "fewer than 30 % of outer folds have positive net"


def dev_kill(ledger_oof: Ledger, fold_nets: Sequence[float]) -> list[str]:
    """S-10.7 triggers (empty = no kill) for ML* out-of-fold on ``W_DEV``. ``fold_nets``: the net of each
    outer test fold (e.g. ``accounting.period_net`` over the fold edges). C_exec / F_net is evaluated only for
    F_net > 0 (F_net ≤ 0 is its own trigger); with F_net > 0 it implies CAC < 0."""
    nets = [float(x) for x in fold_nets]
    if not nets or not all(math.isfinite(x) for x in nets):
        raise ValueError("fold_nets must be a non-empty list of finite values")
    out: list[str] = []
    if not mean_daily_net(ledger_oof) > 0:
        out.append(KILL_MEAN)
    f = ledger_oof.F_net
    if not f > 0:
        out.append(KILL_FNET)
    elif ledger_oof.C_exec / f > FROZEN.kill.cexec_over_fnet:
        out.append(KILL_RATIO)
    if not ledger_oof.CAC > 0:
        out.append(KILL_CAC)
    if sum(x > 0 for x in nets) / len(nets) < FROZEN.kill.min_pos_fold_share:
        out.append(KILL_FOLDS)
    return out


__all__ = ["CANDIDATES", "Choice", "INVALID_DEV", "INVALID_K10", "Invalid", "REQUIRED", "choose",
           "choose_from_lbs", "dev_kill"]
