"""Pairs / relative value (spec 5.3), two legs, market-neutral. Keys are "A/B".

beta_t = rolling OLS beta of log A on log B over W bars (``rolling_beta``); spread
s_t = log A_t - beta_t * log B_t; z_t = EWMA z-score (span W) of the spread history evaluated with
that bar's beta_t (``spread_zscore``; a per-bar beta would otherwise inject d(beta) * log B jumps
into the spread). Entry when |z| crosses above z_in, against the spread (long spread = long A,
short beta*B). Legs (A, +1), (B, -beta) with beta frozen at the entry bar.
``stop_distance = 1.5 * sigma_spread`` (EWMA std of that spread) at entry, in log units ~
fractional return of the pair. Exits as mean reversion (direction-aware z stop STOP_DZ, take
profit at z_out); time stop 288 bars.

Exits read z_t computed with the current beta_t, while the open legs keep the beta frozen at entry.
This is deliberate: z_t is the best current estimate of how far the pair is from equilibrium (the
signal the position is betting on), and re-hedging the legs every bar to follow beta_t would add
turnover and costs, so the hedge ratio is fixed for the life of the trade. The stop/TP therefore
judge the relationship, not the exact P&L of the frozen hedge; the max_hold time stop bounds the
drift between the two.

Regime: classify_regime of leg A, reindexed to the pair index. With ``use_regime`` there is no
direction gate; only HIGH_VOL (and UNKNOWN) bars block entries. Pairs is skipped for spot accounts
by the caller (it needs a short leg); ``signals`` itself works regardless.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from luna.features.core import rolling_beta
from luna.features.pairs import spread, spread_zscore
from luna.regime.rules import classify_regime
from luna.strategy.base import SignalFrame
from luna.strategy.mean_reversion import STOP_DZ, MeanReversion
from luna.types import Leg


class Pairs(MeanReversion):
    name = "pairs"
    GRID = [
        {"W": w, "z_in": zi, "z_out": zo}
        for w in (96, 288, 672)
        for zi in (1.5, 2.0, 2.5)
        for zo in (0.0, 0.5)
    ]
    _defaults = {"W": 288, "z_in": 2.0, "z_out": 0.5, "max_hold": 288}

    def _max_hold(self) -> int:
        return self.params["max_hold"]

    def _gate(self, regime: pd.Series) -> pd.Series:
        return ~regime.isin(["HIGH_VOL", "UNKNOWN"])

    def signals(self, panel: dict[str, pd.DataFrame], keys: list[str]) -> dict[str, SignalFrame]:
        out = {}
        for key in keys:
            a, b = key.split("/")
            idx = panel[a].index.intersection(panel[b].index)
            log_a = np.log(panel[a]["close"].reindex(idx))
            log_b = np.log(panel[b]["close"].reindex(idx))
            w = self.params["W"]
            beta = rolling_beta(log_a, log_b, w)
            z, sigma = spread_zscore(log_a, log_b, beta, w)
            regime = classify_regime(panel[a]).reindex(idx)
            out[key] = self._frame(key, spread(log_a, log_b, beta), z, sigma, beta, regime)
        return out

    def _legs(self, sf, i):
        a, b = sf.key.split("/")
        return (Leg(a, 1.0), Leg(b, -float(sf.legs_beta.iloc[i])))

    def _reason(self, d, f):
        side = "long" if d > 0 else "short"
        return (f"pairs {side} spread: z={f['z']:.2f} beyond z_in={self.params['z_in']}, "
                f"beta={f['beta']:.3f}, spread={f['ref']:.5f}, stop z={f['z'] - d * STOP_DZ:.2f}")
