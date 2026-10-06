"""Hierarchical-shrinkage expectancy table (strategy -> key -> regime) and the ranker built on it."""

from __future__ import annotations

import dataclasses
import math
from dataclasses import dataclass

import numpy as np
import pandas as pd

from luna.research.stats import block_bootstrap_ci
from luna.types import Candidate

ANY = "*"
_SEED = 0


@dataclass
class CellStat:
    n: int
    mean_r: float
    lo_r: float
    hi_r: float
    shrunk_r: float
    accepted: bool
    reason: str


def _shrink(n: int, mean: float, prior: float, n0: float) -> float:
    w = n / (n + n0)
    return w * mean + (1.0 - w) * prior


def fit_expectancy(
    trades: pd.DataFrame, n0: float, n_min: int, n_boot: int, ci: float
) -> dict[tuple[str, str, str], CellStat]:
    """Fit the table from already-filtered trades (columns strategy, key, regime, r_net, day).

    Keys: (strategy, "*", "*"), (strategy, key, "*") and (strategy, key, regime). The strategy level
    shrinks toward 0, each lower level toward its parent's shrunk value. The bootstrap CI (whole
    days resampled, vectorized) and the acceptance test live at (strategy, key); regime cells are
    accepted iff their parent is and their own shrunk_r > 0. CI fields of other levels are NaN.
    """
    table: dict[tuple[str, str, str], CellStat] = {}
    nan = math.nan
    for strat, sdf in trades.groupby("strategy", sort=True):
        s = str(strat)
        n_s = len(sdf)
        mean_s = float(sdf["r_net"].mean())
        shrunk_s = _shrink(n_s, mean_s, 0.0, n0)
        table[(s, ANY, ANY)] = CellStat(n_s, mean_s, nan, nan, shrunk_s, False, "strategy level")
        for key, kdf in sdf.groupby("key", sort=True):
            k = str(key)
            n_k = len(kdf)
            _, lo, hi = block_bootstrap_ci(
                kdf["r_net"].to_numpy(float), kdf["day"].to_numpy(), n_boot, ci, _SEED
            )
            mean_k = float(kdf["r_net"].mean())
            shrunk_k = _shrink(n_k, mean_k, shrunk_s, n0)
            if n_k < n_min:
                accepted, reason = False, f"n<{n_min}"
            elif not lo > 0:
                accepted, reason = False, "ci_lo<=0"
            elif not shrunk_k > 0:
                accepted, reason = False, "shrunk<=0"
            else:
                accepted, reason = True, "ok"
            table[(s, k, ANY)] = CellStat(n_k, mean_k, lo, hi, shrunk_k, accepted, reason)
            for reg, rdf in kdf.groupby("regime", sort=True):
                n_r = len(rdf)
                mean_r = float(rdf["r_net"].mean())
                shrunk_r = _shrink(n_r, mean_r, shrunk_k, n0)
                if not accepted:
                    r_acc, r_reason = False, f"parent: {reason}"
                elif not shrunk_r > 0:
                    r_acc, r_reason = False, "shrunk<=0"
                else:
                    r_acc, r_reason = True, "ok"
                table[(s, k, str(reg))] = CellStat(n_r, mean_r, nan, nan, shrunk_r, r_acc, r_reason)
    return table


class ExpectancyRanker:
    """Scores a candidate by the shrunk expectancy (R units) of its (strategy, key, regime) cell."""

    def __init__(
        self, table: dict[tuple[str, str, str], CellStat], params_id: dict[str, str]
    ) -> None:
        self.table = table
        self.params_id = params_id  # strategy -> selected params id (reporting only)

    def score(self, cand: Candidate, ts: pd.Timestamp) -> Candidate | None:
        cell = self.table.get((cand.strategy, cand.key, cand.regime))
        if cell is None:
            cell = self.table.get((cand.strategy, cand.key, ANY))
        if cell is None or not cell.accepted or not cell.shrunk_r > 0:
            return None
        dist = cand.stop.distance_frac
        if not (np.isfinite(dist) and dist > 0):
            return None
        return dataclasses.replace(cand, score=cell.shrunk_r, edge_bps=cell.shrunk_r * dist * 1e4)
