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
* Real-scale compute (SC-01 part 2, AMB-38): ``trim`` returns a positional slice when the column is sorted;
  ``InstIndex`` (one per ``InstView``, built on first use) answers HALTED, P(tau) with the data-gap rule,
  h_fill, the first executable bar and the hourly beta inputs with ``searchsorted`` on the view's sorted
  arrays, and ``funding_schedule`` reuses the view's per-instance annotation, marks and mark-chain arrays.
  Each returns what the frame-level definition returns; when the arrays do not qualify (unsorted, duplicate
  labels, non-numeric) the frame-level function runs.
* Lazy access (red-team r2 MJ1, AMB-38 (h)): the 1-minute input may be a ``lazy1m.Lazy1m`` instead of a
  frame, and (round 2) the 15m R3 / R4 mark inputs ``lazy1m.LazyBars``; ``restrict`` narrows them like frames
  and ``InstIndex`` answers every rule from windows of them: P(tau) (the bar at tau), h_fill (the rows of
  [tau - 24 h 15 min, tau)), the beta-hat hourly inputs (the bars closing at the 30 d hour grid of t,
  ``beta_arrays``) and the funding marks (the bar ending at each settlement). Data the resident index would
  not take is refused (``ValueError``): no frame-level fallback without the frame.
No price is interpolated or invented: a missing value is ``None`` or a named problem.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from luna.v7.constants import FROZEN
from luna.v7.data.instances import Instance
from luna.v7.data.intervals import u3_gap
from luna.v7.lazy1m import Lazy1m, LazyBars
from luna.v7.market import P, first_bar_within, first_executable_after, halted, mark_at_settle
from luna.v7.pitindex import Hourly
from luna.v7.spread import half_spread, half_spread_arrays
from luna.v7.timeutil import MIN, H, ns_array
from luna.v7.universe import _build_prefix, pit_funding

FUNDING_PROBLEMS = frozenset({"funding_missing", "funding_invalid", "mark_missing"})


def pos(x: object) -> float | None:
    """``float(x)`` if finite and > 0, else None."""
    try:
        v = float(x)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) and v > 0 else None


def trim(df: pd.DataFrame, col: str, lo: int | None, hi: int) -> pd.DataFrame:
    """Rows with lo <= df[col] < hi (lo None: no lower bound); a positional slice when ``col`` is sorted (the
    same rows in the same order, Task 26b), else the boolean mask."""
    if len(df) == 0:
        return df
    t = ns_array(df[col], col)
    if len(t) < 2 or bool((t[1:] >= t[:-1]).all()):
        a = 0 if lo is None else int(np.searchsorted(t, lo, "left"))
        return df.iloc[a:max(a, int(np.searchsorted(t, hi, "left")))]
    m = t < hi
    if lo is not None:
        m &= t >= lo
    return df[m]


@dataclass(frozen=True)
class InstView:
    """An instrument restricted to one instance and to timestamps before the window end."""

    inst: Instance
    b1: pd.DataFrame | Lazy1m  # a frame, or the lazy 1-minute window (red-team r2 MJ1)
    b15: pd.DataFrame | LazyBars  # round 2: the lazy 15m windows
    m15: pd.DataFrame | LazyBars
    funding: pd.DataFrame
    end: int  # window end
    # ``InstIndex`` / funding parts (Task 26b); not an init field: ``dataclasses.replace`` starts a new one
    memo: dict = field(default_factory=dict, init=False, compare=False, repr=False)

    def ix(self) -> InstIndex:
        """The view's ``InstIndex`` (built once; the frames must not be mutated afterwards)."""
        if "ix" not in self.memo:
            self.memo["ix"] = InstIndex(self)
        return self.memo["ix"]


def _cut(df, lo: int, hi: int):
    """``trim(df, "open_ts", lo, hi)`` for a frame, ``window`` for a lazy input."""
    return df.window(lo, hi) if isinstance(df, Lazy1m | LazyBars) else trim(df, "open_ts", lo, hi)


def restrict(bars1m: pd.DataFrame | Lazy1m, bars15: pd.DataFrame | LazyBars, mark15: pd.DataFrame | LazyBars,
             funding: pd.DataFrame, cur: Instance, w_end: int) -> InstView:
    hi = w_end if cur.last_trade_ts is None else min(w_end, cur.last_trade_ts)
    return InstView(
        cur,
        _cut(bars1m, cur.first_bar_ts, hi),
        _cut(bars15, cur.first_bar_ts, hi),
        _cut(mark15, cur.first_bar_ts, hi),
        trim(funding, "funding_ts", None, w_end),  # pit_funding applies the instance start
        w_end,
    )


def usable_open(b1: pd.DataFrame) -> pd.DataFrame:
    o = b1["o"].to_numpy(np.float64)
    return b1[np.isfinite(o) & (o > 0)]


def ref_price(x: InstView, tau: int, before_gap=None) -> tuple[float, bool] | None:
    """Reference price at tau (S-5.2) with the S-5.4 data-gap rule; (price, gap_used) or None.
    ``before_gap`` (the guard for the extended span) runs before the gap search reads past tau + 1 min."""
    ix = x.ix()
    p = ix.P(tau) if ix.ok else P(x.b1, x.b15, tau)
    if p is not None:
        return p, False
    if before_gap is not None:
        before_gap()
    g = ix.first_bar_within(tau, H) if ix.ok else first_bar_within(usable_open(x.b1), tau, H)
    if g is None:
        return None
    return float(g[1]), True


def _int64(df: pd.DataFrame, col: str) -> np.ndarray:
    s = df[col]
    if s.dtype != np.int64:
        raise TypeError(f"{col}: not int64")
    return ns_array(s.to_numpy(), col)


class InstIndex:
    """Sorted-array access to one ``InstView`` (Task 26b; module docstring). ``ok``: the 1m and 15m frames
    have int64 event times strictly increasing, unique index labels, valid ``known_at`` (15m) and numeric
    price / volume columns; then every method equals its frame-level definition:

    * ``halted(tau)`` = ``market.halted(x.b1, tau)`` (prefix counts of volume > 0 bars);
    * ``P(tau)`` = ``market.P(x.b1, x.b15, tau)`` and ``first_bar_within(tau, w)`` =
      ``market.first_bar_within(usable_open(x.b1), tau, w)``;
    * ``half_spread(tau)`` = ``spread.half_spread(x.b15, tau)`` (on the rows opening in [tau - 24 h 15 min,
      tau), the only rows the S-4.5 window reads);
    * ``first_executable_after(tau, hi)`` = ``market.first_executable_after(trim(x.b1, "open_ts", None, hi),
      tau, x.inst)``;
    * ``hourly`` = the ``pitindex.Hourly`` arrays of ``x.b15`` (beta-hat with ``beta.beta_hat_sorted``)."""

    def __init__(self, x: InstView) -> None:
        self.x = x
        self.ok = False
        self.lazy: Lazy1m | None = x.b1 if isinstance(x.b1, Lazy1m) else None
        self.lazy15: LazyBars | None = x.b15 if isinstance(x.b15, LazyBars) else None
        if self.lazy15 is not None and self.lazy is None:
            raise ValueError("lazy 15m bars need the lazy 1-minute input (one outcome universe)")
        if self.lazy is not None:
            self._init_lazy(x)
            return
        try:
            b1, b15 = x.b1, x.b15
            t1, t15 = _int64(b1, "open_ts"), _int64(b15, "open_ts")
            ka15 = ns_array(b15["known_at"], "known_at") if len(b15) else np.empty(0, np.int64)
            o1, c1 = b1["o"].to_numpy(np.float64), b1["c"].to_numpy(np.float64)
            v1 = b1["vol_base"].to_numpy(np.float64)
            o15, c15 = b15["o"].to_numpy(np.float64), b15["c"].to_numpy(np.float64)
            b15["h"].to_numpy(np.float64)
            b15["l"].to_numpy(np.float64)
        except (ValueError, TypeError, KeyError, OverflowError):
            return
        if not (b1.index.is_unique and b15.index.is_unique and (np.diff(t1) > 0).all()
                and (np.diff(t15) > 0).all()):
            return
        self.t1, self.o1, self.c1, self.v1, self.t15, self.o15, self.c15 = t1, o1, c1, v1, t15, o15, c15
        self.ka15 = ka15
        self.cnt1 = np.concatenate([[0], np.cumsum(v1 > 0)]).astype(np.int64)
        self.usable = np.flatnonzero(np.isfinite(o1) & (o1 > 0))
        self.ok = True

    def _init_lazy(self, x: InstView) -> None:
        """The 15m arrays as in the resident case; the 1-minute rules read ``self.lazy``. A 15m frame that
        does not qualify is refused (the frame-level fallback needs the resident 1-minute frame)."""
        b15 = x.b15
        if self.lazy15 is not None:  # every 15m rule reads windows of it (``lazy1m.LazyBars`` checks them)
            self.ok = True
            return
        try:
            t15 = _int64(b15, "open_ts")
            ka15 = ns_array(b15["known_at"], "known_at") if len(b15) else np.empty(0, np.int64)
            o15, c15 = b15["o"].to_numpy(np.float64), b15["c"].to_numpy(np.float64)
            b15["h"].to_numpy(np.float64)
            b15["l"].to_numpy(np.float64)
            ok = b15.index.is_unique and bool((np.diff(t15) > 0).all())
        except (ValueError, TypeError, KeyError, OverflowError):
            ok = False
        if not ok:
            sym = self.lazy.symbol if self.lazy is not None else "?"
            raise ValueError(f"{sym}: the lazy 1-minute path needs 15m bars with int64 strictly increasing "
                             "open_ts, unique labels and numeric prices (use the resident universe)")
        self.t15, self.o15, self.c15, self.ka15 = t15, o15, c15, ka15
        self.ok = True

    def halted(self, tau: int) -> bool:
        if self.lazy is not None:
            return self.lazy.halted(tau)
        lo = int(np.searchsorted(self.t1, tau - H, "left"))
        hi = int(np.searchsorted(self.t1, tau, "left"))
        return bool(self.cnt1[hi] - self.cnt1[lo] == 0)

    @staticmethod
    def _open_at(t: np.ndarray, o: np.ndarray, tau: int) -> float | None:
        i = int(np.searchsorted(t, tau, "left"))
        if i >= len(t) or t[i] != tau or not np.isfinite(float(o[i])):
            return None
        return float(o[i])

    def P(self, tau: int) -> float | None:  # noqa: N802
        v = self.lazy.open_at(tau) if self.lazy is not None else self._open_at(self.t1, self.o1, tau)
        if v is None or v <= 0:
            v = self.lazy15.row_at(tau, "o") if self.lazy15 is not None else self._open_at(self.t15, self.o15,
                                                                                            tau)
        return v if v is not None and v > 0 else None

    def first_bar_within(self, tau: int, window_ns: int) -> tuple[int, float] | None:
        if self.lazy is not None:
            return self.lazy.first_usable_open(tau, window_ns)
        tu = self.t1[self.usable]
        j = int(np.searchsorted(tu, tau, "left"))
        if j >= len(tu) or tu[j] > tau + window_ns:
            return None
        return int(tu[j]), float(self.o1[self.usable[j]])

    def half_spread(self, tau: int) -> float | None:
        if self.lazy15 is not None:
            return x_half_spread_lazy(self.lazy15, tau)
        a = int(np.searchsorted(self.t15, tau - FROZEN.spread.window_ns, "left"))
        b = int(np.searchsorted(self.t15, tau, "left"))
        return half_spread(self.x.b15.iloc[a:b], tau)

    def first_executable_after(self, tau: int, hi: int) -> tuple[int, float] | None:
        inst = self.x.inst
        if self.lazy is not None:
            return self.lazy.first_executable_after(tau, hi, inst)
        a = int(np.searchsorted(self.t1, max(int(tau), int(inst.first_bar_ts)), "left"))
        b = int(np.searchsorted(self.t1, hi, "left"))
        t, v, c = self.t1[a:b], self.v1[a:b], self.c1[a:b]
        ok = (v > 0) & np.isfinite(c) & (c > 0)
        if inst.last_trade_ts is not None:
            ok &= t + MIN <= inst.last_trade_ts
        i = np.flatnonzero(ok)
        return None if len(i) == 0 else (int(t[i[0]]) + MIN, float(c[i[0]]))

    def beta_arrays(self, t: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """(hours, close, known_at) for ``beta.beta_hat_sorted`` at t: the view's whole hourly series, or
        (lazy) the hourly closes of the bars opening in [last - 30 d - 1 h, last + 1 h), last = the hour of
        t - 1 min: every hour the estimate reads (the 30 d grid and the hour before it), each hour's value
        being its own bar's (``pitindex.Hourly``), so the same lookups."""
        if self.lazy15 is None:
            h = self.hourly
            return h.hours, h.close, h.ka
        last = ((int(t) - MIN) // H) * H
        d = self.lazy15.arrays(last - FROZEN.beta.days * 24 * H - H, last + H, ("open_ts", "c", "known_at"))
        ot = np.asarray(d["open_ts"])
        ka = ns_array(np.asarray(d["known_at"]), "known_at") if len(ot) else np.empty(0, np.int64)
        hv = Hourly.build(ot, np.asarray(d["c"], np.float64), ka)
        return hv.hours, hv.close, hv.ka

    @property
    def hourly(self) -> Hourly:
        if "hourly" not in self.x.memo:
            self.x.memo["hourly"] = Hourly.build(self.t15, self.c15, self.ka15)
        return self.x.memo["hourly"]


def x_half_spread_lazy(b15: LazyBars, tau: int) -> float | None:
    """``spread.half_spread`` on the lazy 15m rows of [tau - 24 h 15 min, tau) (``half_spread_arrays``)."""
    d = b15.arrays(int(tau) - FROZEN.spread.window_ns, int(tau), ("open_ts", "known_at", "h", "l", "c"))
    return half_spread_arrays(d["open_ts"], d["known_at"], d["h"], d["l"], d["c"], int(tau))


def x_halted(x: InstView, tau: int) -> bool:
    """``market.halted(x.b1, tau)``."""
    ix = x.ix()
    return ix.halted(tau) if ix.ok else halted(x.b1, tau)


def x_half_spread(x: InstView, tau: int) -> float | None:
    """``spread.half_spread(x.b15, tau)``."""
    ix = x.ix()
    return ix.half_spread(tau) if ix.ok else half_spread(x.b15, tau)


def x_first_executable_after(x: InstView, tau: int, hi: int) -> tuple[int, float] | None:
    """``market.first_executable_after(trim(x.b1, "open_ts", None, hi), tau, x.inst)``."""
    ix = x.ix()
    if ix.ok:
        return ix.first_executable_after(tau, hi)
    return first_executable_after(trim(x.b1, "open_ts", None, hi), tau, x.inst)


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
    chain: tuple[np.ndarray, ...] | LazyMarks | None = field(default=None, compare=False, repr=False)  # 26b

    def mark(self, i: int) -> float | None:
        """S-4.6 mark at settlement ``ts[i]``: record -> mark 15m close -> last-price 15m close. ``chain``:
        (open_ts, close) arrays of ``m15`` and ``l15`` with strictly increasing open_ts (``funding_schedule``;
        the same lookup with ``searchsorted``)."""
        k = int(self.ts[i])
        if isinstance(self.chain, LazyMarks):  # round 2: the lazy 15m windows (the same chain)
            m = pos(self.raw_marks.get(k))
            return m if m is not None else self.chain.close_at(k - 15 * MIN)
        if self.chain is None:
            return pos(mark_at_settle({"mark_at_settle": pos(self.raw_marks.get(k))}, self.m15, self.l15, k))
        m = pos(self.raw_marks.get(k))
        if m is not None:
            return m
        at = k - 15 * MIN
        for t, c in (self.chain[:2], self.chain[2:]):
            j = int(np.searchsorted(t, at, "left"))
            if j < len(t) and t[j] == at and math.isfinite(v := float(c[j])):
                return pos(v)
        return None

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


class LazyMarks:
    """The S-4.6 15m part of the mark chain on lazy windows (red-team r2 MJ1 round 2): the close of the mark
    bar opening at ``at`` (= settlement - 15 min) when finite and > 0, else that of the last-price bar, else
    None: the ``positive_close`` arrays' lookup of the resident chain (a bar with an unusable close is not in
    them)."""

    BLOCK = 8 * 24 * H  # rows read per lookup block (the settlements of a label / episode reuse it)

    def __init__(self, m15: LazyBars, l15: LazyBars) -> None:
        self.m15, self.l15 = m15, l15
        self._blk: dict[int, tuple[int, np.ndarray, np.ndarray]] = {}

    def _block(self, k: int, bars: LazyBars, at: int) -> tuple[np.ndarray, np.ndarray]:
        b0 = int(at) // self.BLOCK * self.BLOCK
        got = self._blk.get(k)
        if got is None or got[0] != b0:
            d = bars.arrays(b0, b0 + self.BLOCK, ("open_ts", "c"))
            got = (b0, np.asarray(d["open_ts"]), np.asarray(d["c"], dtype=np.float64))
            self._blk[k] = got
        return got[1], got[2]

    def close_at(self, at: int) -> float | None:
        for k, bars in enumerate((self.m15, self.l15)):
            t, c = self._block(k, bars, at)
            j = int(np.searchsorted(t, at, "left"))
            if j < len(t) and t[j] == at and math.isfinite(v := float(c[j])) and v > 0:
                return v
        return None


def _schedule_parts(x: InstView) -> tuple | None:
    """Per view (memo): the marks, the positive-close mark chain frames with their arrays (None when an
    open_ts column is not strictly increasing int64) and the per-instance annotation prefix arrays
    (``universe._build_prefix``; None: no fast path). Refused (not memoised) on duplicate funding_ts."""
    if "sched" in x.memo:
        return x.memo["sched"]
    f = x.funding
    fts = ns_array(f["funding_ts"], "funding_ts") if len(f) else np.empty(0, np.int64)
    if np.unique(fts).size != fts.size:
        raise ValueError("funding: duplicate funding_ts")
    marks: dict[int, object] = {}
    if "mark_at_settle" in f.columns:
        marks = dict(zip(fts.tolist(), f["mark_at_settle"].tolist(), strict=True))
    lazy = isinstance(x.m15, LazyBars) or isinstance(x.b15, LazyBars)
    if lazy and not (isinstance(x.m15, LazyBars) and isinstance(x.b15, LazyBars)):
        raise ValueError("funding marks: the mark and last-price 15m inputs must both be lazy or both frames")
    m15 = positive_close(x.m15.empty if lazy else x.m15)  # type: ignore[union-attr]
    l15 = positive_close(x.b15.empty if lazy else x.b15)  # type: ignore[union-attr]
    chain: tuple | LazyMarks | None = LazyMarks(x.m15, x.b15) if lazy else None  # type: ignore[arg-type]
    try:
        if lazy:
            raise TypeError  # no arrays: the lazy chain above
        tm, tl = _int64(m15, "open_ts"), _int64(l15, "open_ts")
        if (np.diff(tm) > 0).all() and (np.diff(tl) > 0).all():
            chain = (tm, m15["c"].to_numpy(np.float64), tl, l15["c"].to_numpy(np.float64))
    except (ValueError, TypeError, KeyError, OverflowError):
        chain = chain if lazy else None
    pre = _build_prefix(f, x.inst) if len(f) else None
    arrs = None
    if pre is not None:
        a = pre.ann
        arrs = (pre, ns_array(a["funding_ts"], "funding_ts"),
                a["rate"].astype("Float64").to_numpy(dtype=float, na_value=np.nan),
                a["nominal_h"].astype("Float64").to_numpy(dtype=float, na_value=np.nan),
                a["invalid"].to_numpy(bool))
    x.memo["sched"] = (marks, m15, l15, chain, arrs)
    return x.memo["sched"]


def funding_schedule(x: InstView, known_by: int) -> FundingSchedule:
    """The instance's funding records with ``known_at`` <= ``known_by`` (instance start applied). Task 26b:
    the view's marks / mark chain / annotation are built once (``_schedule_parts``); the PIT records are the
    annotation prefix when ``universe.pit_funding``'s incremental condition holds, else ``pit_funding``."""
    marks, m15, l15, chain, arrs = _schedule_parts(x)  # type: ignore[misc]
    if arrs is not None:
        pre, ts, rate, nom, inv = arrs
        ok = pre.ka <= int(known_by)
        k = int(ok.sum())
        if k > 0 and ok[:k].all():
            return FundingSchedule(ts[:k], rate[:k], nom[:k], inv[:k], marks, m15, l15, chain)
    a = pit_funding(x.funding, x.inst, known_by + MIN)
    if len(a) == 0:
        e = np.empty(0)
        return FundingSchedule(np.empty(0, np.int64), e, e, np.empty(0, bool), marks, m15, l15, chain)
    return FundingSchedule(
        ns_array(a["funding_ts"], "funding_ts"),
        a["rate"].astype("Float64").to_numpy(dtype=float, na_value=np.nan),
        a["nominal_h"].astype("Float64").to_numpy(dtype=float, na_value=np.nan),
        a["invalid"].to_numpy(bool),
        marks, m15, l15, chain,
    )


def _funding_schedule_reference(x: InstView, known_by: int) -> FundingSchedule:
    """The direct definition (kept for the equivalence tests)."""
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
