"""Research V3 ranking scores (spec §3, §4). Higher score = long side.

All scores read only daily values stamped at or before the decision stamp ``t`` (the 23:45 UTC bar
that closes the day, as in V2 ``daily_closes``):

- ``premium``: minus the mean daily perp-spot premium over (t - L days, t]; NaN with fewer than
  ceil(L / 2) known days. Low premium (uncrowded) ranks long, high premium ranks short.
- ``lowvol``: minus the sample std (ddof=1) of daily log returns stamped in (t - L days, t]; NaN with
  fewer than ceil(L / 2) returns. Low volatility ranks long.
- ``shuffle<seed>:<base>``: null baseline: one standard normal per asset from a generator seeded by
  (seed, t, asset name), so draws do not depend on which other assets are present; NaN wherever the
  base signal (``premium``, ``lowvol``; ``momentum`` when omitted) is NaN, so the null trades exactly
  the candidate's eligible set.
"""

from __future__ import annotations

import dataclasses
import math
import zlib

import numpy as np
import pandas as pd

from luna.v2.xsec import XSecParams, lookback_returns

_DAY = pd.Timedelta(days=1)


def premium_daily(panel: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Premium at each day's decision bar (23:45 UTC) per asset; NaN where that bar is filled."""
    cols = {}
    for a, df in panel.items():
        if "premium" not in df.columns:
            continue
        at = df.loc[(df.index.hour == 23) & (df.index.minute == 45)]
        cols[a] = at["premium"].where(~at["filled"].astype(bool))
    return pd.DataFrame(cols).sort_index() if cols else pd.DataFrame()


def _min_count(lookback_days: int) -> int:
    return math.ceil(lookback_days / 2)


def _premium(
    daily_close: pd.DataFrame, t: pd.Timestamp, lookback: int, aux: pd.DataFrame | None
) -> pd.Series:
    if aux is None or aux.empty:
        return pd.Series(math.nan, index=daily_close.columns)
    win = aux.loc[(aux.index > t - lookback * _DAY) & (aux.index <= t)].reindex(columns=daily_close.columns)
    mean = win.mean()
    return (-mean).where(win.notna().sum() >= _min_count(lookback))


def _lowvol(daily_close: pd.DataFrame, t: pd.Timestamp, lookback: int) -> pd.Series:
    closes = daily_close.loc[(daily_close.index > t - (lookback + 1) * _DAY) & (daily_close.index <= t)]
    rets = np.log(closes).diff()
    rets = rets.loc[rets.index > t - lookback * _DAY]
    return (-rets.std(ddof=1)).where(rets.notna().sum() >= _min_count(lookback))


def _draw(seed: int, t: pd.Timestamp, name: str) -> float:
    """One standard normal per (seed, stamp, asset): independent of which other assets are present."""
    return float(
        np.random.default_rng([seed, int(t.value // 10**9), zlib.crc32(name.encode())]).standard_normal()
    )


def _shuffle(
    daily_close: pd.DataFrame,
    t: pd.Timestamp,
    params: XSecParams,
    seed: int,
    base: str,
    aux: pd.DataFrame | None,
) -> pd.Series:
    draws = pd.Series([_draw(seed, t, a) for a in daily_close.columns], index=daily_close.columns)
    if base == "momentum":
        ok = lookback_returns(daily_close, t, params.lookback_days)
    else:
        ok = score(daily_close, t, dataclasses.replace(params, signal=base), aux)
    return draws.where(np.isfinite(ok))


def base_signal(signal: str) -> str:
    """The real signal behind a shuffled null (``shuffle3:premium`` -> ``premium``)."""
    return signal.split(":", 1)[1] if signal.startswith("shuffle") and ":" in signal else signal


def score(
    daily_close: pd.DataFrame, t: pd.Timestamp, params: XSecParams, aux: pd.DataFrame | None
) -> pd.Series:
    sig = params.signal
    if sig == "premium":
        return _premium(daily_close, t, params.lookback_days, aux)
    if sig == "lowvol":
        return _lowvol(daily_close, t, params.lookback_days)
    head, _, base = sig.partition(":")
    seed = head[len("shuffle") :]
    if head.startswith("shuffle") and seed.isdigit() and base in ("", "momentum", "premium", "lowvol"):
        return _shuffle(daily_close, t, params, int(seed), base or "momentum", aux)
    raise ValueError(f"unknown Research V3 signal {sig!r}")
