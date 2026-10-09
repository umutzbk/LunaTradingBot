"""V4 signals and eligibility (spec §3, §8).

Decision time t (00:00 UTC). Every value at t uses only:
- bars with ``open_ts <= t - 15m`` (bars are labelled by open_ts and close 15 minutes later);
- funding periods that have ended, ``start + 1h <= t`` (``luna.v4.funding.known_rates``);
- forward daily aggregates of UTC days that ended by t.

Signals: ``F7`` = mean relative funding of the last 168 ended hourly periods x 8760; ``FL`` the same
over L x 24 periods (L in {7, 30}). Venue basis B = perp / spot - 1 (filled bars excluded); ``B24`` =
median over the last 24 h (>= 48 valid bars historically, >= 720 valid snapshots forward); ``B30med``
= median of the daily B24 values at t, t - 1d, ..., t - 29d (>= 27 needed).
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable

import numpy as np
import pandas as pd

from luna.v4.constants import BAR, DAY, HOUR
from luna.v4.funding import HOURS_PER_YEAR, known_rates

MIN_VOLUME_USD = 1_000_000.0
VOLUME_DAYS, VOLUME_MIN_DAYS = 30, 27
F7_HOURS, F7_MIN_HOURS = 168, 160
H2_COVERAGE = 0.90
H2_MIN_AGE = pd.Timedelta(days=30)
B24_MIN_BARS, B24_MIN_SNAPSHOTS = 48, 720
B30_DAYS, B30_MIN_DAYS = 30, 27
NAN = float("nan")


# --- funding -------------------------------------------------------------------------------------


def trailing_rel(rates: pd.DataFrame, t: pd.Timestamp, hours: int) -> tuple[float, int]:
    """(mean relative rate, number of periods) over ended periods starting in [t - hours h, t - 1h]."""
    known = known_rates(rates, t)
    w = known.loc[known.index >= t - hours * HOUR, "funding_rel"] if len(known) else known
    n = len(w)
    return (float(w.mean()) if n else NAN), n


def f7(rates: pd.DataFrame, t: pd.Timestamp) -> tuple[float, int]:
    m, n = trailing_rel(rates, t, F7_HOURS)
    return m * HOURS_PER_YEAR, n


def fl(rates: pd.DataFrame, t: pd.Timestamp, lookback_days: int) -> tuple[float, int]:
    m, n = trailing_rel(rates, t, lookback_days * 24)
    return m * HOURS_PER_YEAR, n


# --- volume --------------------------------------------------------------------------------------


def daily_volume(bars: pd.DataFrame) -> pd.Series:
    """Per UTC day: sum(volume * close) over non-filled 15m bars; days without one are absent."""
    if bars is None or bars.empty:
        return pd.Series(dtype="float64", index=pd.DatetimeIndex([], tz="UTC"))
    real = bars[~bars["filled"].astype(bool)] if "filled" in bars else bars
    return (real["volume"] * real["close"]).groupby(real.index.floor("D")).sum()


def volume_median_from_daily(daily: pd.Series, t: pd.Timestamp) -> float:
    """Median over the 30 completed days [t - 30d, t - 1d]; needs >= 27 day values."""
    w = daily[(daily.index >= t.floor("D") - VOLUME_DAYS * DAY) & (daily.index < t.floor("D"))].dropna()
    return float(w.median()) if len(w) >= VOLUME_MIN_DAYS else NAN


def hist_volume_median(bars: pd.DataFrame, t: pd.Timestamp) -> float:
    return volume_median_from_daily(daily_volume(bars[bars.index <= t - BAR]), t)


def forward_volume_median(values: pd.Series, t: pd.Timestamp) -> float:
    """Forward daily values (first valid snapshot after each 00:00); day t itself is not known at t."""
    return volume_median_from_daily(values, t)


# --- basis ---------------------------------------------------------------------------------------


def venue_basis(perp: pd.DataFrame, spot: pd.DataFrame) -> pd.Series:
    """B by open_ts from 15m closes; NaN where a leg is missing or either bar is filled."""
    if perp is None or spot is None or perp.empty or spot.empty:
        return pd.Series(dtype="float64", index=pd.DatetimeIndex([], tz="UTC"))
    idx = perp.index.union(spot.index)
    pc, sc = perp["close"].reindex(idx), spot["close"].reindex(idx)
    pf = perp["filled"].reindex(idx).astype("boolean").fillna(True).astype(bool)
    sf = spot["filled"].reindex(idx).astype("boolean").fillna(True).astype(bool)
    return (pc / sc - 1.0).where(~pf & ~sf & pc.notna() & sc.notna() & (sc > 0))


def b24_hist(basis: pd.Series, t: pd.Timestamp) -> float:
    w = basis[(basis.index >= t - 24 * HOUR) & (basis.index <= t - BAR)].dropna()
    return float(w.median()) if len(w) >= B24_MIN_BARS else NAN


def b30med(b24_at: Callable[[pd.Timestamp], float], t: pd.Timestamp) -> float:
    vals = [b24_at(t - k * DAY) for k in range(B30_DAYS)]
    vals = [v for v in vals if v is not None and not math.isnan(v)]
    return float(np.median(vals)) if len(vals) >= B30_MIN_DAYS else NAN


# --- tables --------------------------------------------------------------------------------------

COLUMNS = ["F7", "F7_n", "FL7", "FL7_n", "FL30", "FL30_n", "perp_vol_med", "spot_vol_med",
           "listed_perp", "listed_spot", "age_days", "B24", "B30med", "elig_h1", "elig_h2_7", "elig_h2_30"]


def _row(rates, t, perp_med, spot_med, listed_perp, listed_spot, age_days, b24, b30) -> dict:
    f, fn = f7(rates, t) if rates is not None and len(rates) else (NAN, 0)
    l7, l7n = fl(rates, t, 7) if rates is not None and len(rates) else (NAN, 0)
    l30, l30n = fl(rates, t, 30) if rates is not None and len(rates) else (NAN, 0)
    vol_ok_p = perp_med >= MIN_VOLUME_USD  # NaN compares False
    vol_ok_s = spot_med >= MIN_VOLUME_USD
    old = age_days >= H2_MIN_AGE.days
    return {
        "F7": f, "F7_n": fn, "FL7": l7, "FL7_n": l7n, "FL30": l30, "FL30_n": l30n,
        "perp_vol_med": perp_med, "spot_vol_med": spot_med, "listed_perp": bool(listed_perp),
        "listed_spot": bool(listed_spot), "age_days": age_days, "B24": b24, "B30med": b30,
        "elig_h1": bool(listed_perp and listed_spot and vol_ok_p and vol_ok_s and fn >= F7_MIN_HOURS),
        "elig_h2_7": bool(listed_perp and old and vol_ok_p and l7n >= H2_COVERAGE * 7 * 24),
        "elig_h2_30": bool(listed_perp and old and vol_ok_p and l30n >= H2_COVERAGE * 30 * 24),
    }


def _table(rows: dict) -> pd.DataFrame:
    df = pd.DataFrame.from_dict(rows, orient="index", columns=COLUMNS)
    df.index = pd.MultiIndex.from_tuples(df.index, names=["day", "asset"])
    return df.sort_index()


def hist_signal_table(inp, days: Iterable[pd.Timestamp]) -> pd.DataFrame:
    """Signals for every (decision day, asset) from historical inputs (``HistoricalInputs``)."""
    days = list(days)
    rows = {}
    for a in inp.assets:
        perp, spot = inp.perp.get(a), inp.spot.get(a)
        pvol = daily_volume(perp)
        svol = daily_volume(spot)
        basis = venue_basis(perp, spot)
        real_p = perp[~perp["filled"].astype(bool)] if perp is not None and len(perp) else None
        real_s = spot[~spot["filled"].astype(bool)] if spot is not None and len(spot) else None
        first_p = real_p.index.min() if real_p is not None and len(real_p) else None
        first_s = real_s.index.min() if real_s is not None and len(real_s) else None
        cache: dict[pd.Timestamp, float] = {}

        def b24_at(u, basis=basis, cache=cache):
            if u not in cache:
                cache[u] = b24_hist(basis, u)
            return cache[u]

        for t in days:
            listed_p = first_p is not None and first_p <= t - BAR
            listed_s = first_s is not None and first_s <= t - BAR
            age = (t - first_p) / DAY if listed_p else NAN
            rows[(t, a)] = _row(inp.funding.get(a), t, volume_median_from_daily(pvol, t),
                                volume_median_from_daily(svol, t), listed_p, listed_s, age,
                                b24_at(t), b30med(b24_at, t))
    return _table(rows)


def _assets(df: pd.DataFrame) -> list[str]:
    return list(df.index.get_level_values("asset").unique()) if len(df) else []


def _xs(df: pd.DataFrame, asset: str, cols: list[str]) -> pd.DataFrame:
    if asset in _assets(df):
        return df.xs(asset, level="asset")
    return pd.DataFrame(columns=cols, index=pd.DatetimeIndex([], tz="UTC"))


def forward_signal_table(fd, funding: dict[str, pd.DataFrame], days: Iterable[pd.Timestamp]) -> pd.DataFrame:
    """Signals from forward daily aggregates (``ForwardDaily``) and collector funding."""
    days = list(days)
    rows = {}
    assets = sorted(set(_assets(fd.exec)) | set(funding))
    for a in assets:
        ex = _xs(fd.exec, a, ["perp_vol_quote", "spot_vol_usd"])
        dl = _xs(fd.daily, a, ["basis_median", "basis_n"])
        pvol = pd.to_numeric(ex["perp_vol_quote"], errors="coerce")
        svol = pd.to_numeric(ex["spot_vol_usd"], errors="coerce")
        listing = fd.listing.get(a)

        def b24_at(u, dl=dl):
            prev = u.floor("D") - DAY
            if prev not in dl.index:
                return NAN
            r = dl.loc[prev]
            return float(r["basis_median"]) if r["basis_n"] >= B24_MIN_SNAPSHOTS else NAN

        for t in days:
            seen_p = pvol[(pvol.index < t.floor("D"))].dropna()
            seen_s = svol[(svol.index < t.floor("D"))].dropna()
            listed_p = listing is not None and listing <= t and len(seen_p) > 0
            listed_s = len(seen_s) > 0
            age = (t - listing) / DAY if listed_p else NAN
            rows[(t, a)] = _row(funding.get(a), t, forward_volume_median(pvol, t),
                                forward_volume_median(svol, t), listed_p, listed_s, age,
                                b24_at(t), b30med(b24_at, t))
    return _table(rows)
