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
import math
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


# --- point-in-time funding (protocol §3.1) ---------------------------------------------------------


class FundingPIT:
    """A ``FundingStore`` seen at ``now``: only periods with start + 1h <= now exist. Used for the
    signals (``trailing``, ``window``, ``newest_end``) and for settlement, which reads a period's
    rate only after the period ended."""

    def __init__(self, store, now: pd.Timestamp) -> None:
        self.store, self.now = store, pd.Timestamp(now)

    def _check(self, t: pd.Timestamp) -> None:
        if pd.Timestamp(t) > self.now:
            raise ValueError(f"funding view at {self.now} asked about {t}")

    def rate(self, asset: str, period_start: pd.Timestamp):
        if pd.Timestamp(period_start) + HOUR > self.now:
            return None
        return self.store.rate(asset, period_start)

    def window(self, asset: str, now: pd.Timestamp, hours: int):
        self._check(now)
        return self.store.window(asset, now, hours)

    def trailing(self, asset: str, now: pd.Timestamp, hours: int):
        self._check(now)
        return self.store.trailing(asset, now, hours)

    def newest_end(self, asset: str) -> pd.Timestamp | None:
        keys, _ = self.store._arrays(asset)
        i = int(np.searchsorted(keys, (self.now - HOUR).value, "right"))
        return pd.Timestamp(int(keys[i - 1]), tz="UTC") + HOUR if i else None


# --- historical snapshots (protocol §3.1, §3.2) ----------------------------------------------------

LOOKBACK = (pd.Timestamp("2025-10-06T00:00Z"), pd.Timestamp("2025-11-10T00:00Z"))
VOLUME_BARS = 96


@dataclass
class HistoricalData:
    """Point-in-time inputs: 15m mark and trade bars (by open_ts), 5-minute spread snapshots and
    the realized funding store. ``real`` is set only by the frozen-copy loader (outcome guard)."""

    mark: dict[str, pd.DataFrame]
    perp: dict[str, pd.DataFrame]
    spreads: dict[str, pd.DataFrame]
    store: object
    real: bool = False


@dataclass(frozen=True)
class QuoteInfo:
    mid_source: str | None  # "mark" | "trade" | None
    dec_fallback: bool
    dec_n: int


@dataclass(frozen=True)
class FillQuote:
    mid: float
    half: float
    ts: pd.Timestamp
    fallback: bool
    n_valid: int
    mid_source: str


def lookback_p90(spreads: dict[str, pd.DataFrame]) -> dict[str, float]:
    """S-FB-CONS: per asset the p90 of the valid snapshots in the lookback [2025-10-06, 2025-11-10)."""
    out = {}
    lo, hi = LOOKBACK
    for a, df in spreads.items():
        v = df["spread_full"]
        v = v[(v.index >= lo) & (v.index < hi)].to_numpy(dtype=float)
        with np.errstate(invalid="ignore"):
            v = v[np.isfinite(v) & (v > 0) & (v <= MAX_VALID_FULL)]
        if len(v):
            out[a] = float(np.quantile(v, 0.9))
    return out


class SnapshotBuilder:
    """§3.1 snapshot at t and §3.2 fill quote for the order decided at t (fill at t + 15m).

    ``known_after``: 5m primary, 10m S-PIT10. ``spread_mode="fixed"``: S-SPREAD-FIX (10 bps for gate
    and fill). ``fallback_full``: S-FB-CONS per-asset value used instead of 10 bps when a window
    falls back."""

    def __init__(self, data: HistoricalData, *, known_after: pd.Timedelta = KNOWN_AFTER,
                 spread_mode: str = "primary", fallback_full: dict[str, float] | None = None, auth=None) -> None:
        from luna.carry.authz import require_authorization

        require_authorization(data, auth)  # every outcome path builds its snapshots here
        if spread_mode not in ("primary", "fixed"):
            raise ValueError(spread_mode)
        self.data, self.mode = data, spread_mode
        self.fallback_full = fallback_full or {}
        for kind, frames in (("mark", data.mark), ("perp", data.perp)):
            for a, df in frames.items():
                if len(df) and df.index.max() > CUTOFF - BAR:  # a bar closing after the cut
                    raise HoldoutLeak(f"{kind} {a}: bar {df.index.max()} closes after {CUTOFF}")
        for a in data.store.assets():
            end = data.store.newest_end(a)
            if end is not None and end > CUTOFF:
                raise HoldoutLeak(f"funding {a}: period ending {end} after {CUTOFF}")
        self.assets = sorted(set(data.mark) | set(data.perp))
        self.spread = {a: Spread5m(data.spreads.get(a), known_after=known_after) for a in self.assets}
        self._mark = {a: data.mark[a]["close"] for a in data.mark}
        self._trade = {}
        self._vol = {}
        for a, df in data.perp.items():
            real = df[~df["filled"].astype(bool)]
            self._trade[a] = real["close"]
            usd = (df["volume"] * df["close"]).fillna(0.0)
            self._vol[a] = usd.rolling(VOLUME_BARS, min_periods=1).sum()

    def mid(self, a: str, t: pd.Timestamp) -> tuple[float, str | None]:
        """Close of the bar that opened at t - 15m (closed at t): mark, else a real trade bar."""
        bar = t - BAR
        for src, name in ((self._mark.get(a), "mark"), (self._trade.get(a), "trade")):
            if src is not None and bar in src.index:
                v = float(src.at[bar])
                if math.isfinite(v) and v > 0:
                    return v, name
        return math.nan, None

    def _full(self, a: str, obs: SpreadObs) -> float:
        if self.mode == "fixed":
            return FALLBACK_FULL
        if obs.fallback:
            return self.fallback_full.get(a, FALLBACK_FULL)
        return obs.full

    def snapshot(self, t: pd.Timestamp):
        from luna.carry.market import MarketSnapshot, Quote

        quotes, info = {}, {}
        for a in self.assets:
            mid, src = self.mid(a, t)
            obs = self.spread[a].decision(t)
            info[a] = QuoteInfo(src, obs.fallback and self.mode == "primary", obs.n_valid)
            if src is None:
                continue
            full = self._full(a, obs)
            vol = self._vol.get(a)
            v = vol.asof(t - BAR) if vol is not None and len(vol) and vol.index[0] <= t - BAR else math.nan
            nan = math.nan
            quotes[a] = Quote(a, mid * (1 - full / 2), mid * (1 + full / 2), math.inf, math.inf, mid, nan,
                              nan, nan, nan, float(v), nan, False, False)
        return MarketSnapshot(t, quotes), info

    def fill_quote(self, a: str, t: pd.Timestamp) -> FillQuote | None:
        """Fill of the order decided at t: mid of the snapshot at t + 15m, half spread of the
        non-overlapping fill window [t, t + 15m) known at t + 15m."""
        ft = t + FILL_DELAY
        mid, src = self.mid(a, ft)
        if src is None:
            return None
        obs = self.spread[a].fill(t)
        return FillQuote(mid, self._full(a, obs) / 2, ft, obs.fallback and self.mode == "primary",
                         obs.n_valid, src)
