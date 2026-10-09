"""The closed V4 executable config set (spec R6, §3, §4) and its null models.

Exactly seven configs: H1-Y10, H1-Y20 (spot/perp cash-and-carry), H2-L7-k1, H2-L7-k2, H2-L30-k1,
H2-L30-k2 (perp-only cross-sectional funding carry), H5-HFWB (H1 mechanics + basis filter). Nulls
(N-RANDENTRY, N-ALWAYS, N-SHUFFLE) are benchmarks: never a config, never GO, never promoted.

Decisions read only the signal table row of the decision day (built from data before t, see
``luna.v4.signals``). Ties are broken alphabetically.
"""

from __future__ import annotations

import zlib
from dataclasses import dataclass

import numpy as np
import pandas as pd

from luna.v4.accounting import CarryDecision, CarryState, H2Decision
from luna.v4.constants import DAY

MAX_HOLD = pd.Timedelta(days=90)
COOLDOWN = pd.Timedelta(days=7)
MAX_CONCURRENT = 3
NULL_SEEDS = (1, 2, 3, 4, 5)
REDRAWS = 100


@dataclass(frozen=True)
class CarryConfig:
    id: str
    y: float
    basis_filter: bool = False
    account: str = "carry"


@dataclass(frozen=True)
class H2Config:
    id: str
    lookback: int
    k: int
    account: str = "h2"


CONFIGS: dict[str, CarryConfig | H2Config] = {
    "H1-Y10": CarryConfig("H1-Y10", 0.10),
    "H1-Y20": CarryConfig("H1-Y20", 0.20),
    "H2-L7-k1": H2Config("H2-L7-k1", 7, 1),
    "H2-L7-k2": H2Config("H2-L7-k2", 7, 2),
    "H2-L30-k1": H2Config("H2-L30-k1", 30, 1),
    "H2-L30-k2": H2Config("H2-L30-k2", 30, 2),
    "H5-HFWB": CarryConfig("H5-HFWB", 0.10, basis_filter=True),
}
H2_IDS = tuple(c for c, v in CONFIGS.items() if v.account == "h2")
CARRY_IDS = tuple(c for c, v in CONFIGS.items() if v.account == "carry")


def is_null(run_id: str) -> bool:
    return run_id.startswith("N-")


def null_ids(config_id: str) -> list[str]:
    if CONFIGS[config_id].account == "h2":
        return [f"N-SHUFFLE:{s}:{config_id}" for s in NULL_SEEDS]
    return [f"N-RANDENTRY:{s}:{config_id}" for s in NULL_SEEDS] + [f"N-ALWAYS:{config_id}"]


class _Table:
    def __init__(self, table: pd.DataFrame) -> None:
        self.by_day = {d: g.droplevel("day") for d, g in table.groupby(level="day")} if len(table) else {}

    def day(self, t: pd.Timestamp) -> pd.DataFrame:
        return self.by_day.get(t, pd.DataFrame())


class CarryRule(_Table):
    """H1 / H5 at 00:00 UTC daily: exits (F7 <= 0 or 90 days held) then entries (eligible, F7 >= Y,
    H5 also B24 >= B30med, 7-day cooldown), by F7 descending; the account fills free slots."""

    def __init__(self, cfg: CarryConfig, table: pd.DataFrame) -> None:
        super().__init__(table)
        self.cfg = cfg

    def decide(self, t: pd.Timestamp, state: CarryState) -> CarryDecision:
        rows = self.day(t)
        exits = []
        for a, entry in sorted(state.positions.items()):
            f7 = rows["F7"].get(a, np.nan) if len(rows) else np.nan
            if t - entry >= MAX_HOLD or (not np.isnan(f7) and f7 <= 0):
                exits.append(a)
        cands = []
        for a, r in rows.iterrows() if len(rows) else []:
            if a in state.positions or not r["elig_h1"] or not (r["F7"] >= self.cfg.y):
                continue
            if a in state.last_exit and t < state.last_exit[a] + COOLDOWN:
                continue
            if self.cfg.basis_filter and not (r["B24"] >= r["B30med"]):  # NaN -> no entry
                continue
            cands.append((-r["F7"], a))
        return CarryDecision(exits=exits, entries=[a for _, a in sorted(cands)])


class AlwaysRule(_Table):
    """N-ALWAYS: at D0 and on every month's 1st, target the first 3 eligible assets alphabetically;
    only changed assets trade. No F7 exit, no 90-day cap, no cooldown."""

    def __init__(self, table: pd.DataFrame, d0: pd.Timestamp) -> None:
        super().__init__(table)
        self.d0 = d0

    def decide(self, t: pd.Timestamp, state: CarryState) -> CarryDecision:
        if t != self.d0 and t.day != 1:
            return CarryDecision()
        rows = self.day(t)
        target = sorted(a for a, ok in rows["elig_h1"].items() if ok)[:MAX_CONCURRENT] if len(rows) else []
        return CarryDecision(exits=sorted(a for a in state.positions if a not in target),
                             entries=[a for a in target if a not in state.positions])


class ScheduleRule:
    """Replays a fixed entry / exit schedule (N-RANDENTRY)."""

    def __init__(self, schedule: list[dict]) -> None:
        self.schedule = schedule

    def decide(self, t: pd.Timestamp, state: CarryState) -> CarryDecision:
        held = state.positions
        exits = sorted(x["asset"] for x in self.schedule if x["exit_t"] == t and x["asset"] in held)
        entries = sorted(x["asset"] for x in self.schedule if x["entry_t"] == t)
        return CarryDecision(exits=exits, entries=entries)


def randentry_schedule(trades: list[dict], table: pd.DataFrame, days: list[pd.Timestamp], config_id: str,
                       seed: int) -> tuple[list[dict], int]:
    """N-RANDENTRY (§4): for every realised entry of the config (chronological), keep its holding time
    and draw the entry day uniformly from the evaluation days and the asset uniformly from that day's
    eligible set, with ``rng = default_rng([seed, crc32(config_id)])``. A draw breaking the 3-slot or
    7-day cooldown constraint is redrawn up to 100 times, then dropped (counted)."""
    rng = np.random.default_rng([seed, zlib.crc32(config_id.encode())])
    tab = _Table(table)
    end = days[-1] + DAY
    sched: list[dict] = []
    dropped = 0
    for tr in sorted(trades, key=lambda x: x["entry_t"]):
        dur = (tr["exit_t"] or end) - tr["entry_t"]
        for _ in range(1 + REDRAWS):
            day = days[int(rng.integers(len(days)))]
            rows = tab.day(day)
            elig = sorted(a for a, ok in rows["elig_h1"].items() if ok) if len(rows) else []
            if not elig:
                continue
            cand = {"asset": elig[int(rng.integers(len(elig)))], "entry_t": day, "exit_t": day + dur}
            if _fits(cand, sched):
                sched.append(cand)
                break
        else:
            dropped += 1
    return sorted(sched, key=lambda x: (x["entry_t"], x["asset"])), dropped


def _fits(c: dict, sched: list[dict]) -> bool:
    for y in sched:
        if y["asset"] == c["asset"] and not (c["entry_t"] >= y["exit_t"] + COOLDOWN
                                             or y["entry_t"] >= c["exit_t"] + COOLDOWN):
            return False
    # concurrency: positions overlapping c at any of its start times
    starts = [c["entry_t"]] + [y["entry_t"] for y in sched if c["entry_t"] <= y["entry_t"] < c["exit_t"]]
    for s in starts:
        live = sum(1 for y in sched if y["entry_t"] <= s < y["exit_t"])
        if live + 1 > MAX_CONCURRENT:
            return False
    return True


class H2Rule(_Table):
    """H2 on Mondays 00:00: among eligible assets with FL_L, long the k lowest and short the k highest
    (ties alphabetical); fewer than 2k eligible -> flat week. With ``shuffle_seed`` (N-SHUFFLE) the
    ranking is a permutation drawn with ``default_rng(crc32(f"{seed}:{config_id}:{t}"))``."""

    def __init__(self, cfg: H2Config, table: pd.DataFrame, shuffle_seed: int | None = None) -> None:
        super().__init__(table)
        self.cfg, self.seed = cfg, shuffle_seed

    def decide(self, t: pd.Timestamp) -> H2Decision | None:
        if t.weekday() != 0:
            return None
        rows = self.day(t)
        col, elig = f"FL{self.cfg.lookback}", f"elig_h2_{self.cfg.lookback}"
        ok = rows[rows[elig].astype(bool) & rows[col].notna()] if len(rows) else rows
        k = self.cfg.k
        if len(ok) < 2 * k:
            return H2Decision.flat()
        if self.seed is None:
            order = [a for _, a in sorted((float(v), a) for a, v in ok[col].items())]
        else:
            key = f"{self.seed}:{self.cfg.id}:{t.isoformat()}"
            order = list(np.random.default_rng(zlib.crc32(key.encode())).permutation(sorted(ok.index)))
        return H2Decision.rebalance({**{a: 1 for a in order[:k]}, **{a: -1 for a in order[-k:]}})
