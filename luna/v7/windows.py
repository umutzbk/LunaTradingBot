"""Data windows, decision times and walk-forward folds (spec S-1.2, S-1.4, S-1.8, S-10.2, S-10.4)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from luna.v7.constants import FROZEN
from luna.v7.spec import v6_artifacts
from luna.v7.timeutil import MIN, D, to_ns

D0 = to_ns(FROZEN.d0)
W_DEV = (D0, to_ns(FROZEN.w_dev_end))  # S-1.4
W_HOLD = (to_ns(FROZEN.w_dev_end), to_ns(FROZEN.w_hold_end))  # S-1.4, sealed
MIN_FOLDS = 8  # S-10.2: fewer folds is STAGE-0 FAIL


@dataclass(frozen=True)
class Fold:
    fold_id: int
    test_lo: int
    test_hi: int
    tr: tuple[int, int]
    sel: tuple[int, int]
    cal: tuple[int, int]


def decision_times(lo: int, hi: int) -> np.ndarray:
    """00:05 / 08:05 / 16:05 UTC decision times in [lo, hi) (S-4.1)."""
    if hi <= lo:
        return np.empty(0, dtype=np.int64)
    days = np.arange(lo // D, (hi - 1) // D + 1, dtype=np.int64) * D
    offs = np.array(FROZEN.decision.minutes, dtype=np.int64) * MIN
    t = (days[:, None] + offs[None, :]).ravel()
    return t[(t >= lo) & (t < hi)]


def _shift_months(ns: int, months: int) -> int:
    return int((pd.Timestamp(ns, tz="UTC") + pd.DateOffset(months=months)).value)


def _blocks(cutoff: int, d0: int) -> tuple[tuple[int, int], tuple[int, int], tuple[int, int]]:
    """S-10.4: T_tr [D0, C-12mo), T_sel [C-12mo, C-6mo), T_cal [C-6mo, C)."""
    c12, c6 = _shift_months(cutoff, -12), _shift_months(cutoff, -6)
    return (d0, c12), (c12, c6), (c6, cutoff)


def outer_folds(d0: int) -> list[Fold]:
    """Consecutive calendar quarters, the first starting >= D0 + 24 mo, ending at W_DEV end (S-10.2)."""
    end = W_DEV[1]
    earliest = _shift_months(d0, 24)
    q = pd.Timestamp(earliest, tz="UTC").normalize().replace(day=1)
    while int(q.value) < earliest or q.month not in (1, 4, 7, 10):
        q = q + pd.DateOffset(months=1)
    folds: list[Fold] = []
    lo = int(q.value)
    while lo < end:
        hi = _shift_months(lo, 3)
        tr, sel, cal = _blocks(lo, d0)
        folds.append(Fold(len(folds), lo, hi, tr, sel, cal))
        lo = hi
    return folds


def stage0_fail(d0: int) -> bool:
    """STAGE-0 FAIL if the fold count is < 8 (S-10.2, DEC-15)."""
    return len(outer_folds(d0)) < MIN_FOLDS


def final_blocks() -> Fold:
    """Final model before W_HOLD (S-10.4 step 6); test = W_HOLD."""
    c = W_HOLD[0]
    tr, sel, cal = _blocks(c, D0)
    return Fold(0, c, W_HOLD[1], tr, sel, cal)


def m6(repo: Path | None = None) -> tuple[int, int | None]:
    """M6 = [V6 holdout start, R_V6); the end is None while R_V6 is undetermined (S-1.2)."""
    return v6_artifacts(repo).holdout_start, None


def overlaps(lo: int, hi: int, span: tuple[int, int | None]) -> bool:
    """Half-open overlap of [lo, hi) with span; a span end of None is open-ended."""
    s_lo, s_hi = span
    return lo < (np.inf if s_hi is None else s_hi) and s_lo < hi
