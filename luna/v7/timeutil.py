"""Time helpers: every V7 timestamp is an int64 ns UTC epoch (common rule; spec S-3)."""

from __future__ import annotations

import numbers

import pandas as pd

NS = 10**9  # nanoseconds per second
MIN = 60 * NS
H = 3600 * NS
D = 86400 * NS

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
