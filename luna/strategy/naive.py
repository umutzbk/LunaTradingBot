"""Naive baselines (spec 8.1): momentum EMA cross, EWMA-z mean reversion, ETH/BTC pairs z-score.

Stop distances: momentum = 3*ATR/close (fraction). Mean reversion = (stop_z - enter_z) * ewm_std
/ close at entry. Pairs = (stop_z - enter_z) * ewm_std of the log spread (log units ~ fractional
return of the pair).
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

from luna.features.core import atr, ema, ewma_zscore
from luna.features.pairs import spread
from luna.strategy.base import SignalFrame, SignalStrategy, edge_trigger
from luna.types import ExitSpec, Leg, StopSpec


class _Naive(SignalStrategy):
    name: str
    _defaults: dict

    def __init__(self, params: dict | None = None):
        self.params = {**self._defaults, **(params or {})}


class NaiveMomentum(_Naive):
    name = "naive_momentum"
    _defaults = {"fast": 96, "slow": 480, "atr_n": 96, "stop_atr": 3.0, "max_hold": 2880}

    def signals(self, panel: dict[str, pd.DataFrame], keys: list[str]) -> dict[str, SignalFrame]:
        p = self.params
        out = {}
        for key in keys:
            bars = panel[key]
            close = bars["close"]
            fast, slow = ema(close, p["fast"]), ema(close, p["slow"])
            state = np.sign(fast - slow)  # +1 / -1 / 0, NaN in warm-up
            prev = state.shift(1)
            cross = state.notna() & prev.notna() & (state != prev) & (state != 0)
            entry = state.where(cross, 0).fillna(0).astype("int8")
            a = atr(bars, p["atr_n"])
            out[key] = SignalFrame(
                key=key,
                legs_beta=None,
                entry=entry,
                exit_long=(state < 0).fillna(False),
                exit_short=(state > 0).fillna(False),
                stop_distance=p["stop_atr"] * a / close,
                stop_kind="price",
                stop_z=None,
                z=None,
                atr=a,
                exit_spec=ExitSpec(max_hold_bars=p["max_hold"]),
                regime=pd.Series("NONE", index=close.index),
                ref=close,
            )
        return out

    @staticmethod
    def _stop(sf, i, d, ref, dist):
        # dist = k*ATR/close, so level = entry -/+ k*ATR
        return StopSpec("price", ref * (1.0 - d * dist), dist)

    @staticmethod
    def _reason(d, f):
        side = "long" if d > 0 else "short"
        return f"EMA cross {side}: close={f['ref']:.4f} atr={f['atr']:.4f}"

    @staticmethod
    def _stop_hit(sf, i, pos, bar_close):
        px = bar_close.get(pos.key)
        return px is not None and pos.direction * (px - pos.stop.level) <= 0


class NaiveMeanReversion(_Naive):
    name = "naive_mean_reversion"
    _defaults = {"span": 96, "enter_z": 2.0, "exit_z": 0.5, "stop_z": 3.5, "max_hold": 288}

    def signals(self, panel: dict[str, pd.DataFrame], keys: list[str]) -> dict[str, SignalFrame]:
        out = {}
        for key in keys:
            close = panel[key]["close"]
            out[key] = self._frame(key, close, None, scale=close)
        return out

    def _frame(self, key, level, beta, scale) -> SignalFrame:
        """z-score frame on `level` (close or spread); stop_distance = (stop_z-enter_z)*sigma/scale."""
        p = self.params
        z = ewma_zscore(level, p["span"])
        sigma = level.ewm(span=p["span"], min_periods=p["span"], adjust=False).std()
        entry = edge_trigger(z.abs() > p["enter_z"], -np.sign(z))
        small = (z.abs() < p["exit_z"]).fillna(False)
        return SignalFrame(
            key=key,
            legs_beta=beta,
            entry=entry,
            exit_long=small,
            exit_short=small,
            stop_distance=(p["stop_z"] - p["enter_z"]) * sigma / scale,
            stop_kind="zscore",
            stop_z=p["stop_z"],
            z=z,
            atr=None,
            exit_spec=ExitSpec(max_hold_bars=p["max_hold"], take_profit_z=p["exit_z"]),
            regime=pd.Series("NONE", index=level.index),
            ref=level,
        )

    @staticmethod
    def _stop(sf, i, d, ref, dist):
        return StopSpec("zscore", float(sf.stop_z), dist)

    @staticmethod
    def _reason(d, f):
        side = "long" if d > 0 else "short"
        return f"z={f['z']:.2f} crossed entry band, revert {side}: ref={f['ref']:.4f}"

    @staticmethod
    def _stop_hit(sf, i, pos, bar_close):
        z = float(sf.z.iloc[i])
        return math.isfinite(z) and abs(z) > float(sf.stop_z)


class NaivePairs(NaiveMeanReversion):
    name = "naive_pairs"
    _defaults = {
        "span": 288, "enter_z": 2.0, "exit_z": 0.5, "stop_z": 3.5, "max_hold": 288, "beta": 1.0,
    }

    def signals(self, panel: dict[str, pd.DataFrame], keys: list[str]) -> dict[str, SignalFrame]:
        out = {}
        for key in keys:
            a, b = key.split("/")
            idx = panel[a].index.intersection(panel[b].index)
            beta = pd.Series(float(self.params["beta"]), index=idx)
            s = spread(np.log(panel[a]["close"].reindex(idx)),
                       np.log(panel[b]["close"].reindex(idx)), beta)
            out[key] = self._frame(key, s, beta, scale=1.0)  # log units ~ fractional return
        return out

    def _legs(self, sf, i):
        a, b = sf.key.split("/")
        return (Leg(a, 1.0), Leg(b, -float(sf.legs_beta.iloc[i])))

    @staticmethod
    def _reason(d, f):
        side = "long" if d > 0 else "short"
        return f"spread z={f['z']:.2f} beyond entry band, {side} spread: spread={f['ref']:.5f}"
