"""Activity-based contract instances and point-in-time membership (spec S-2.2, DEC-24/35/44, EC-1).

An instance ends after >= 7 consecutive UTC calendar days without a volume > 0 15-minute bar (missing
and zero-volume bars both count as inactive). ``last_trade_ts`` is the close of the last volume > 0 bar
and ``end_known_at = last_trade_ts + 7*24 h`` (EC-1, canonical): the end is known only once the
dormancy is complete. A relisting starts a new instance at the open of the first new volume > 0 bar.
A leading period before the first trade ends nothing: the first instance starts at the first archived
bar. No cross-instance computation anywhere.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from luna.v7.constants import FROZEN
from luna.v7.timeutil import MIN, D

BAR_15M = 15 * MIN


@dataclass(frozen=True)
class Instance:
    idx: int
    first_bar_ts: int
    last_trade_ts: int | None
    end_known_at: int | None


def build_instances(bars15: pd.DataFrame, data_end_ns: int) -> list[Instance]:
    """Instances from a normalised 15m frame (``open_ts`` ns, ``vol_base``); see module docstring."""
    if len(bars15) == 0:
        return []
    t = bars15["open_ts"].to_numpy(np.int64)
    act = np.sort(t[bars15["vol_base"].to_numpy(np.float64) > 0])
    if len(act) == 0:
        return []
    dorm = FROZEN.instances.dormancy_days
    days = np.unique(act // D)
    starts = [int(t.min())]
    ends: list[int | None] = []
    for d0, d1 in zip(days[:-1], days[1:], strict=True):
        if d1 - d0 - 1 >= dorm:
            ends.append(int(act[np.searchsorted(act, (d0 + 1) * D) - 1]) + BAR_15M)
            starts.append(int(act[np.searchsorted(act, d1 * D)]))
    trailing = int(data_end_ns) // D - (int(days[-1]) + 1)  # complete inactive UTC days before the data end
    ends.append(int(act[-1]) + BAR_15M if trailing >= dorm else None)
    return [
        Instance(i, s, e, None if e is None else e + dorm * D)
        for i, (s, e) in enumerate(zip(starts, ends, strict=True))
    ]


def current_instance(insts: list[Instance], t_ns: int) -> Instance | None:
    """PIT (DEC-44): latest instance with ``first_bar_ts <= t``; None once ``end_known_at <= t``."""
    cur = None
    for i in insts:
        if i.first_bar_ts <= t_ns:
            cur = i
    if cur is None or (cur.end_known_at is not None and cur.end_known_at <= t_ns):
        return None
    return cur


def instance_of(insts: list[Instance], ts_ns: int) -> int | None:
    """Membership of a row timestamp: ``first_bar_ts <= ts < last_trade_ts``; dormant intervals -> None."""
    for i in insts:
        if i.first_bar_ts <= ts_ns and (i.last_trade_ts is None or ts_ns < i.last_trade_ts):
            return i.idx
    return None


def assign(df: pd.DataFrame, insts: list[Instance], ts_col: str) -> pd.Series:
    """Instance index per row (object Series, None for no instance), aligned to ``df.index``."""
    return pd.Series([instance_of(insts, int(x)) for x in df[ts_col]], index=df.index, dtype=object)
