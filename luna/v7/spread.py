"""Half-spread proxy: exact Abdi-Ranaldo (2017) window estimator on 15m last-price bars (spec S-4.5, DEC-38).

For consecutive present bars (k, k+1) of ONE instance: pi_k = 4 (c_k - eta_k)(c_k - eta_{k+1}) with
c = ln close, eta = (ln high + ln low) / 2; known_at(pi_k) = close of bar k+1. Window at tau: pairs with
bar k opening >= tau - 24 h - 15 min and known_at <= tau - 1 min. S = sqrt(max(mean pi, 0)); the value is
h = max(lam * S / 2, 1 bp) (full spread -> /2 -> *lam -> floor). Empty pair set -> None (missing).
The caller passes bars of a single instance only (no cross-instance computation).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from luna.v7.constants import FROZEN
from luna.v7.timeutil import MIN

BAR_15M = 15 * MIN


def pair_products(bars15: pd.DataFrame) -> pd.DataFrame:
    """Pairs (open_ts of bar k, pi_k, known_at) over consecutive present bars with valid prices."""
    b = bars15.sort_values("open_ts")
    hlc = b[["h", "l", "c"]].to_numpy(np.float64)
    b = b[np.isfinite(hlc).all(axis=1) & (hlc > 0).all(axis=1)]
    t = b["open_ts"].to_numpy(np.int64)
    c = np.log(b["c"].to_numpy(np.float64))
    eta = (np.log(b["h"].to_numpy(np.float64)) + np.log(b["l"].to_numpy(np.float64))) / 2.0
    if "known_at" not in b.columns:
        raise ValueError("bars15 has no known_at column: point-in-time filtering is impossible")
    if not np.isfinite(pd.to_numeric(b["known_at"], errors="coerce").to_numpy(np.float64)).all():
        raise ValueError("bars15 has a missing / non-finite known_at: point-in-time filtering is impossible")
    ka = b["known_at"].to_numpy(np.int64)
    consec = (t[1:] - t[:-1]) == BAR_15M
    pi = 4.0 * (c[:-1] - eta[:-1]) * (c[:-1] - eta[1:])
    known = np.maximum(ka[1:], t[1:] + BAR_15M)
    return pd.DataFrame({"open_ts": t[:-1][consec], "pi": pi[consec], "known_at": known[consec]})


def half_spread(bars15_instance: pd.DataFrame, tau_ns: int) -> float | None:
    """S-4.5 proxy value h(tau) (decision-time or fill-time); None if the pair set is empty."""
    p = pair_products(bars15_instance)
    sel = p[(p["open_ts"] >= tau_ns - FROZEN.spread.window_ns) & (p["known_at"] <= tau_ns - MIN)]
    if len(sel) == 0:
        return None
    s = float(np.sqrt(max(float(sel["pi"].mean()), 0.0)))
    return max(FROZEN.spread.lam * s / 2.0, FROZEN.spread.floor)
