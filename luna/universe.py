"""Universe selection from liquidity (ADV) and history length, using only past data."""

from __future__ import annotations

import pandas as pd


def adv_usd(bars: pd.DataFrame, asof: pd.Timestamp, days: int = 30) -> float:
    """Mean daily traded USD (volume * close) over [asof - days, asof); days without bars count as 0."""
    lo = asof - pd.Timedelta(days=days)
    w = bars[(bars.index >= lo) & (bars.index < asof)]
    if w.empty:
        return 0.0
    daily = (w["volume"] * w["close"]).groupby(w.index.floor("D")).sum()
    full = pd.date_range(lo.floor("D"), asof - pd.Timedelta(days=1), freq="D", tz="UTC")
    return float(daily.reindex(full, fill_value=0.0).mean())


def select_universe(
    panel: dict[str, pd.DataFrame], asof: pd.Timestamp, min_adv_usd: float, min_history_days: int
) -> list[str]:
    """Assets with >= min_history_days of history before asof and ADV >= min_adv_usd; ADV descending."""
    chosen: list[tuple[float, str]] = []
    for asset, bars in panel.items():
        past = bars[bars.index < asof]
        if past.empty or (asof - past.index[0]) < pd.Timedelta(days=min_history_days):
            continue
        adv = adv_usd(past, asof)
        if adv >= min_adv_usd:
            chosen.append((adv, asset))
    return [a for _, a in sorted(chosen, key=lambda t: (-t[0], t[1]))]
