"""Historical snapshot building for the V5 backtest (protocol §3.1). Replay-only; paper never imports it.

Implemented so far: the **parity mode** quote (protocol §5.3 B2), which must equal the frozen V4
``HistoricalMarket.quote`` for the perp leg:

- mid = close of the first real (non-filled) trade bar with open_ts in [t, t + 1h); the quote is
  stamped at that bar's close; none -> no quote;
- half spread = mean of the analytics ``spread_full`` samples in [t - 1h, t) / 2 when >= 30 valid
  samples exist, else the 10 bps full fallback / 2 (flagged).

The primary mode (mark mid at t, continuous spread series) needs the Stage 0 spread series and is
not built here.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

HOUR = pd.Timedelta("1h")
BAR = pd.Timedelta("15min")
FALLBACK_FULL = 0.0010
MIN_DECISION_SAMPLES = 30


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
