"""Frozen forward data-quality rules Q1–Q9 (spec §7.2). Engineering thresholds from the collector's
design cadence; never tuned on results and never changed in V4.

Validity units:
- Q1–Q3 are pair-level (one ``fut_tickers`` + ``spot_ticker`` envelope with the same ``pair_seq``);
- Q4 is asset-level; a pair is valid for asset i when Q1–Q3 and Q4(i) pass;
- Q5 / Q6 / Q7 are (day, asset)-level; a day is DQ-valid for config c when every asset in c's eligible
  set at 00:00 (H5: H1 eligibility) and every asset held during the day passes, or, with an empty set,
  every asset of the §2a universe (``config_day_valid``). M2 is computed per config.
- Q8: an action uses the first pair within 15 minutes after t that is valid for all its leg assets.
- Q9: book walk only with a depth snapshot at most 10 minutes old, else top of book (counted).
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from typing import Any

import pandas as pd

from luna.forward.envelope import ns_from_iso

PAIR_SKEW_MS = 2000  # Q1
FRESH_MS = 5000  # Q2
CLOCK_S = 5  # Q3
SPOT_CLOCK_WINDOW = pd.Timedelta(minutes=5)  # Q3 (spot Time check every 5 minutes)
DAY_MIN_PAIRS = 1368  # Q5: of 1440 minute pairs
MAX_CONSECUTIVE_INVALID = 15  # Q6
FUNDING_SEEN_WITHIN = pd.Timedelta(hours=3)  # Q7
EXEC_WINDOW = pd.Timedelta(minutes=15)  # Q8
DEPTH_MAX_AGE = pd.Timedelta(minutes=10)  # Q9
MINUTES = 1440
_MS, _S = 1_000_000, 1_000_000_000


def _ns(iso: Any) -> int | None:
    try:
        return ns_from_iso(iso) if iso else None
    except (TypeError, ValueError):
        return None


def spot_clock_bad_windows(checks: Iterable[tuple[int, int]]) -> list[tuple[int, int]]:
    """``checks`` = [(recv_ns, server_unixtime_ns)] of ``spot_time``; a failed check (> 5 s) invalidates
    spot snapshots received in [recv, recv + 5 min)."""
    w = SPOT_CLOCK_WINDOW.value
    return sorted((r, r + w) for r, s in checks if abs(r - s) > CLOCK_S * _S)


def in_windows(t_ns: int, windows: list[tuple[int, int]]) -> bool:
    return any(a <= t_ns < b for a, b in windows)


def pair_ok(fut: dict, spot: dict | None, spot_clock_bad: list[tuple[int, int]] | None = None) -> bool:
    """Q1–Q3 for one pair of envelopes (``_recv_ns`` as streamed by ``iter_rows``)."""
    if not fut or not spot or not fut.get("ok") or not spot.get("ok"):
        return False
    rf, rs = fut["_recv_ns"], spot["_recv_ns"]
    if abs(rs - rf) > PAIR_SKEW_MS * _MS:  # Q1
        return False
    for env, recv in ((fut, rf), (spot, rs)):  # Q2
        req = _ns(env.get("req_ts"))
        if req is None or recv - req > FRESH_MS * _MS:
            return False
    server = _ns((fut.get("payload") or {}).get("serverTime"))  # Q3 futures
    if server is None or abs(rf - server) > CLOCK_S * _S:
        return False
    return not in_windows(rs, spot_clock_bad or [])  # Q3 spot


def quote_ok(bid: Any, ask: Any) -> bool:
    try:
        b, a = float(bid), float(ask)
    except (TypeError, ValueError):
        return False
    return math.isfinite(b) and math.isfinite(a) and b > 0 and a > 0 and b < a


def asset_ok(perp: dict | None, spot: dict | None) -> bool:
    """Q4 for asset i in a pair: present in both payloads, valid quotes, not suspended."""
    if perp is None or spot is None or perp.get("suspended"):
        return False
    return quote_ok(perp.get("bid"), perp.get("ask")) and quote_ok(spot.get("bid"), spot.get("ask"))


def day_asset_checks(valid_minutes: set[int]) -> dict:
    """Q5 (>= 1368 valid pairs) and Q6 (<= 15 consecutive invalid or missing minute pairs)."""
    longest = run = 0
    for m in range(MINUTES):
        run = 0 if m in valid_minutes else run + 1
        longest = max(longest, run)
    n = sum(1 for m in valid_minutes if 0 <= m < MINUTES)
    return {"valid_pairs": n, "longest_invalid": longest, "Q5": n >= DAY_MIN_PAIRS,
            "Q6": longest <= MAX_CONSECUTIVE_INVALID}


def funding_day_ok(first_seen: dict[int, int], day: pd.Timestamp) -> bool:
    """Q7: all 24 periods of ``day`` were observed in a ``fut_funding`` response within 3 hours of
    the period start (``first_seen`` = {period_start_ns: first recv_ns})."""
    lim = FUNDING_SEEN_WITHIN.value
    for h in range(24):
        t = (day + pd.Timedelta(hours=h)).value
        seen = first_seen.get(t)
        if seen is None or seen - t > lim:
            return False
    return True


def config_day_valid(day: pd.Timestamp, eligible: set[str], held: set[str],
                     asset_ok: dict[tuple[pd.Timestamp, str], bool], universe: Iterable[str]) -> bool:
    assets = set(eligible) | set(held)
    if not assets:
        assets = set(universe)
    return all(asset_ok.get((day, a), False) for a in assets)
