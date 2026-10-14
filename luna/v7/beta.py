"""Point-in-time BTC beta (spec S-5.2, DEC-16).

Inputs are hourly close series (``pd.Series`` indexed by the int64 ns timestamp of a full hour, value = last
price at that hour). Build them from 15m last-price bars with ``hourly_close_from_15m`` (the 15m bar that
CLOSES at each full hour). Returns are hourly log returns between consecutive hours; joint observations are
hours where both the asset and BTC returns exist, over the 30 d ending at the last full hour <= t - 1 min.
The caller passes only the bars of the asset's current instance (S-2.2; no cross-instance returns). Prices
whose ``known_at`` (``hourly_known_at_from_15m``, required for both series) is after t - 1 min are dropped
inside ``beta_hat``. Fewer than 500 joint observations -> None.
beta = clip(0.7 * OLS + 0.3 * 1.0, 0, 3).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from luna.v7.constants import FROZEN
from luna.v7.timeutil import MIN, D, H, ns_array

BAR_15M = 15 * MIN


def hourly_close_from_15m(bars15: pd.DataFrame) -> pd.Series:
    """Close of the 15m bar closing at each full hour, indexed by that hour (ns)."""
    end = ns_array(bars15["open_ts"], "open_ts") + BAR_15M
    m = end % H == 0
    return pd.Series(bars15["c"].to_numpy(np.float64)[m], index=end[m]).sort_index()


def hourly_known_at_from_15m(bars15: pd.DataFrame) -> pd.Series:
    """``known_at`` of the 15m bar closing at each full hour, same index as ``hourly_close_from_15m``.

    Every row's ``known_at`` is validated (``timeutil.ns_array``: finite, integral, plausible ns), not only
    the hour-closing rows that are returned - intentionally: a corrupt known_at anywhere in the frame means
    the input is not trustworthy for point-in-time filtering, so it raises ValueError instead of failing open.
    """
    end = ns_array(bars15["open_ts"], "open_ts") + BAR_15M
    m = end % H == 0
    if "known_at" not in bars15.columns:
        raise ValueError("bars15 has no known_at column: point-in-time filtering is impossible")
    ka = ns_array(bars15["known_at"], "known_at")
    return pd.Series(ka[m], index=end[m]).sort_index()


def _pit(close: pd.Series, known_at: pd.Series, t_ns: int) -> pd.Series:
    """Keep prices with known_at <= t - 1 min; a price without a known_at entry is dropped.

    ``known_at`` values are validated with ``timeutil.ns_array`` (ValueError on non-finite / out-of-range).
    """
    if not isinstance(known_at, pd.Series):
        raise TypeError("known_at must be a pandas Series (point-in-time filter is mandatory)")
    known_at = pd.Series(ns_array(known_at, "known_at"), index=known_at.index)
    ka = known_at.reindex(close.index)
    return close[(ka <= t_ns - MIN).to_numpy()]


def _rets(close_1h: pd.Series, hours: np.ndarray) -> np.ndarray:
    cur = close_1h.reindex(hours).to_numpy(np.float64)
    prev = close_1h.reindex(hours - H).to_numpy(np.float64)
    with np.errstate(all="ignore"):
        r = np.log(cur / prev)
    r[~np.isfinite(r)] = np.nan
    return r


def beta_hat(
    asset_close_1h: pd.Series,
    btc_close_1h: pd.Series,
    t_ns: int,
    asset_known_at: pd.Series,
    btc_known_at: pd.Series,
) -> float | None:
    cfg = FROZEN.beta
    asset_close_1h = _pit(asset_close_1h, asset_known_at, t_ns)
    btc_close_1h = _pit(btc_close_1h, btc_known_at, t_ns)
    last = ((t_ns - MIN) // H) * H
    hours = np.arange(last - cfg.days * D + H, last + H, H, dtype=np.int64)
    ra = _rets(asset_close_1h, hours)
    rb = _rets(btc_close_1h, hours)
    ok = np.isfinite(ra) & np.isfinite(rb)
    if int(ok.sum()) < cfg.min_obs:
        return None
    x, y = rb[ok], ra[ok]
    var = float(np.var(x))
    if var <= 0.0:
        return None
    ols = float(np.mean((x - x.mean()) * (y - y.mean())) / var)
    lo, hi = cfg.clip
    return float(min(max(cfg.w * ols + (1.0 - cfg.w) * cfg.prior, lo), hi))
