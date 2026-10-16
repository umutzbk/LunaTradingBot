"""Lazy, time-chunked 1-minute (R5) access for the outcome side: labels and the book engine (red-team r2 MJ1,
SC-01; spec §17, S-5.2, S-5.4, S-10.4, S-10.6; AMB-38 (h)).

A ``Lazy1m`` stands for one symbol's R5 rows with event time in [lo, hi) without holding them: every read
asks its ``fetch`` for the rows of a sub-window [a, b) (only the columns the rule needs), so nothing loads the
full history. ``rawstore.StoreSource`` backs it with the R5 ``TableLoader`` and a dedicated, byte-bounded
``ChunkCache`` (``LAZY_1M_CACHE_BYTES``; deterministic LRU eviction of decoded (symbol, month) chunk
columns); ``runner.InMemorySource`` with the in-memory frame (``FrameFetch``, the same protocol for the
synthetic tests).

``refs.restrict`` narrows it to one instance (``window``) like a frame, and ``refs.InstIndex`` /
``book.execution.ExecView`` answer every 1-minute rule from it with the semantics they have on the resident
sorted frame (``refs.InstIndex``, Task 26b):

* ``halted(tau)``: no volume > 0 bar opening in [tau - 60 min, tau) (S-5.4);
* ``open_at(tau)``: the open of the bar opening at tau (finite, else None; S-5.2 P(tau) 1-minute part);
* ``first_usable_open(tau, w)``: the first bar opening in [tau, tau + w] with a finite open > 0 (S-5.4 data
  gap rule);
* ``first_executable_after(tau, hi, inst)``: the first volume > 0 bar of the instance opening in
  [max(tau, first_bar_ts), hi) with a finite close > 0 that closes by the instance's last trade (S-5.4 halted
  exit), searched forward in steps of ``SCAN_STEP_NS`` (the first qualifying row of disjoint, ordered windows
  is the first qualifying row of their union: identical to the one-pass search).

Every read is checked: event times int64, strictly increasing and inside [a, b) (else ``ValueError``: the
lazy path refuses data the resident index would not take, it never falls back to a different rule).
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace

import numpy as np
import pandas as pd

from luna.v7.data.instances import Instance
from luna.v7.timeutil import MIN, D, H, ns_array

SCAN_STEP_NS = 7 * D  # forward search step of the halted-exit scan (implementation parameter, AMB-38 (h))
LAZY_1M_CACHE_BYTES = 256 * 2**20  # StoreSource's dedicated R5 chunk-cache budget (implementation parameter)
COLS = ("o", "c", "vol_base")  # the R5 columns the outcome rules read (besides open_ts)

Fetch = Callable[[int, int, tuple[str, ...]], Mapping[str, object]]
SpanOf = Callable[[int, int], "tuple[int, int] | None"]


def _empty(cols: Sequence[str]) -> dict[str, np.ndarray]:
    out: dict[str, np.ndarray] = {"open_ts": np.empty(0, np.int64)}
    out.update({c: np.empty(0, np.float64) for c in cols})
    return out


@dataclass(frozen=True)
class Lazy1m:
    """One symbol's R5 rows with lo <= open_ts < hi, read on demand (module docstring). ``fetch(a, b, cols)``
    returns {"open_ts", *cols} arrays of the rows with a <= open_ts < b, sorted; ``span_of(a, b)`` an envelope
    (min, max + 1) of those rows' event times (None: no row), used for the guarded data span."""

    symbol: str
    lo: int
    hi: int
    fetch: Fetch
    span_of: SpanOf

    def window(self, a: int | None, b: int) -> Lazy1m:
        """The rows with a <= open_ts < b (a None: no lower bound) inside this window (``refs.trim``)."""
        lo = self.lo if a is None else max(self.lo, int(a))
        return replace(self, lo=lo, hi=max(lo, min(self.hi, int(b))))

    def span(self) -> tuple[int, int] | None:
        return None if self.hi <= self.lo else self.span_of(self.lo, self.hi)

    def arrays(self, a: int, b: int, cols: Sequence[str] = COLS) -> dict[str, np.ndarray]:
        """{"open_ts": int64, col: float64} of the rows with max(a, lo) <= open_ts < min(b, hi)."""
        a, b = max(int(a), self.lo), min(int(b), self.hi)
        cols = tuple(cols)
        if a >= b:
            return _empty(cols)
        d = self.fetch(a, b, cols)
        t = d["open_ts"]
        if not isinstance(t, np.ndarray) or t.dtype != np.int64:
            raise ValueError(f"{self.symbol}.bars1m: open_ts is not int64")
        if len(t) and not (bool((t[1:] > t[:-1]).all()) and int(t[0]) >= a and int(t[-1]) < b):
            raise ValueError(f"{self.symbol}.bars1m: open_ts not strictly increasing inside [{a}, {b}) (the "
                             "lazy 1-minute path needs sorted unique bars; use the resident universe)")
        out = {"open_ts": t}
        for c in cols:
            v = np.asarray(d[c], dtype=np.float64)
            if v.shape != t.shape:
                raise ValueError(f"{self.symbol}.bars1m.{c}: {len(v)} values for {len(t)} rows")
            out[c] = v
        return out

    def frame(self, a: int, b: int) -> pd.DataFrame:
        """The rows of [a, b) as a frame (open_ts, o, c, vol_base; RangeIndex): the slices the
        ``book.execution`` rules run on."""
        return pd.DataFrame(self.arrays(a, b, COLS))

    # --- the refs.InstIndex rules on the window's rows ---------------------------------------------------

    def halted(self, tau: int) -> bool:
        v = self.arrays(int(tau) - H, int(tau), ("vol_base",))["vol_base"]
        return not bool((v > 0).any())

    def open_at(self, tau: int) -> float | None:
        d = self.arrays(int(tau), int(tau) + 1, ("o",))
        if not len(d["open_ts"]) or int(d["open_ts"][0]) != int(tau) or not np.isfinite(float(d["o"][0])):
            return None
        return float(d["o"][0])

    def first_usable_open(self, tau: int, window_ns: int) -> tuple[int, float] | None:
        d = self.arrays(int(tau), int(tau) + int(window_ns) + 1, ("o",))
        o = d["o"]
        i = np.flatnonzero(np.isfinite(o) & (o > 0))
        return None if len(i) == 0 else (int(d["open_ts"][i[0]]), float(o[i[0]]))

    def first_executable_after(self, tau: int, hi: int, inst: Instance) -> tuple[int, float] | None:
        """(fill time = bar close, close) of the first executable bar (module docstring), or None."""
        s, hi = max(int(tau), int(inst.first_bar_ts), self.lo), min(int(hi), self.hi)
        while s < hi:
            e = min(hi, s + SCAN_STEP_NS)
            d = self.arrays(s, e, ("c", "vol_base"))
            t, c, v = d["open_ts"], d["c"], d["vol_base"]
            ok = (v > 0) & np.isfinite(c) & (c > 0)
            if inst.last_trade_ts is not None:
                ok &= t + MIN <= inst.last_trade_ts
            i = np.flatnonzero(ok)
            if len(i):
                return int(t[i[0]]) + MIN, float(c[i[0]])
            s = e
        return None


class FrameFetch:
    """``Lazy1m`` fetch over an in-memory 1-minute frame (synthetic inputs, small runs): validated once
    (int64 open_ts strictly increasing, numeric o / c / vol_base; else ``ValueError``), then positional
    slices."""

    def __init__(self, df: pd.DataFrame, what: str) -> None:
        if len(df):
            if df["open_ts"].dtype != np.int64:
                raise ValueError(f"{what}: open_ts is not int64 (lazy 1-minute path)")
            t = ns_array(df["open_ts"].to_numpy(), f"{what}.open_ts")
            if not bool((t[1:] > t[:-1]).all()):
                raise ValueError(f"{what}: open_ts not strictly increasing (the lazy 1-minute path needs "
                                 "sorted unique bars; use the resident universe)")
            cols = {c: df[c].to_numpy(np.float64) for c in COLS}
        else:
            t = np.empty(0, np.int64)
            cols = {c: np.empty(0, np.float64) for c in COLS}
        self.t, self.cols = t, cols

    def __call__(self, a: int, b: int, cols: tuple[str, ...]) -> dict[str, np.ndarray]:
        i, j = int(np.searchsorted(self.t, a, "left")), int(np.searchsorted(self.t, b, "left"))
        out = {"open_ts": self.t[i:j]}
        out.update({c: self.cols[c][i:j] for c in cols})
        return out

    def span(self, a: int, b: int) -> tuple[int, int] | None:
        i, j = int(np.searchsorted(self.t, a, "left")), int(np.searchsorted(self.t, b, "left"))
        return None if j <= i else (int(self.t[i]), int(self.t[j - 1]) + 1)


def from_frame(symbol: str, df: pd.DataFrame, lo: int, hi: int) -> Lazy1m:
    """A ``Lazy1m`` over an in-memory frame, restricted to [lo, hi)."""
    f = FrameFetch(df, f"{symbol}.bars1m")
    return Lazy1m(symbol, int(lo), int(hi), f, f.span)


# ------------------------------------------------------------------------- 15m bars and aggTrades (round 2)


@dataclass(frozen=True)
class LazyBars:
    """Any sorted table of one symbol read on demand by window (red-team r2 MJ1 round 2, AMB-38 (h)): the R3
    15m bars (``bars15``), the R4 mark bars (``mark15``) and the E_M aggTrades (``aggtrades``, ``ts``) of
    the outcome side. ``fetch(a, b, cols)`` returns {ts_col, *cols} arrays (numpy or pandas extension arrays,
    the table's own dtypes) of the rows with a <= ts < b, sorted; ``frame(a, b)`` is the frame of ``cols``
    the ``refs`` / ``book.execution`` rules run on (RangeIndex). ``strict``: event times strictly increasing
    (bars) or non-decreasing (trades); any other read raises ``ValueError``. ``empty``: the zero-row frame
    (the dtypes of an empty window)."""

    symbol: str
    name: str
    lo: int
    hi: int
    fetch: Fetch
    span_of: SpanOf
    cols: tuple[str, ...]
    empty: pd.DataFrame
    ts_col: str = "open_ts"
    strict: bool = True

    def window(self, a: int | None, b: int) -> LazyBars:
        lo = self.lo if a is None else max(self.lo, int(a))
        return replace(self, lo=lo, hi=max(lo, min(self.hi, int(b))))

    def span(self) -> tuple[int, int] | None:
        return None if self.hi <= self.lo else self.span_of(self.lo, self.hi)

    def arrays(self, a: int, b: int, cols: Sequence[str] | None = None) -> dict[str, object]:
        a, b = max(int(a), self.lo), min(int(b), self.hi)
        cols = tuple(self.cols if cols is None else cols)
        if a >= b:
            return {c: self.empty[c].array for c in cols}
        d = self.fetch(a, b, tuple(c for c in cols if c != self.ts_col))
        t = d[self.ts_col]
        if not isinstance(t, np.ndarray) or t.dtype != np.int64:
            raise ValueError(f"{self.symbol}.{self.name}: {self.ts_col} is not int64")
        ok = (t[1:] > t[:-1]) if self.strict else (t[1:] >= t[:-1])
        if len(t) and not (bool(ok.all()) and int(t[0]) >= a and int(t[-1]) < b):
            raise ValueError(f"{self.symbol}.{self.name}: {self.ts_col} not sorted inside [{a}, {b}) (the "
                             "lazy path needs sorted rows; use the resident universe)")
        out: dict[str, object] = {}
        for c in cols:
            v = d[c]
            if len(v) != len(t):  # type: ignore[arg-type]
                raise ValueError(f"{self.symbol}.{self.name}.{c}: values / rows differ in length")
            out[c] = v
        return out

    def frame(self, a: int, b: int) -> pd.DataFrame:
        return pd.DataFrame(self.arrays(a, b))

    def row_at(self, tau: int, col: str) -> float | None:
        """The finite value of ``col`` in the row with ts == tau, else None (``refs.InstIndex._open_at`` on
        this table's arrays)."""
        d = self.arrays(int(tau), int(tau) + 1, (self.ts_col, col))
        t = d[self.ts_col]
        if not len(t) or int(t[0]) != int(tau):  # type: ignore[arg-type,index]
            return None
        v = float(d[col][0])  # type: ignore[index]
        return v if np.isfinite(v) else None


class FrameTable:
    """``LazyBars`` fetch over an in-memory frame: validated once (int64 event times, strictly increasing or
    non-decreasing; else ``ValueError``), then positional slices of each column's own array (dtypes kept)."""

    def __init__(self, df: pd.DataFrame, ts_col: str, cols: Sequence[str], what: str,
                 strict: bool = True) -> None:
        if len(df):
            if df[ts_col].dtype != np.int64:
                raise ValueError(f"{what}: {ts_col} is not int64 (lazy path)")
            t = ns_array(df[ts_col].to_numpy(), f"{what}.{ts_col}")
            ok = (t[1:] > t[:-1]) if strict else (t[1:] >= t[:-1])
            if not bool(ok.all()):
                raise ValueError(f"{what}: {ts_col} not sorted (the lazy path needs sorted rows; use the "
                                 "resident universe)")
        else:
            t = np.empty(0, np.int64)
        self.ts_col, self.t = ts_col, t
        self.cols = {c: (df[c].to_numpy() if isinstance(df[c].dtype, np.dtype) else df[c].array)
                     for c in cols if c != ts_col}
        self.empty = df.iloc[0:0][list(cols)].reset_index(drop=True)

    def __call__(self, a: int, b: int, cols: tuple[str, ...]) -> dict[str, object]:
        i, j = int(np.searchsorted(self.t, a, "left")), int(np.searchsorted(self.t, b, "left"))
        out: dict[str, object] = {self.ts_col: self.t[i:j]}
        out.update({c: self.cols[c][i:j] for c in cols})
        return out

    def span(self, a: int, b: int) -> tuple[int, int] | None:
        i, j = int(np.searchsorted(self.t, a, "left")), int(np.searchsorted(self.t, b, "left"))
        return None if j <= i else (int(self.t[i]), int(self.t[j - 1]) + 1)


def table_from_frame(symbol: str, name: str, df: pd.DataFrame, lo: int, hi: int, cols: Sequence[str],
                     ts_col: str = "open_ts", strict: bool = True) -> LazyBars:
    """A ``LazyBars`` over an in-memory frame, restricted to [lo, hi)."""
    f = FrameTable(df, ts_col, cols, f"{symbol}.{name}", strict)
    return LazyBars(symbol, name, int(lo), int(hi), f, f.span, tuple(cols), f.empty, ts_col, strict)


TRADE_COLS = ("ts", "price", "qty")
LAZY_AGG_CACHE_BYTES = 128 * 2**20  # aggTrades (symbol, UTC day) cache budget (implementation parameter)


class DayFetch:
    """``LazyBars`` fetch of aggTrades per (symbol, UTC day) (R10: one daily file per symbol and UTC day):
    ``day(day_start)`` returns that day's trades (ts, price, qty); each day is validated (int64 ts inside the
    day), sorted stably by ts (the resident path's ``mergesort`` order: all rows of one ts lie in one day) and
    kept in a byte-bounded LRU (``cache``: ``get`` / ``put(key, value, nbytes)``, e.g. a
    ``rawstore.ChunkCache``; deterministic eviction)."""

    def __init__(self, symbol: str, day: Callable[[int], pd.DataFrame], cache) -> None:
        self.symbol, self.day, self.cache = symbol, day, cache

    def _day(self, d: int) -> dict[str, np.ndarray]:
        key = ("aggtrades", self.symbol, int(d))
        got = self.cache.get(key)
        if got is not None:
            return got
        df = self.day(int(d))
        miss = [c for c in TRADE_COLS if c not in df.columns]
        if miss:
            raise ValueError(f"{self.symbol}.aggtrades day {d}: columns missing {miss}")
        t = ns_array(df["ts"].to_numpy(), f"{self.symbol}.aggtrades.ts") if len(df) else np.empty(0, np.int64)
        if len(t) and (int(t.min()) < d or int(t.max()) >= d + D):
            raise ValueError(f"{self.symbol}.aggtrades day {d}: rows outside the UTC day")
        o = np.argsort(t, kind="stable")
        out = {"ts": t[o], "price": df["price"].to_numpy(np.float64)[o],
               "qty": df["qty"].to_numpy(np.float64)[o]}
        self.cache.put(key, out, sum(v.nbytes for v in out.values()))
        return out

    def __call__(self, a: int, b: int, cols: tuple[str, ...]) -> dict[str, object]:
        parts: dict[str, list] = {c: [] for c in ("ts", *cols)}
        for d in range(int(a) // D * D, (int(b) - 1) // D * D + 1, D):
            x = self._day(d)
            i, j = int(np.searchsorted(x["ts"], a, "left")), int(np.searchsorted(x["ts"], b, "left"))
            for c in parts:
                parts[c].append(x[c][i:j])
        return {c: np.concatenate(v) for c, v in parts.items()}


def trades_from_days(symbol: str, day: Callable[[int], pd.DataFrame], lo: int, hi: int, cache,
                     span_of: SpanOf | None = None) -> LazyBars:
    """Lazy aggTrades of [lo, hi) from a per-UTC-day loader (``DayFetch``); ``span_of`` defaults to the window
    itself (an envelope of the rows)."""
    empty = pd.DataFrame({"ts": np.empty(0, np.int64), "price": np.empty(0), "qty": np.empty(0)})
    f = DayFetch(symbol, day, cache)
    sp = span_of if span_of is not None else (lambda a, b: (a, b) if a < b else None)
    return LazyBars(symbol, "aggtrades", int(lo), int(hi), f, sp, TRADE_COLS, empty, "ts", False)


class FrameDays:
    """An in-memory aggTrades frame as a per-UTC-day loader (synthetic inputs): the rows of the day in the
    frame's order (``DayFetch`` sorts them stably)."""

    def __init__(self, df: pd.DataFrame, what: str) -> None:
        self.df = df
        self.t = ns_array(df["ts"].to_numpy(), f"{what}.ts") if len(df) else np.empty(0, np.int64)
        if not bool((self.t[1:] >= self.t[:-1]).all()):  # the resident windows are searchsorted slices
            raise ValueError(f"{what}: ts not sorted (the lazy path needs sorted trades)")

    def __call__(self, d: int) -> pd.DataFrame:
        return self.df[(self.t >= d) & (self.t < d + D)]

    def span(self, a: int, b: int) -> tuple[int, int] | None:
        m = (self.t >= a) & (self.t < b)
        return (int(self.t[m].min()), int(self.t[m].max()) + 1) if m.any() else None



B15_COLS = ("open_ts", "o", "h", "l", "c", "known_at")  # the R3 columns the outcome rules read
M15_COLS = ("open_ts", "c")  # the R4 mark columns the outcome rules read (``engine.Instrument.mark15``)
LAZY_15M_CACHE_BYTES = 128 * 2**20  # StoreSource's R3 / R4-mark lazy cache budget (implementation parameter)


class ByteLRU:
    """A byte-bounded LRU (``get`` / ``put(key, value, nbytes)``), the ``rawstore.ChunkCache`` semantics:
    least recently used evicted first (total access order, no ties); an item larger than the budget is
    returned but not kept."""

    def __init__(self, budget_bytes: int) -> None:
        if isinstance(budget_bytes, bool) or not isinstance(budget_bytes, int) or budget_bytes <= 0:
            raise ValueError(f"budget_bytes must be a positive int, got {budget_bytes!r}")
        self.budget, self.bytes = budget_bytes, 0
        self._items: OrderedDict = OrderedDict()
        self.stats = {"hits": 0, "misses": 0, "evictions": 0}

    def get(self, key: tuple) -> object | None:
        if key in self._items:
            self._items.move_to_end(key)
            self.stats["hits"] += 1
            return self._items[key][0]
        self.stats["misses"] += 1
        return None

    def put(self, key: tuple, value: object, nbytes: int) -> None:
        if key in self._items:
            self.bytes -= self._items.pop(key)[1]
        if nbytes > self.budget:
            return
        self._items[key] = (value, int(nbytes))
        self.bytes += int(nbytes)
        while self.bytes > self.budget:
            _, (_, b) = self._items.popitem(last=False)
            self.bytes -= b
            self.stats["evictions"] += 1


@dataclass(frozen=True)
class LazyInputs:
    """One symbol's lazily read outcome inputs (``runner.ResearchUniverse``): the R5 1-minute bars, the R3
    15m bars and R4 mark bars (None: resident frames) and the E_M aggTrades (None: none / resident), plus
    ``spans``: envelopes of tables held lazily for the decision side only (R4 premium / index), so the
    universe's guarded data span still covers every input."""

    b1: Lazy1m
    b15: LazyBars | None = None
    m15: LazyBars | None = None
    trades: LazyBars | None = None
    spans: tuple[tuple[int, int], ...] = ()

    def all_spans(self) -> list[tuple[int, int]]:
        out = [x.span() for x in (self.b1, self.b15, self.m15, self.trades) if x is not None]
        return [(int(a), int(b)) for sp in (*out, *self.spans) if sp is not None for a, b in (sp,)]


__all__ = ["B15_COLS", "COLS", "LAZY_15M_CACHE_BYTES", "LAZY_1M_CACHE_BYTES", "LAZY_AGG_CACHE_BYTES",
           "M15_COLS", "SCAN_STEP_NS", "TRADE_COLS", "ByteLRU", "DayFetch", "FrameDays", "FrameFetch",
           "FrameTable", "Lazy1m", "LazyBars", "LazyInputs", "from_frame", "table_from_frame",
           "trades_from_days"]
