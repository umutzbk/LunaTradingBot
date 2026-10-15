"""Indexed point-in-time access to sorted per-symbol frames (SC-01 part 2, red-team M1; AMB-38). Pure
performance: every function here returns exactly what the boolean-mask definition it replaces returns (the
equivalence tests in ``tests/v7/test_perf_equivalence.py`` compare against verbatim copies of the replaced
code); no research rule lives here.

* ``FrameIndex``: a frame's validated int64 event-time and ``known_at`` arrays. The fast path applies only
  when the event times are STRICTLY increasing and ``known_at`` is non-decreasing in that order (then the PIT
  set ``known_at <= t - 1 min`` is a prefix found with ``searchsorted``) and every float column the consumers
  read converts to float64; otherwise ``ok`` is False and the callers keep the boolean reference path (same
  values, same errors).
* ``FrameIndex.window``: the PIT rows of one instance limited to a look-back (``LOOKBACK_15M`` /
  ``LOOKBACK_R4``), always including the instance's last PIT row (so emptiness and the max ``known_at`` of the
  slice equal those of the whole PIT instance set). Valid only for consumers that read no row older than the
  look-back (documented at each use).
* ``HaltIndex``: S-5.4 ``halted`` over a 1-minute frame's PIT prefix with prefix counts of volume > 0 bars,
  and the U6 72 h "not HALTED at any time" test evaluated once per (scheduled time, effective prefix) on the
  same 1-minute grid as ``notices.U6State`` (``HaltProbe``).
* ``LRU``: a bounded memo (entries) with deterministic least-recently-used eviction.

Implementation parameters (AMB-38): ``LOOKBACK_15M``, ``LOOKBACK_R4``, ``HALT_MEMO_ENTRIES``.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Sequence
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from luna.v7.timeutil import MIN, D, H, ns_array

# Look-backs of the windowed slices (implementation parameters, AMB-38). LOOKBACK_15M bounds every 15m
# consumer at a decision t: U2 / F8 (30 UTC days ended before t: >= t - 31 d), F10 / F11 30 d returns and
# beta-hat (hours >= t - 30 d - 2 h), F6 (t - 8 d), U4 / U5 / U8 / F9 / qv24 (<= 24 h 15 min); LOOKBACK_R4
# bounds F5 (premium / mark / index bars closing in the last 24 h).
LOOKBACK_15M = 35 * D
LOOKBACK_R4 = 2 * D
HALT_MEMO_ENTRIES = 4096


class LRU:
    """Bounded memo: at most ``entries`` items, least recently used evicted first (deterministic: access
    order is total)."""

    def __init__(self, entries: int) -> None:
        if isinstance(entries, bool) or not isinstance(entries, int) or entries < 1:
            raise ValueError(f"entries must be a positive int, got {entries!r}")
        self.entries = entries
        self._d: OrderedDict = OrderedDict()
        self.stats = {"hits": 0, "misses": 0, "evictions": 0}

    def __contains__(self, key: object) -> bool:
        return key in self._d

    def __len__(self) -> int:
        return len(self._d)

    def get(self, key: object, default: object = None) -> object:
        if key in self._d:
            self._d.move_to_end(key)
            self.stats["hits"] += 1
            return self._d[key]
        self.stats["misses"] += 1
        return default

    def put(self, key: object, value: object) -> None:
        self._d[key] = value
        self._d.move_to_end(key)
        while len(self._d) > self.entries:
            self._d.popitem(last=False)
            self.stats["evictions"] += 1

    def clear(self) -> None:
        self._d.clear()


_EMPTY = np.empty(0, np.int64)


@dataclass(frozen=True)
class FrameIndex:
    """Validated arrays of ``df`` (module docstring); ``ok`` = the indexed fast path applies."""

    df: pd.DataFrame
    ok: bool
    ts: np.ndarray
    ka: np.ndarray
    cols: dict[str, np.ndarray] = field(default_factory=dict, compare=False, repr=False)  # float64 columns

    @classmethod
    def build(cls, df: pd.DataFrame, ts_col: str = "open_ts", float_cols: Sequence[str] = ()) -> FrameIndex:
        try:
            if not isinstance(df, pd.DataFrame) or ts_col not in df.columns or "known_at" not in df.columns:
                return cls(df, False, _EMPTY, _EMPTY)
            if len(df) == 0:
                return cls(df, True, _EMPTY, _EMPTY)
            ts = ns_array(df[ts_col], ts_col)
            ka = ns_array(df["known_at"], "known_at")
            cols = {c: df[c].to_numpy(np.float64) for c in float_cols}
        except (ValueError, TypeError, KeyError, OverflowError):
            return cls(df, False, _EMPTY, _EMPTY)
        ok = bool((np.diff(ts) > 0).all() and (np.diff(ka) >= 0).all())
        return cls(df, ok, ts, ka, cols) if ok else cls(df, False, _EMPTY, _EMPTY)

    def pit_end(self, t: int) -> int:
        """Number of rows with known_at <= t - 1 min (a prefix; requires ``ok``)."""
        return int(np.searchsorted(self.ka, int(t) - MIN, "right"))

    def at(self, x: int) -> int:
        """First row with event time >= x."""
        return int(np.searchsorted(self.ts, int(x), "left"))

    def instance_pit(self, first_bar_ts: int, t: int) -> pd.DataFrame:
        """``pit_rows`` + ``event time >= first_bar_ts`` as a positional slice."""
        i0, k = self.at(first_bar_ts), self.pit_end(t)
        return self.df.iloc[i0:max(i0, k)]

    def window_range(self, first_bar_ts: int, t: int, lookback: int) -> tuple[int, int]:
        """[a, k): the PIT rows of the instance starting at ``first_bar_ts`` with event time >= t -
        ``lookback``, plus the instance's last PIT row when none is that recent (empty iff the whole PIT
        instance set is)."""
        i0, k = self.at(first_bar_ts), self.pit_end(t)
        if k <= i0:
            return i0, i0
        return max(i0, min(self.at(int(t) - lookback), k - 1)), k

    def window(self, first_bar_ts: int, t: int, lookback: int) -> pd.DataFrame:
        a, k = self.window_range(first_bar_ts, t, lookback)
        return self.df.iloc[a:k]

    def max_ka(self, a: int, k: int) -> int | None:
        """Max known_at of rows [a, k) (the last one: known_at is monotone); None if empty."""
        return int(self.ka[k - 1]) if k > a else None


@dataclass(frozen=True)
class Hourly:
    """The hourly series of 15m bars with strictly increasing ``open_ts`` as arrays: ``hours`` / ``close`` /
    ``ka`` = ``beta.hourly_close_from_15m`` / ``hourly_known_at_from_15m`` (the bar closing at each full
    hour; already sorted) and the finite, positive subset ``fhours`` / ``fclose`` / ``fka``
    (``features._hourly``)."""

    hours: np.ndarray
    close: np.ndarray
    ka: np.ndarray
    fhours: np.ndarray
    fclose: np.ndarray
    fka: np.ndarray

    @classmethod
    def build(cls, open_ts: np.ndarray, close: np.ndarray, ka: np.ndarray) -> Hourly:
        end = open_ts + 15 * MIN
        m = end % H == 0
        hours, c, k = end[m], close[m], ka[m]
        f = np.isfinite(c) & (c > 0)
        return cls(hours, c, k, hours[f], c[f], k[f])

    def get(self, hour: int) -> float | None:
        """``features._hourly(bars).get(hour)``: the finite, positive close at that hour, else None."""
        i = int(np.searchsorted(self.fhours, hour, "left"))
        return self.fclose[i] if i < len(self.fhours) and self.fhours[i] == hour else None


class HaltIndex:
    """S-5.4 ``market.halted`` over the PIT prefix of a 1-minute frame (module docstring). Built only when
    the frame has valid ``open_ts`` / ``known_at`` (``ns_array``), a float ``vol_base``, non-decreasing
    ``open_ts`` and non-decreasing ``known_at``; else ``ok`` is False (callers use the reference)."""

    def __init__(self, bars1m: pd.DataFrame) -> None:
        self.ok = False
        self.ts, self.ka, self.cnt = _EMPTY, _EMPTY, np.zeros(1, np.int64)
        self._memo = LRU(HALT_MEMO_ENTRIES)
        try:
            if len(bars1m):
                ts = ns_array(bars1m["open_ts"], "open_ts")
                ka = ns_array(bars1m["known_at"], "known_at")
                v = bars1m["vol_base"].to_numpy(np.float64)
            else:
                ts, ka, v = _EMPTY, _EMPTY, np.empty(0)
        except (ValueError, TypeError, KeyError, OverflowError):
            return
        if (np.diff(ts) < 0).any() or (np.diff(ka) < 0).any():
            return
        self.ts, self.ka = ts, ka
        self.cnt = np.concatenate([[0], np.cumsum(v > 0)]).astype(np.int64)
        self.ok = True

    def pit_end(self, t: int) -> int:
        return int(np.searchsorted(self.ka, int(t) - MIN, "right"))

    def halted_many(self, taus: np.ndarray, k: int) -> np.ndarray:
        """``halted(prefix k, tau)`` for every tau: no volume > 0 bar opening in [tau - 60 min, tau)."""
        lo = np.minimum(np.searchsorted(self.ts, taus - H, "left"), k)
        hi = np.minimum(np.searchsorted(self.ts, taus, "left"), k)
        return self.cnt[hi] - self.cnt[lo] == 0

    def no_halt(self, sched: int, end: int, step: int, k: int) -> bool:
        """``not any(halted(x) for x in [*range(sched, end, step), end])`` on the PIT prefix k; cached per
        (sched, end, step, effective prefix) (rows opening at / after ``end`` never enter the test)."""
        eff = min(int(k), int(np.searchsorted(self.ts, int(end), "left")))
        key = (int(sched), int(end), int(step), eff)
        got = self._memo.get(key)
        if got is None:
            grid = np.append(np.arange(int(sched), int(end), int(step), dtype=np.int64), np.int64(end))
            got = not bool(self.halted_many(grid, eff).any())
            self._memo.put(key, got)
        return bool(got)


class HaltProbe:
    """``halted_fn`` for ``notices.U6State.active`` over a ``HaltIndex`` at the PIT prefix of decision t:
    callable like ``lambda tau: market.halted(pit_bars1m, tau)``; ``no_halt`` answers the whole 72 h test."""

    def __init__(self, index: HaltIndex, k: int) -> None:
        self.index, self.k = index, int(k)

    def __call__(self, tau: int) -> bool:
        return bool(self.index.halted_many(np.array([int(tau)], dtype=np.int64), self.k)[0])

    def no_halt(self, sched: int, end: int, step: int) -> bool:
        return self.index.no_halt(sched, end, step, self.k)


__all__ = ["HALT_MEMO_ENTRIES", "LOOKBACK_15M", "LOOKBACK_R4", "LRU", "FrameIndex", "HaltIndex", "HaltProbe",
           "Hourly"]
