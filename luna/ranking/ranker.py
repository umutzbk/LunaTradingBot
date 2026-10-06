"""Ranker protocol and the two non-learning rankers (accept-all baseline, random ablation)."""

from __future__ import annotations

import dataclasses
from typing import Protocol

import numpy as np
import pandas as pd

from luna.types import Candidate


class Ranker(Protocol):
    def score(self, cand: Candidate, ts: pd.Timestamp) -> Candidate | None:
        """Candidate with score/edge_bps filled, or None = rejected (caller records "ranker")."""
        ...


class AcceptAllRanker:
    """Accepts every candidate; score = 1 / stop_distance (tighter stop first), edge 0."""

    def score(self, cand: Candidate, ts: pd.Timestamp) -> Candidate | None:
        dist = cand.stop.distance_frac
        if not (np.isfinite(dist) and dist > 0):
            return None
        return dataclasses.replace(cand, score=1.0 / dist, edge_bps=0.0)


class RandomRanker:
    """Ablation ranker: accepts every candidate with a uniform random score (seeded, deterministic
    for a given call sequence)."""

    def __init__(self, seed: int) -> None:
        self._rng = np.random.default_rng(seed)

    def score(self, cand: Candidate, ts: pd.Timestamp) -> Candidate | None:
        return dataclasses.replace(cand, score=float(self._rng.random()), edge_bps=0.0)
