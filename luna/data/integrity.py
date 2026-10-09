"""Spot bar integrity over the perp universe (after the archive + Trades gap-fill backfill)."""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from luna.data.store import BarStore

OHLC = ["open", "high", "low", "close"]


@dataclass(frozen=True)
class IntegrityResult:
    asset: str
    ok: bool
    reason: str


def _problems(df: pd.DataFrame, since: pd.Timestamp, min_last: pd.Timestamp, max_gap_h: float) -> list[str]:
    if df.empty:
        return ["no kraken_spot bars"]
    out = []
    idx = df.index
    if not idx.is_unique:
        out.append(f"index not unique ({int(idx.duplicated().sum())} duplicates)")
    if not idx.is_monotonic_increasing:
        out.append("index not monotonic increasing")
    if out:
        return out  # window/gap checks below assume a sorted unique index
    if idx[0] > since:
        out.append(f"first bar {idx[0].isoformat()} > since {since.isoformat()}")
    if idx[-1] < min_last:
        out.append(f"last bar {idx[-1].isoformat()} < min-last {min_last.isoformat()}")
    # value and gap checks cover the gap-fill period: from the last bar at/before `since`
    start = idx[idx <= since][-1] if (idx <= since).any() else idx[0]
    w = df[idx >= start]
    nan = int(w[OHLC].isna().any(axis=1).sum())
    if nan:
        out.append(f"{nan} bars with NaN OHLC")
    bad = int((w["high"] < w["low"]).sum())
    if bad:
        out.append(f"{bad} bars with high<low")
    if "filled" in w.columns:
        filled = int(w["filled"].fillna(False).astype(bool).sum())
        if filled:
            out.append(f"{filled} filled=True bars")
    if len(w) > 1:
        deltas = w.index.to_series().diff().iloc[1:]
        worst = deltas.max()
        if worst > pd.Timedelta(hours=max_gap_h):
            at = deltas.idxmax()
            out.append(f"gap {worst.total_seconds() / 3600:.2f}h > {max_gap_h:g}h ending {at.isoformat()}")
    return out


def check_spot_integrity(
    store: BarStore, since: pd.Timestamp, min_last: pd.Timestamp, max_gap_h: float = 24.0
) -> list[IntegrityResult]:
    """One result per asset of the perp store universe (``store.assets("kraken_futures")``)."""
    res = []
    for asset in store.assets("kraken_futures"):
        probs = _problems(store.read("kraken_spot", asset), since, min_last, max_gap_h)
        res.append(IntegrityResult(asset, not probs, "; ".join(probs) if probs else "ok"))
    return res
