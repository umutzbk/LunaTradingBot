"""Instance-restricted reference prices and funding settlements, shared by labels and the executed book
(spec S-1.5, S-2.6, S-4.4, S-4.6, S-5.2, S-5.4, S-5.5, E10, DEC-29).

* ``restrict`` cuts one instrument's frames to ONE instance (E10) and to timestamps strictly before the
  window end (S-1.5); every price, proxy and funding value of a label or a book fill comes from such a view.
* ``ref_price``: P(tau) (S-5.2) with the S-5.4 data-gap rule (the open of the first usable 1-minute bar
  opening in [tau, tau + 60 min]; the fill time stays tau, AMB-10 (a)).
* ``funding_schedule``: the instance's funding records known by a time, annotated point-in-time
  (``universe.pit_funding``), with the DEC-29 missing-settlement test over a window (head record, adjacent
  gaps, tail), invalid records (S-2.6) and the S-4.6 mark chain (record -> mark 15m close -> last-price 15m
  close; only finite prices > 0). Labels sum it (S-5.3); the book books each settlement at its own time.
No price is interpolated or invented: a missing value is ``None`` or a named problem.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import pandas as pd

from luna.v7.data.instances import Instance
from luna.v7.data.intervals import u3_gap
from luna.v7.market import P, first_bar_within, mark_at_settle
from luna.v7.timeutil import MIN, H, ns_array
from luna.v7.universe import pit_funding

FUNDING_PROBLEMS = frozenset({"funding_missing", "funding_invalid", "mark_missing"})


def pos(x: object) -> float | None:
    """``float(x)`` if finite and > 0, else None."""
    try:
        v = float(x)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) and v > 0 else None


def trim(df: pd.DataFrame, col: str, lo: int | None, hi: int) -> pd.DataFrame:
    """Rows with lo <= df[col] < hi (lo None: no lower bound)."""
    if len(df) == 0:
        return df
    t = ns_array(df[col], col)
    m = t < hi
    if lo is not None:
        m &= t >= lo
    return df[m]


@dataclass(frozen=True)
class InstView:
    """An instrument restricted to one instance and to timestamps before the window end."""

    inst: Instance
    b1: pd.DataFrame
    b15: pd.DataFrame
    m15: pd.DataFrame
    funding: pd.DataFrame
    end: int  # window end


def restrict(bars1m: pd.DataFrame, bars15: pd.DataFrame, mark15: pd.DataFrame, funding: pd.DataFrame,
             cur: Instance, w_end: int) -> InstView:
    hi = w_end if cur.last_trade_ts is None else min(w_end, cur.last_trade_ts)
    return InstView(
        cur,
        trim(bars1m, "open_ts", cur.first_bar_ts, hi),
        trim(bars15, "open_ts", cur.first_bar_ts, hi),
        trim(mark15, "open_ts", cur.first_bar_ts, hi),
        trim(funding, "funding_ts", None, w_end),  # pit_funding applies the instance start
        w_end,
    )


def usable_open(b1: pd.DataFrame) -> pd.DataFrame:
    o = b1["o"].to_numpy(np.float64)
    return b1[np.isfinite(o) & (o > 0)]


def ref_price(x: InstView, tau: int, before_gap=None) -> tuple[float, bool] | None:
    """Reference price at tau (S-5.2) with the S-5.4 data-gap rule; (price, gap_used) or None.
    ``before_gap`` (the guard for the extended span) runs before the gap search reads past tau + 1 min."""
    p = P(x.b1, x.b15, tau)
    if p is not None:
        return p, False
    if before_gap is not None:
        before_gap()
    g = first_bar_within(usable_open(x.b1), tau, H)
    if g is None:
        return None
    return float(g[1]), True


def positive_close(bars: pd.DataFrame) -> pd.DataFrame:
    """Bars whose close is finite and > 0 (a mark fallback never uses an unusable price)."""
    if len(bars) == 0:
        return pd.DataFrame({"open_ts": pd.Series(dtype="int64"), "c": pd.Series(dtype="float64")})
    c = bars["c"].to_numpy(np.float64)
    return bars[np.isfinite(c) & (c > 0)]


class FundingProblem(Exception):
    def __init__(self, reason: str) -> None:
        if reason not in FUNDING_PROBLEMS:
            raise ValueError(f"unknown funding problem {reason!r}")
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class FundingSchedule:
    """One instance's funding records known by ``known_by`` (``pit_funding``), sorted by ``funding_ts``."""

    ts: np.ndarray
    rate: np.ndarray
    nom: np.ndarray
    invalid: np.ndarray
    raw_marks: dict[int, object]
    m15: pd.DataFrame
    l15: pd.DataFrame

    def mark(self, i: int) -> float | None:
        """S-4.6 mark at settlement ``ts[i]``: record -> mark 15m close -> last-price 15m close."""
        k = int(self.ts[i])
        return pos(mark_at_settle({"mark_at_settle": pos(self.raw_marks.get(k))}, self.m15, self.l15, k))

    def usable(self, i: int) -> bool:
        """A valid record (S-2.6) with a finite rate."""
        return bool(not self.invalid[i] and np.isfinite(self.rate[i]))

    def check(self, lo: int, hi: int) -> str | None:
        """The first problem of the settlements in (lo, hi], or None (S-5.5, DEC-29, S-2.6, S-4.6).

        Missing settlement (gap rule incl. head = last record <= lo and tail up to hi) -> funding_missing;
        an invalid record or a non-finite rate -> funding_invalid; no usable mark -> mark_missing."""
        ts, nom = self.ts, self.nom
        if len(ts) == 0:
            return "funding_missing"
        p = int(np.searchsorted(ts, lo, "right")) - 1
        q = int(np.searchsorted(ts, hi, "right"))
        if p < 0:
            return "funding_missing"  # no head record: a missing settlement cannot be excluded
        for i in range(p + 1, q):
            if u3_gap(nom[i - 1], nom[i], int(ts[i] - ts[i - 1])):
                return "funding_missing"
        if u3_gap(nom[q - 1], nom[q - 1], int(hi - ts[q - 1])):
            return "funding_missing"
        if self.invalid[p + 1:q].any() or not np.isfinite(self.rate[p + 1:q]).all():
            return "funding_invalid"
        for i in range(p + 1, q):
            if self.mark(i) is None:
                return "mark_missing"
        return None

    def window_sum(self, lo: int, hi: int) -> float:
        """Sum of r_k * M(tau_k) over settlements in (lo, hi]; raises ``FundingProblem``."""
        why = self.check(lo, hi)
        if why is not None:
            raise FundingProblem(why)
        p = int(np.searchsorted(self.ts, lo, "right"))
        q = int(np.searchsorted(self.ts, hi, "right"))
        return float(sum(float(self.rate[i]) * self.mark(i) for i in range(p, q)))  # type: ignore[operator]


def funding_schedule(x: InstView, known_by: int) -> FundingSchedule:
    """The instance's funding records with ``known_at`` <= ``known_by`` (instance start applied)."""
    f = x.funding
    fts = ns_array(f["funding_ts"], "funding_ts") if len(f) else np.empty(0, np.int64)
    if np.unique(fts).size != fts.size:
        raise ValueError("funding: duplicate funding_ts")
    a = pit_funding(f, x.inst, known_by + MIN)
    marks: dict[int, object] = {}
    if "mark_at_settle" in f.columns:
        marks = dict(zip(fts.tolist(), f["mark_at_settle"].tolist(), strict=True))
    if len(a) == 0:
        e = np.empty(0)
        return FundingSchedule(np.empty(0, np.int64), e, e, np.empty(0, bool), marks,
                               positive_close(x.m15), positive_close(x.b15))
    return FundingSchedule(
        ns_array(a["funding_ts"], "funding_ts"),
        a["rate"].astype("Float64").to_numpy(dtype=float, na_value=np.nan),
        a["nominal_h"].astype("Float64").to_numpy(dtype=float, na_value=np.nan),
        a["invalid"].to_numpy(bool),
        marks,
        positive_close(x.m15),
        positive_close(x.b15),
    )
