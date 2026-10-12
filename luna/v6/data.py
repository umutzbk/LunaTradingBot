"""Point-in-time inputs for V6 (spec §1). Funding: ``rel`` relative rate per hourly period start
(NaN = not known), optional ``abs`` (USD per unit), and ``known_at`` (historical: p + 1h; forward:
the collector receive time). A period is visible at ``t`` iff ``p + 1h <= t`` and ``known_at <= t``."""

from __future__ import annotations

import numpy as np
import pandas as pd

HOUR = pd.Timedelta("1h")


def _check_index(idx: pd.Index, step: pd.Timedelta, name: str) -> None:
    if not isinstance(idx, pd.DatetimeIndex) or idx.tz is None:
        raise ValueError(f"{name}: index must be a tz-aware DatetimeIndex")
    if not idx.is_monotonic_increasing or idx.has_duplicates:
        raise ValueError(f"{name}: index must be strictly increasing")
    if (idx.as_unit("ns").asi8 % step.value).any():
        raise ValueError(f"{name}: index must lie on the {step} grid")


class FundingPanel:
    def __init__(self, rel: pd.DataFrame, abs_: pd.DataFrame | None = None,
                 known_at: pd.DataFrame | None = None) -> None:
        _check_index(rel.index, HOUR, "funding")
        full = pd.date_range(rel.index.min(), rel.index.max(), freq="h") if len(rel) else rel.index
        full = full.as_unit("ns")
        self.rel = rel.reindex(full).astype(float)
        self.abs = abs_.reindex(full).astype(float) if abs_ is not None else None
        if known_at is None:
            ka = np.broadcast_to((full + HOUR).asi8[:, None], self.rel.shape)
            self._known = np.array(ka, dtype=np.int64)
        else:
            k = known_at.reindex(index=full, columns=self.rel.columns)
            never = np.iinfo(np.int64).max
            cols = []
            for c in k.columns:
                col = pd.to_datetime(k[c], utc=True).dt.as_unit("ns")
                cols.append(np.where(col.notna(), col.astype("int64"), never))
            self._known = np.stack(cols, axis=1).astype(np.int64)
        self._t = full.asi8
        self._v = self.rel.to_numpy()
        self.assets = list(self.rel.columns)

    def delayed(self, lag: pd.Timedelta) -> "FundingPanel":
        """The same values, each known ``lag`` later (forward S-PIT10 decision view); settlement keeps
        its own panel."""
        import copy

        d = copy.copy(self)
        never = np.iinfo(np.int64).max
        d._known = np.where(self._known == never, never, self._known + pd.Timedelta(lag).value)
        return d

    def _visible(self, lo: pd.Timestamp, hi: pd.Timestamp, t: pd.Timestamp) -> tuple[np.ndarray, np.ndarray]:
        """Values of periods p in [lo, hi] that are visible at t (others NaN)."""
        a = np.searchsorted(self._t, pd.Timestamp(lo).value, "left")
        b = np.searchsorted(self._t, pd.Timestamp(hi).value, "right")
        v = self._v[a:b].copy()
        ended = (self._t[a:b] + HOUR.value) <= pd.Timestamp(t).value
        ok = ended[:, None] & (self._known[a:b] <= pd.Timestamp(t).value) & np.isfinite(v)
        v[~ok] = np.nan
        return v, ok

    def fl(self, t: pd.Timestamp, hours: int, min_known: int) -> pd.Series:
        """Mean rate of the periods starting in [t - hours h, t - 1h] visible at t; NaN below
        ``min_known`` visible periods."""
        t = pd.Timestamp(t)
        v, ok = self._visible(t - hours * HOUR, t - HOUR, t)
        n = ok.sum(axis=0)
        with np.errstate(invalid="ignore"):
            m = np.where(n >= min_known, np.nansum(v, axis=0) / np.maximum(n, 1), np.nan)
        return pd.Series(m, index=self.assets)

    def newest_end(self, asset: str, t: pd.Timestamp) -> pd.Timestamp | None:
        """End (p + 1h) of the newest period of ``asset`` visible at t."""
        t = pd.Timestamp(t)
        j = self.assets.index(asset)
        b = np.searchsorted(self._t, (t - HOUR).value, "right")
        lo = max(0, b - 24 * 7)
        ok = np.isfinite(self._v[lo:b, j]) & (self._known[lo:b, j] <= t.value)
        if not ok.any():
            return None
        return pd.Timestamp(int(self._t[lo:b][ok][-1]), tz="UTC") + HOUR

    def abs_rate(self, asset: str, p: pd.Timestamp, t: pd.Timestamp) -> float:
        """USD per unit for period p if visible at t (settlement), else NaN."""
        p, t = pd.Timestamp(p), pd.Timestamp(t)
        if self.abs is None or p + HOUR > t:
            return float("nan")
        i = np.searchsorted(self._t, p.value, "left")
        if i >= len(self._t) or self._t[i] != p.value:
            return float("nan")
        j = self.assets.index(asset)
        if self._known[i, j] > t.value:
            return float("nan")
        return float(self.abs.iloc[i, j])

    def label(self, s: pd.Timestamp, h: int, t: pd.Timestamp) -> tuple[pd.Series, pd.Series]:
        """(mean of the known periods in [s, s + h - 1h], share of the h periods known) as of t.
        Refused unless the whole window ended by t (s + h <= t)."""
        s, t = pd.Timestamp(s), pd.Timestamp(t)
        if s + h * HOUR > t:
            raise ValueError(f"label for {s} + {h}h is not known at {t}")
        v, ok = self._visible(s, s + (h - 1) * HOUR, t)
        n = ok.sum(axis=0)
        with np.errstate(invalid="ignore"):
            m = np.where(n > 0, np.nansum(v, axis=0) / np.maximum(n, 1), np.nan)
        return pd.Series(m, index=self.assets), pd.Series(n / h, index=self.assets)


BAR = pd.Timedelta("15min")


class BarPanel:
    """15-minute bars by open time (``close`` frame, columns = assets). A bar that opened at b is
    closed (visible) at b + 15m."""

    def __init__(self, close: pd.DataFrame, volume: pd.DataFrame | None = None) -> None:
        _check_index(close.index, BAR, "bars")
        self.close = close.astype(float)
        self.volume = volume.reindex_like(close).astype(float) if volume is not None else None
        self._t = close.index.as_unit("ns").asi8

    def closed_window(self, t: pd.Timestamp, n: int) -> pd.DataFrame:
        """The last ``n`` bars closed by t (open time in [t - n*15m, t - 15m])."""
        t = pd.Timestamp(t)
        a = np.searchsorted(self._t, (t - n * BAR).value, "left")
        b = np.searchsorted(self._t, (t - BAR).value, "right")
        return self.close.iloc[a:b]

    def mid(self, t: pd.Timestamp) -> pd.Series:
        """Close of the bar that opened at t - 15m (S-DATA-2); NaN if absent."""
        b = (pd.Timestamp(t) - BAR).value
        i = int(np.searchsorted(self._t, b, "left"))
        if i < len(self._t) and self._t[i] == b:
            return self.close.iloc[i]
        return pd.Series(np.nan, index=self.close.columns)
