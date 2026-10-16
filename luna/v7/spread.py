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
from luna.v7.timeutil import MIN, ns_array

BAR_15M = 15 * MIN


def _pairs(bars15: pd.DataFrame, span: tuple[int, int] | None = None
           ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(open_ts of bar k, pi_k, known_at) arrays over consecutive present bars with valid prices (numpy core).

    ``open_ts`` / ``known_at`` are validated on the whole frame (fail closed: a missing column, a non-finite
    or out-of-range value raises ValueError even on a bar with an invalid price). ``span`` = [lo, hi): only
    bars opening in it enter the products (``half_spread``: every pair of its window has bar k opening >= lo
    and bar k + 1 opening < tau, so trimming first leaves the window's pair set unchanged; performance)."""
    if "known_at" not in bars15.columns:
        raise ValueError("bars15 has no known_at column: point-in-time filtering is impossible")
    t = ns_array(bars15["open_ts"], "open_ts")
    ka = ns_array(bars15["known_at"], "known_at")  # ValueError on missing / non-finite / out-of-range values
    h = bars15["h"].to_numpy(np.float64)
    lo = bars15["l"].to_numpy(np.float64)
    c = bars15["c"].to_numpy(np.float64)
    return _products(t, ka, h, lo, c, span)


def _f64(x) -> np.ndarray:
    return x.to_numpy(np.float64) if hasattr(x, "to_numpy") else np.asarray(x, dtype=np.float64)


def _pairs_arrays(open_ts, known_at, high, low, close, span: tuple[int, int] | None = None
                  ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """``_pairs`` on the columns (arrays; the lazy windows of red-team r2 MJ1 round 2): the same validation,
    in the same order, and the same products."""
    t = ns_array(open_ts, "open_ts")
    ka = ns_array(known_at, "known_at")
    h = _f64(high)
    lo = _f64(low)
    c = _f64(close)
    return _products(t, ka, h, lo, c, span)


def _products(t: np.ndarray, ka: np.ndarray, h: np.ndarray, lo: np.ndarray, c: np.ndarray,
              span: tuple[int, int] | None) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    if span is not None:
        m = (t >= span[0]) & (t < span[1])
        t, ka, h, lo, c = t[m], ka[m], h[m], lo[m], c[m]
    order = np.argsort(t, kind="stable")
    h, lo, c = h[order], lo[order], c[order]
    ok = np.isfinite(h) & np.isfinite(lo) & np.isfinite(c) & (h > 0) & (lo > 0) & (c > 0)
    t, ka, h, lo, c = t[order][ok], ka[order][ok], h[ok], lo[ok], c[ok]
    lc = np.log(c)
    eta = (np.log(h) + np.log(lo)) / 2.0
    consec = (t[1:] - t[:-1]) == BAR_15M
    pi = 4.0 * (lc[:-1] - eta[:-1]) * (lc[:-1] - eta[1:])
    known = np.maximum(ka[1:], t[1:] + BAR_15M)
    return t[:-1][consec], pi[consec], known[consec]


def pair_products(bars15: pd.DataFrame) -> pd.DataFrame:
    """Pairs (open_ts of bar k, pi_k, known_at) over consecutive present bars with valid prices."""
    t, pi, known = _pairs(bars15)
    return pd.DataFrame({"open_ts": t, "pi": pi, "known_at": known})


def half_spread(bars15_instance: pd.DataFrame, tau_ns: int) -> float | None:
    """S-4.5 proxy value h(tau) (decision-time or fill-time); None if the pair set is empty."""
    lo = tau_ns - FROZEN.spread.window_ns
    return _value(*_pairs(bars15_instance, (lo, tau_ns)), lo, tau_ns)


def half_spread_arrays(open_ts, known_at, high, low, close, tau_ns: int) -> float | None:
    """``half_spread`` on a bar window's columns (``_pairs_arrays``; the lazy 15m windows): the same value."""
    lo = tau_ns - FROZEN.spread.window_ns
    return _value(*_pairs_arrays(open_ts, known_at, high, low, close, (lo, tau_ns)), lo, tau_ns)


def _value(t: np.ndarray, pi: np.ndarray, known: np.ndarray, lo: int, tau_ns: int) -> float | None:
    sel = pi[(t >= lo) & (known <= tau_ns - MIN)]
    if len(sel) == 0:
        return None
    s = float(np.sqrt(max(float(np.mean(sel)), 0.0)))
    return max(FROZEN.spread.lam * s / 2.0, FROZEN.spread.floor)
