"""Mutable-by-replacement risk state carried between ticks (kill switch, pause, day/peak equity)."""

from __future__ import annotations

import dataclasses
import math
from dataclasses import dataclass
from datetime import date

import pandas as pd


@dataclass
class RiskState:
    kill_switch: bool = False
    paused: bool = False
    day: date | None = None  # UTC date that day_start_equity / orders_today refer to
    day_start_equity: float | None = None
    peak_equity: float | None = None
    orders_today: int = 0
    entries_blocked_day: date | None = None  # R3: no new entries for the rest of this UTC day


def roll_day(state: RiskState, now: pd.Timestamp, equity: float) -> RiskState:
    """Return the state for decision time `now` (pure; `state` is not modified).

    On the first call on a new UTC day the day resets once: `day_start_equity = equity`,
    `orders_today = 0`,
    `entries_blocked_day = None`. Downtime across several midnights therefore resets exactly once, to the
    equity at the current tick. A timestamp earlier than `state.day` never resets. The peak
    always tracks `max(peak, equity)`. Non-finite equity raises ValueError (it would poison
    the day start and the peak). Kill switch and pause are preserved.
    """
    if now.tzinfo is None:
        raise ValueError("roll_day requires a tz-aware timestamp (UTC)")
    if not math.isfinite(equity):
        raise ValueError(f"roll_day requires finite equity, got {equity!r}")
    today = now.tz_convert("UTC").date()
    peak = equity if state.peak_equity is None else max(state.peak_equity, equity)
    if state.day is None or today > state.day:
        return dataclasses.replace(
            state,
            day=today,
            day_start_equity=equity,
            orders_today=0,
            entries_blocked_day=None,
            peak_equity=peak,
        )
    return dataclasses.replace(state, peak_equity=peak)
