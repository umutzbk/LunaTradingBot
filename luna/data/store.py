"""Parquet bar store (15-minute bars, UTC bar-open index) and series validation."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from luna.types import Venue

BAR_COLUMNS = ["open", "high", "low", "close", "volume", "trades", "filled"]
_BAR_DTYPES = {
    "open": "float64",
    "high": "float64",
    "low": "float64",
    "close": "float64",
    "volume": "float64",
    "trades": "int64",
    "filled": "bool",
}
SPIKE_WINDOW = 2880  # 30 days of 15-minute bars
SPIKE_MIN_PERIODS = 96  # 1 day; lets short series be checked without a full window


def _empty_bars() -> pd.DataFrame:
    idx = pd.DatetimeIndex([], tz="UTC")
    return pd.DataFrame({c: pd.Series(dtype=t) for c, t in _BAR_DTYPES.items()}, index=idx)


def _normalize(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    if out.index.tz is None:
        out.index = out.index.tz_localize("UTC")
    else:
        out.index = out.index.tz_convert("UTC")
    if "filled" not in out.columns:
        out["filled"] = False
    return out[BAR_COLUMNS]


class BarStore:
    def __init__(self, root: Path):
        self.root = Path(root)

    def _bar_path(self, venue: Venue, asset: str) -> Path:
        return self.root / "bars" / venue / f"{asset}.parquet"

    def _funding_path(self, asset: str) -> Path:
        return self.root / "funding" / f"{asset}.parquet"

    def read(
        self,
        venue: Venue,
        asset: str,
        start: pd.Timestamp | None = None,
        end: pd.Timestamp | None = None,
    ) -> pd.DataFrame:
        """Bars with start <= index <= end (both inclusive); empty frame if nothing stored."""
        path = self._bar_path(venue, asset)
        if not path.exists():
            return _empty_bars()
        df = pd.read_parquet(path)
        if start is not None:
            df = df[df.index >= start]
        if end is not None:
            df = df[df.index <= end]
        return df

    def write(self, venue: Venue, asset: str, df: pd.DataFrame) -> None:
        path = self._bar_path(venue, asset)
        path.parent.mkdir(parents=True, exist_ok=True)
        _normalize(df).to_parquet(path)

    def append(self, venue: Venue, asset: str, df: pd.DataFrame) -> int:
        """Merge into stored bars (new rows win on duplicate index); return rows added."""
        old = self.read(venue, asset)
        new = _normalize(df)
        merged = pd.concat([old, new])
        merged = merged[~merged.index.duplicated(keep="last")].sort_index()
        self.write(venue, asset, merged)
        return len(merged) - len(old)

    def delete(self, venue: Venue, asset: str) -> None:
        self._bar_path(venue, asset).unlink(missing_ok=True)

    def assets(self, venue: Venue) -> list[str]:
        d = self.root / "bars" / venue
        return sorted(p.stem for p in d.glob("*.parquet")) if d.exists() else []

    def read_funding(self, asset: str) -> pd.DataFrame:
        path = self._funding_path(asset)
        if not path.exists():
            return pd.DataFrame({"rate": pd.Series(dtype="float64")}, index=pd.DatetimeIndex([], tz="UTC"))
        return pd.read_parquet(path)

    def write_funding(self, asset: str, df: pd.DataFrame) -> None:
        path = self._funding_path(asset)
        path.parent.mkdir(parents=True, exist_ok=True)
        out = df[["rate"]].copy()
        out.index = out.index.tz_localize("UTC") if out.index.tz is None else out.index.tz_convert("UTC")
        out.to_parquet(path)


@dataclass
class ValidationReport:
    rows: int
    start: pd.Timestamp
    end: pd.Timestamp
    duplicates: int
    non_monotonic: int
    gaps: int
    filled_bars: int
    spikes: list[pd.Timestamp]
    ok: bool


def fill_missing(df: pd.DataFrame, freq: str = "15min") -> pd.DataFrame:
    """Reindex to the full range; missing bars get OHLC = previous close, volume/trades 0, filled=True."""
    if df.empty:
        return df.copy()
    full = pd.date_range(df.index.min(), df.index.max(), freq=freq, tz="UTC")
    out = df.reindex(full)
    missing = out["close"].isna()
    prev_close = out["close"].ffill()
    for c in ("open", "high", "low", "close"):
        out.loc[missing, c] = prev_close[missing]
    out.loc[missing, ["volume", "trades"]] = 0
    out["filled"] = out["filled"].fillna(False).astype(bool) | missing
    out["trades"] = out["trades"].astype("int64")
    return out[BAR_COLUMNS]


def validate_bars(df: pd.DataFrame, spike_sigma: float = 10.0) -> ValidationReport:
    """Check duplicates, ordering, missing 15-minute slots and price spikes.

    gaps: number of missing 15-min slots between consecutive (sorted, de-duplicated) index values.
    spikes: bars where |log return| > spike_sigma * std of the *preceding* log returns
    (rolling 2880, min_periods 96; the current bar is excluded so a spike cannot mask itself).
    """
    idx = df.index
    duplicates = int(idx.duplicated().sum())
    non_monotonic = int((idx[1:] < idx[:-1]).sum()) if len(idx) > 1 else 0
    clean = df[~idx.duplicated(keep="last")].sort_index()
    step = pd.Timedelta("15min")
    diffs = clean.index.to_series().diff().dropna()
    gaps = int(((diffs // step) - 1).clip(lower=0).sum())
    filled_bars = int(clean["filled"].sum()) if "filled" in clean.columns else 0
    rets = np.log(clean["close"]).diff()
    sigma = rets.rolling(SPIKE_WINDOW, min_periods=SPIKE_MIN_PERIODS).std().shift(1)
    spikes = list(rets.index[(rets.abs() > spike_sigma * sigma).to_numpy()])
    ok = duplicates == 0 and non_monotonic == 0 and gaps == 0 and not spikes
    return ValidationReport(
        rows=len(df),
        start=clean.index[0] if len(clean) else pd.NaT,
        end=clean.index[-1] if len(clean) else pd.NaT,
        duplicates=duplicates,
        non_monotonic=non_monotonic,
        gaps=gaps,
        filled_bars=filled_bars,
        spikes=spikes,
        ok=ok,
    )
