"""Time helpers: every V7 timestamp is an int64 ns UTC epoch (common rule; spec S-3)."""

from __future__ import annotations

import numbers

import numpy as np
import pandas as pd

NS = 10**9  # nanoseconds per second
MIN = 60 * NS
H = 3600 * NS
D = 86400 * NS

# Plausibility window for ns timestamps given as floats (also the ingest bound, data/klines.to_ns_checked)
NS_LO = float(pd.Timestamp("2017-01-01", tz="UTC").value)
NS_HI = float(pd.Timestamp("2100-01-01", tz="UTC").value)

_MIN_NS = 10**15  # 10**15 ns is 1970-01-12; a smaller int is certainly seconds/ms by mistake


def to_ns(x: pd.Timestamp | str | int) -> int:
    """Timestamp / ISO string / int ns -> int ns UTC. Naive values are UTC. Small ints are ambiguous."""
    if isinstance(x, bool):
        raise ValueError("bool is not a timestamp")
    if isinstance(x, numbers.Integral):
        v = int(x)
        if abs(v) < _MIN_NS:
            raise ValueError(f"ambiguous time unit for int {v}: use from_ms / from_s")
        return v
    t = pd.Timestamp(x)
    t = t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")
    return int(t.value)


def from_ms(v: int) -> int:
    return int(v) * 10**6


def from_s(v: int) -> int:
    return int(v) * NS


def ts(ns: int) -> pd.Timestamp:
    return pd.Timestamp(int(ns), tz="UTC")


def ns_array(values, name: str) -> np.ndarray:
    """Array-like of ns timestamps -> int64 ndarray, refusing anything a plain cast would corrupt.

    Integer dtypes are accepted as-is (unsigned only if it fits int64). Floats (and numeric objects) are
    accepted only when every value is finite, integral and inside [2017-01-01, 2100-01-01) - the ingest
    plausibility window - so NaN / inf / 1e30 can never become a silent INT64_MIN. Anything else, including
    non-numeric or bool values, raises ValueError naming ``name``. Never emits a RuntimeWarning.
    """
    arr = np.asarray(values)
    if arr.dtype.kind == "b":
        raise ValueError(f"{name}: bool is not a timestamp")
    if arr.dtype.kind == "u":
        if arr.size and arr.max() > np.iinfo(np.int64).max:
            raise ValueError(f"{name}: value exceeds the int64 range")
        return arr.astype(np.int64)
    if arr.dtype.kind == "i":
        return arr.astype(np.int64)
    if arr.dtype.kind != "f":
        num = pd.to_numeric(pd.Series(arr.ravel()), errors="coerce")
        if num.dtype.kind in "iu":
            return ns_array(num.to_numpy().reshape(arr.shape), name)
        arr = num.to_numpy(np.float64).reshape(arr.shape)
    f = arr.astype(np.float64)
    if not np.isfinite(f).all():
        raise ValueError(f"{name}: missing / non-finite value")
    if (f != np.floor(f)).any():
        raise ValueError(f"{name}: non-integral ns value")
    if ((f < NS_LO) | (f >= NS_HI)).any():
        raise ValueError(f"{name}: value outside the plausible ns window [2017-01-01, 2100-01-01)")
    return f.astype(np.int64)
