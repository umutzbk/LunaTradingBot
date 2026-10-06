"""Markdown data-integrity report over the bar store."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from luna.data.store import BarStore, validate_bars
from luna.types import Venue

_VENUES: list[Venue] = ["kraken_futures", "kraken_spot"]


def _ts(t: pd.Timestamp) -> str:
    return "-" if pd.isna(t) else t.strftime("%Y-%m-%d %H:%M")


def build_data_report(store: BarStore, out: Path) -> Path:
    lines = ["# Luna data report", "", "## Validation", ""]
    lines += ["| venue | asset | rows | start | end | dup | non-mono | gaps | filled | spikes | status |",
              "|---|---|---|---|---|---|---|---|---|---|---|"]
    for venue in _VENUES:
        for asset in store.assets(venue):
            df = store.read(venue, asset)
            if df.empty:
                lines.append(f"| {venue} | {asset} | 0 | - | - | 0 | 0 | 0 | 0 | 0 | empty |")
                continue
            r = validate_bars(df)
            status = "ok" if r.ok else "issues"
            lines.append(
                f"| {venue} | {asset} | {r.rows} | {_ts(r.start)} | {_ts(r.end)} | {r.duplicates} | "
                f"{r.non_monotonic} | {r.gaps} | {r.filled_bars} | {len(r.spikes)} | {status} |"
            )
    lines += ["", "## Funding coverage", "", "| asset | rows | first | last |", "|---|---|---|---|"]
    for asset in store.assets("kraken_futures"):
        f = store.read_funding(asset)
        first, last = (f.index[0], f.index[-1]) if len(f) else (pd.NaT, pd.NaT)
        lines.append(f"| {asset} | {len(f)} | {_ts(first)} | {_ts(last)} |")
    lines += ["", "## Spot vs perp basis", "",
              "abs(log(spot/perp)) of 15m closes on common bars.", "",
              "| asset | common bars | median | p95 |", "|---|---|---|---|"]
    spot_assets = set(store.assets("kraken_spot"))
    for asset in store.assets("kraken_futures"):
        if asset not in spot_assets:
            continue
        perp = store.read("kraken_futures", asset)["close"]
        spot = store.read("kraken_spot", asset)["close"]
        common = perp.index.intersection(spot.index)
        if len(common) == 0:
            lines.append(f"| {asset} | 0 | - | - |")
            continue
        b = np.abs(np.log(spot.loc[common] / perp.loc[common]))
        lines.append(f"| {asset} | {len(common)} | {b.median():.5f} | {b.quantile(0.95):.5f} |")
    lines += ["", "## Spread proxy", "",
              "Median 15m (high-low)/close in bps (range-based upper bound on the half-spread scale).", "",
              "| venue | asset | median range bps |", "|---|---|---|"]
    for venue in _VENUES:
        for asset in store.assets(venue):
            df = store.read(venue, asset)
            if df.empty:
                continue
            rng = ((df["high"] - df["low"]) / df["close"] * 1e4).median()
            lines.append(f"| {venue} | {asset} | {rng:.2f} |")
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out
