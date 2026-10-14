"""Kline normalisation and monthly-union-daily reconstruction (spec S-3 R3/R5, S-2.2 DEC-33, L12).

``normalise`` turns raw Binance kline rows (open_time, open, high, low, close, volume, close_time,
quote_volume, count, taker_buy_volume, taker_buy_quote_volume, ignore) into ns-time frames; the unit is
explicit and implausible values are refused (ambiguous units are never ingested). ``union`` = monthly union
daily by key, daily filling keys missing from monthly; a differing value for the same key is an
``IntegrityBlocker``.
"""

from __future__ import annotations

import io
from typing import Literal

import pandas as pd

from luna.v7.data.schema import IntegrityBlocker
from luna.v7.timeutil import MIN

RAW_COLUMNS = ("open_time", "open", "high", "low", "close", "volume", "close_time", "quote_volume", "count",
               "taker_buy_volume", "taker_buy_quote_volume", "ignore")
VALUE_COLUMNS = ("o", "h", "l", "c", "vol_base", "vol_quote", "n_trades", "taker_buy_quote")
OUT_COLUMNS = ("open_ts", *VALUE_COLUMNS, "known_at")
_MULT = {"ms": 10**6, "us": 10**3, "ns": 1}
_LO = float(pd.Timestamp("2017-01-01", tz="UTC").value)
_HI = float(pd.Timestamp("2100-01-01", tz="UTC").value)
_RENAME = {"open": "o", "high": "h", "low": "l", "close": "c", "volume": "vol_base",
           "quote_volume": "vol_quote", "count": "n_trades", "taker_buy_quote_volume": "taker_buy_quote"}


def to_ns_checked(values: pd.Series, unit: str) -> pd.Series:
    """Raw integer times in ``unit`` -> int64 ns; refuse values implausible for that unit (L12)."""
    if unit not in _MULT:
        raise ValueError(f"unit must be one of ms/us/ns, got {unit!r}")
    v = pd.to_numeric(values)
    if v.isna().any():
        raise ValueError("missing time values")
    mult = _MULT[unit]
    f = v.astype("float64") * mult  # float space first so a wrong unit cannot overflow int64
    if ((f < _LO) | (f >= _HI)).any():
        raise ValueError(f"time values implausible for unit {unit!r} (ambiguous or wrong unit)")
    return v.astype("int64") * mult


def parse_csv(text: str) -> pd.DataFrame:
    """Raw archive kline CSV text (header row optional) -> frame with the raw column names."""
    df = pd.read_csv(io.StringIO(text), header=None, dtype=str)
    if len(df) and not str(df.iloc[0, 0]).strip().lstrip("-").isdigit():
        df = df.iloc[1:]
    df = df.iloc[:, : len(RAW_COLUMNS)].copy()
    df.columns = list(RAW_COLUMNS)[: df.shape[1]]
    return df.reset_index(drop=True)


def normalise(df: pd.DataFrame, unit: Literal["ms", "us", "ns"], bar_ns: int = 15 * MIN) -> pd.DataFrame:
    """Raw kline frame -> ``open_ts, o, h, l, c, vol_base, vol_quote, n_trades, taker_buy_quote, known_at``.

    ``known_at = open_ts + bar_ns`` (R3: 15 min, the default; R5: pass ``MIN``). An optional ``symbol`` column
    is kept (first).
    """
    out = pd.DataFrame({"open_ts": to_ns_checked(df["open_time"], unit).to_numpy()})
    for raw, new in _RENAME.items():
        out[new] = pd.to_numeric(df[raw]).to_numpy()
    out["n_trades"] = out["n_trades"].astype("int64")
    out["known_at"] = out["open_ts"] + int(bar_ns)
    if "symbol" in df.columns:
        out.insert(0, "symbol", df["symbol"].to_numpy())
    return out.reset_index(drop=True)


def union(monthly: pd.DataFrame, daily: pd.DataFrame) -> pd.DataFrame:
    """Monthly union daily by (symbol, open_ts); daily fills missing keys, conflicts raise (DEC-33).

    Inputs are ``normalise`` outputs. Result is sorted by key and indexed by ``open_ts`` (ns).
    """
    key = ["symbol", "open_ts"] if "symbol" in monthly.columns or "symbol" in daily.columns else ["open_ts"]
    for name, f in (("monthly", monthly), ("daily", daily)):
        if f.duplicated(key).any():
            raise IntegrityBlocker(f"duplicate keys inside {name} klines")
    both = monthly.merge(daily, on=key, how="inner", suffixes=("_m", "_d"))
    for c in VALUE_COLUMNS:
        bad = both[both[f"{c}_m"] != both[f"{c}_d"]]
        if len(bad):
            raise IntegrityBlocker(
                f"kline conflict monthly vs daily in {c} for {len(bad)} key(s), first: "
                f"{bad[key].iloc[0].to_dict()}"
            )
    extra = daily.merge(monthly[key], on=key, how="left", indicator=True)
    extra = extra[extra["_merge"] == "left_only"].drop(columns="_merge")
    out = pd.concat([monthly, extra], ignore_index=True).sort_values(key, kind="stable")
    return out.set_index("open_ts", drop=False).rename_axis(None)
