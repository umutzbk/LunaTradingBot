"""Data windows, decision times and walk-forward folds (spec S-1.2, S-1.4, S-1.8, S-10.2, S-10.4)."""

from __future__ import annotations

import functools
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from luna.v7 import release
from luna.v7.constants import FROZEN
from luna.v7.spec import v6_artifacts
from luna.v7.timeutil import MIN, D, to_ns

D0 = to_ns(FROZEN.d0)
W_DEV = (D0, to_ns(FROZEN.w_dev_end))  # S-1.4
W_HOLD = (to_ns(FROZEN.w_dev_end), to_ns(FROZEN.w_hold_end))  # S-1.4, sealed
MIN_FOLDS = FROZEN.cv.min_folds  # S-10.2: fewer folds is STAGE-0 FAIL


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
    m_sel, m_cal = FROZEN.cv.block_months
    c12, c6 = _shift_months(cutoff, -m_sel), _shift_months(cutoff, -m_cal)
    return (d0, c12), (c12, c6), (c6, cutoff)


def outer_folds(d0: int) -> list[Fold]:
    """Consecutive calendar quarters, the first starting >= D0 + 24 mo, ending at W_DEV end (S-10.2)."""
    end = W_DEV[1]
    earliest = _shift_months(d0, FROZEN.cv.first_test_months)
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


def hold_quarter_edges() -> tuple[int, ...]:
    """The 5 edges of the 4 calendar quarters of W_HOLD (S-13.2 P5 "the 4 holdout quarters"; red-team r2
    MJ3: the only quarter edges ``runner_select.holdout_inputs`` accepts). W_HOLD starts and ends on calendar
    quarter starts; anything else raises."""
    lo, hi = W_HOLD
    edges = [lo]
    while edges[-1] < hi:
        edges.append(_shift_months(edges[-1], 3))
    for e in edges:
        ts = pd.Timestamp(e, tz="UTC")
        if ts != ts.normalize() or ts.day != 1 or ts.month not in (1, 4, 7, 10):
            raise ValueError(f"W_HOLD edge {ts} is not a calendar quarter start")
    if edges[-1] != hi or len(edges) != 5:
        raise ValueError("W_HOLD is not 4 calendar quarters")
    return tuple(int(e) for e in edges)


def final_blocks() -> Fold:
    """Final model before W_HOLD (S-10.4 step 6); test = W_HOLD."""
    c = W_HOLD[0]
    tr, sel, cal = _blocks(c, D0)
    return Fold(0, c, W_HOLD[1], tr, sel, cal)


def _canonical_repo() -> Path:
    """The canonical code repository: the guards' single location seam (``guard._roots``; tests monkeypatch
    it). No caller repository is taken (red-team r2 MJ5, SC-06)."""
    from luna.v7 import guard  # local: guard imports this module

    return Path(guard._roots()[0])


def m6() -> tuple[int, int | None]:
    """M6 = [V6 holdout start, R_V6) (S-1.2). The start comes from the hash-checked V6 artefacts; the end is
    R_V6 from the canonical, hash-checked release record (``release.r_v6``, SC-06 / AMB-34) and None while
    that record is absent or does not verify (R_V6 undetermined: M6 open-ended). Both are read from the
    canonical repository only (``guard._roots``; no caller repository, red-team r2 MJ5)."""
    repo = _canonical_repo()
    return v6_artifacts(repo).holdout_start, release.r_v6(repo)


@functools.cache
def _m6_by_repo(repo_key: str) -> tuple[int, int | None]:
    repo = Path(repo_key)
    return v6_artifacts(repo).holdout_start, release.r_v6(repo)


def m6_cached() -> tuple[int, int | None]:
    """``m6()`` read once per process and resolved canonical repository (the hash-checked V6 artefacts and
    R_V6 release record via git); a failure (SpecMismatch) is not cached. This is the canonical span every
    guard compares against (outcome guard, ``netguard.partition_m6_keys``). No caller repository (MJ5)."""
    return _m6_by_repo(release.repo_key(_canonical_repo()))


def overlaps(lo: int, hi: int, span: tuple[int, int | None]) -> bool:
    """Half-open overlap of [lo, hi) with span; a span end of None is open-ended."""
    s_lo, s_hi = span
    return lo < (np.inf if s_hi is None else s_hi) and s_lo < hi
