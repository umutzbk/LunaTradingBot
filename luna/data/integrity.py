"""Spot bar integrity over the perp universe (after the archive + Trades gap-fill backfill)."""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from luna.data.store import BarStore

OHLC = ["open", "high", "low", "close"]
GRID = pd.Timedelta("15min")
HOUR = pd.Timedelta(hours=1)


@dataclass(frozen=True)
class IntegrityResult:
    asset: str
    ok: bool
    reason: str
    gaps_over_1h: int | None = None  # within the checked window; informational, not a failure
    max_gap_h: float | None = None


def _check(
    df: pd.DataFrame, since: pd.Timestamp, min_last: pd.Timestamp, max_gap_h: float
) -> tuple[list[str], int | None, float | None]:
    """(problems, gaps > 1h, largest gap in hours) for one asset's bars."""
    if df.empty:
        return ["no kraken_spot bars"], None, None
    out = []
    idx = df.index
    if not idx.is_unique:
        out.append(f"index not unique ({int(idx.duplicated().sum())} duplicates)")
    if not idx.is_monotonic_increasing:
        out.append("index not monotonic increasing")
    if out:
        return out, None, None  # window/gap checks below assume a sorted unique index
    if idx[0] > since:
        out.append(f"first bar {idx[0].isoformat()} > since {since.isoformat()}")
    if idx[-1] < min_last:
        out.append(f"last bar {idx[-1].isoformat()} < min-last {min_last.isoformat()}")
    # value, grid and gap checks cover the gap-fill period: from the last bar at/before `since`
    start = idx[idx <= since][-1] if (idx <= since).any() else idx[0]
    w = df[idx >= start]

    def count(mask: pd.Series, what: str) -> None:
        n = int(mask.sum())
        if n:
            out.append(f"{n} bars with {what}")

    count(w[OHLC].isna().any(axis=1), "NaN OHLC")
    count(w["high"] < w["low"], "high<low")
    count(w["high"] < w[["open", "close"]].max(axis=1), "high<max(open,close)")
    count(w["low"] > w[["open", "close"]].min(axis=1), "low>min(open,close)")
    count((w[OHLC] <= 0).any(axis=1), "non-positive price")
    count(pd.Series(w.index.floor(GRID) != w.index, index=w.index), "timestamp off the 15-minute grid")
    if "filled" in w.columns:
        count(w["filled"].fillna(False).astype(bool), "filled=True")
    gaps_1h, worst_h = 0, None
    if len(w) > 1:
        deltas = w.index.to_series().diff().iloc[1:]
        worst = deltas.max()
        gaps_1h, worst_h = int((deltas > HOUR).sum()), round(worst.total_seconds() / 3600, 2)
        if worst > pd.Timedelta(hours=max_gap_h):
            out.append(f"gap {worst_h:.2f}h > {max_gap_h:g}h ending {deltas.idxmax().isoformat()}")
    return out, gaps_1h, worst_h


def check_spot_integrity(
    store: BarStore, since: pd.Timestamp, min_last: pd.Timestamp, max_gap_h: float = 24.0
) -> list[IntegrityResult]:
    """One result per asset of the perp store universe (``store.assets("kraken_futures")``)."""
    res = []
    for asset in store.assets("kraken_futures"):
        probs, gaps_1h, worst_h = _check(store.read("kraken_spot", asset), since, min_last, max_gap_h)
        res.append(IntegrityResult(asset, not probs, "; ".join(probs) if probs else "ok", gaps_1h, worst_h))
    return res
