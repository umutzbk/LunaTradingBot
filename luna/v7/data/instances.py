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
from luna.v7.timeutil import MIN, D, ns_array

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
    t = ns_array(bars15["open_ts"], "open_ts")
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


def start_known_at(inst: Instance) -> int:
    """When an instance's start is known: its first volume > 0 bar is known at the bar close (R3 ``known_at``
    = open + 15 min)."""
    return int(inst.first_bar_ts) + BAR_15M


def current_instance(insts: list[Instance], t_ns: int) -> Instance | None:
    """PIT (DEC-44, L1): the latest instance whose first bar is known by t - 1 min (``first_bar_ts + 15 min
    <= t - 1 min``, DK-108 / red-team m5, user round 2 ruling R38: the volume > 0 that defines the first bar
    is known only at its close); None once ``end_known_at <= t``. Before that, the earlier instance (if not
    yet ended) stays current; in practice it has ended >= 7 d earlier, so the result is None."""
    cur = None
    for i in insts:
        if start_known_at(i) <= t_ns - MIN:
            cur = i
    if cur is None or (cur.end_known_at is not None and cur.end_known_at <= t_ns):
        return None
    return cur


def started_instance(insts: list[Instance], t_ns: int) -> Instance | None:
    """Attribution only, NOT a point-in-time membership: the latest instance with ``first_bar_ts <= t`` that
    has not ended by ``end_known_at``. Used to attribute a notice version to the instance live at its
    ``known_at`` (``notices.U6State``); the version is used only at decisions where that instance is
    ``current_instance`` (>= 16 min after its first bar), so no information is used before it is known."""
    cur = None
    for i in insts:
        if i.first_bar_ts <= t_ns:
            cur = i
    if cur is None or (cur.end_known_at is not None and cur.end_known_at <= t_ns):
        return None
    return cur


def instance_of(insts: list[Instance], ts_ns: int) -> int | None:
    """Membership of a row timestamp: ``first_bar_ts <= ts < last_trade_ts``; dormant intervals -> None.

    Retrospective: ``last_trade_ts`` is knowable only at ``end_known_at``, so this must not be used as-of t
    (use ``current_instance`` for point-in-time decisions). A timestamp exactly at the final bar close is
    outside the instance (AMB-04).
    """
    for i in insts:
        if i.first_bar_ts <= ts_ns and (i.last_trade_ts is None or ts_ns < i.last_trade_ts):
            return i.idx
    return None


def assign(df: pd.DataFrame, insts: list[Instance], ts_col: str) -> pd.Series:
    """Instance index per row (object Series, None for no instance), aligned to ``df.index``."""
    return pd.Series([instance_of(insts, int(x)) for x in df[ts_col]], index=df.index, dtype=object)
