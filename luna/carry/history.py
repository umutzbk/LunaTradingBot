"""Historical snapshot building for the V5 backtest (protocol §3.1). Replay-only; paper never imports it.

Implemented so far: the **parity mode** quote (protocol §5.3 B2), which must equal the frozen V4
``HistoricalMarket.quote`` for the perp leg:

- mid = close of the first real (non-filled) trade bar with open_ts in [t, t + 1h); the quote is
  stamped at that bar's close; none -> no quote;
- half spread = mean of the analytics ``spread_full`` samples in [t - 1h, t) / 2 when >= 30 valid
  samples exist, else the 10 bps full fallback / 2 (flagged).

Primary spreads (protocol rev 3 §3.1/§3.2): ``Spread5m`` over the real 5-minute Kraken analytics
snapshots (``interval=300``). A snapshot labelled T is taken inside [T, T + 5m) (Stage 0 probe), so it
is treated as known only from **T + 5 min**. Rows that are absent stay absent: no forward-fill, no
interpolation. A series that is not on the 5-minute grid (e.g. the 60 s endpoint) is refused.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from luna.carry.frozen import CUTOFF, check_5m_provenance
from luna.v2.data import HoldoutLeak

HOUR = pd.Timedelta("1h")
BAR = pd.Timedelta("15min")
FALLBACK_FULL = 0.0010
MIN_DECISION_SAMPLES = 30
SNAPSHOT = pd.Timedelta("5min")
KNOWN_AFTER = SNAPSHOT  # a snapshot labelled T is known from T + 5m
FILL_DELAY = pd.Timedelta("15min")
DECISION_MIN_VALID = 6  # of 12 in [t - 1h, t)
FILL_MIN_VALID = 2  # of 3 in [t, t + 15m)
MAX_VALID_FULL = 0.05  # 500 bps; above is invalid


@dataclass(frozen=True)
class ParityQuote:
    mid: float
    half: float
    ts: pd.Timestamp
    fallback: bool


def decision_half_spread(spreads: pd.DataFrame | None, t: pd.Timestamp) -> tuple[float, bool]:
    """(half spread, fallback) from the samples in [t - 1h, t)."""
    if spreads is not None and len(spreads):
        idx = spreads.index
        lo, hi = idx.searchsorted(t - HOUR, "left"), idx.searchsorted(t, "left")
        w = spreads["spread_full"].iloc[lo:hi].dropna()
        if len(w) >= MIN_DECISION_SAMPLES:
            return float(w.mean()) / 2, False
    return FALLBACK_FULL / 2, True


def parity_quote(bars: pd.DataFrame | None, spreads: pd.DataFrame | None, t: pd.Timestamp) -> ParityQuote | None:
    if bars is None or not len(bars):
        return None
    w = bars.iloc[bars.index.searchsorted(t, "left") : bars.index.searchsorted(t + HOUR, "left")]
    w = w[~w["filled"].astype(bool)]
    if w.empty:
        return None
    half, fallback = decision_half_spread(spreads, t)
    return ParityQuote(float(w["close"].iloc[0]), half, w.index[0] + BAR, fallback)


@dataclass(frozen=True)
class SpreadObs:
    full: float
    n_valid: int
    n_expected: int
    fallback: bool

    @property
    def half(self) -> float:
        return self.full / 2


class Spread5m:
    """Point-in-time view of one asset's 5-minute snapshot series (``spread_full`` by label T)."""

    def __init__(self, df: pd.DataFrame | None, known_after: pd.Timedelta = KNOWN_AFTER):
        """``known_after`` = T + 5m (primary) or T + 10m (S-PIT10 sensitivity); never shorter."""
        if known_after < KNOWN_AFTER:
            raise ValueError(f"known_after {known_after} is shorter than the snapshot interval")
        self.known_after = known_after
        if df is None or not len(df):
            self.frame = pd.DataFrame({"spread_full": []}, index=pd.DatetimeIndex([], tz="UTC"))
        else:
            idx = df.index
            if not isinstance(idx, pd.DatetimeIndex) or idx.tz is None:
                raise ValueError("spread snapshots need a tz-aware DatetimeIndex")
            if not idx.is_monotonic_increasing or idx.has_duplicates:
                raise ValueError("spread snapshot labels must be strictly increasing")
            if (idx.as_unit("ns").asi8 % SNAPSHOT.value).any():
                raise ValueError("spread snapshot labels must lie on the 5-minute grid (no 1-minute series)")
            if idx.max() >= CUTOFF:
                raise HoldoutLeak(f"spread snapshot {idx.max()} at or after {CUTOFF}")
            self.frame = df[["spread_full"]]
        v = self.frame["spread_full"].to_numpy(dtype=float)
        with np.errstate(invalid="ignore"):
            self._valid = np.isfinite(v) & (v > 0) & (v <= MAX_VALID_FULL)
        self._v = v
        self._idx = self.frame.index

    def _labels(self, lo: pd.Timestamp, hi: pd.Timestamp, now: pd.Timestamp) -> slice:
        end = min(self._idx.searchsorted(hi, "left"), self._idx.searchsorted(now - self.known_after, "right"))
        start = self._idx.searchsorted(lo, "left")
        return slice(start, max(start, end))

    def _expected(self, lo: pd.Timestamp, hi: pd.Timestamp, now: pd.Timestamp) -> int:
        last = min(hi - SNAPSHOT, now - self.known_after)
        return max(0, (last - lo) // SNAPSHOT + 1)

    @staticmethod
    def _check_t(t: pd.Timestamp) -> pd.Timestamp:
        t = pd.Timestamp(t)
        if t.tzinfo is None or t.value % BAR.value:
            raise ValueError(f"decision time {t} must be tz-aware and on the 15-minute grid")
        return t

    def _obs(self, sl: slice, need: int, expected: int) -> SpreadObs:
        ok = self._valid[sl]
        n = int(ok.sum())
        if n >= need:
            return SpreadObs(float(self._v[sl][ok].mean()), n, expected, False)
        return SpreadObs(FALLBACK_FULL, n, expected, True)

    def _window(self, t: pd.Timestamp, kind: str) -> tuple[pd.Timestamp, pd.Timestamp, pd.Timestamp]:
        t = self._check_t(t)
        return (t - HOUR, t, t) if kind == "decision" else (t, t + FILL_DELAY, t + FILL_DELAY)

    def decision_labels(self, t: pd.Timestamp) -> pd.DatetimeIndex:
        return self._idx[self._labels(*self._window(t, "decision"))]

    def fill_labels(self, t: pd.Timestamp) -> pd.DatetimeIndex:
        return self._idx[self._labels(*self._window(t, "fill"))]

    def decision(self, t: pd.Timestamp) -> SpreadObs:
        w = self._window(t, "decision")
        return self._obs(self._labels(*w), DECISION_MIN_VALID, self._expected(*w))

    def fill(self, t: pd.Timestamp) -> SpreadObs:
        """Spread for the fill at t + 15m of the order decided at t."""
        w = self._window(t, "fill")
        return self._obs(self._labels(*w), FILL_MIN_VALID, self._expected(*w))


def load_spread5m(root: Path, asset: str, known_after: pd.Timedelta = KNOWN_AFTER) -> Spread5m:
    """Primary loader: refuses a file whose provenance is not real 300 s snapshots or whose sha differs."""
    p = Path(root) / f"{asset}.parquet"
    prov = json.loads((Path(root) / f"{asset}.provenance.json").read_text(encoding="utf-8"))
    check_5m_provenance(prov, p, asset)
    return Spread5m(pd.read_parquet(p), known_after=known_after)
