"""Mean reversion (spec 5.2), single asset, long/short.

z = EWMA z-score of the close (span W). Entry at the close of bar t when |z| crosses above z_in,
against the move (long when z < -z_in); with ``use_regime`` only in RANGE (no entry in HIGH_VOL,
trend or UNKNOWN bars). Edge-triggered.

Exits, direction-aware:
- stop: z moves STOP_DZ = 1.5 further against the position than at entry (long entered at
  z = -2.2 stops at z <= -3.7; short at +2.2 stops at z >= +3.7). ``StopSpec.level`` holds that
  signed z level; ``stop_distance = 1.5 * ewm_std(close, W) / close`` at entry.
- take profit ("signal"): the z-score has reverted to within z_out of the mean, i.e.
  z >= -z_out for a long and z <= z_out for a short (``|z| < z_out`` plus any overshoot through
  the band, so that z_out = 0 means "z crossed the mean").
- time stop after 4 * W bars.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

from luna.features.core import ewma_zscore
from luna.portfolio.portfolio import Position
from luna.regime.rules import classify_regime
from luna.strategy.base import GridStrategy, SignalFrame, edge_trigger
from luna.types import ExitSpec, StopSpec

STOP_DZ = 1.5


class MeanReversion(GridStrategy):
    name = "mean_reversion"
    GRID = [
        {"W": w, "z_in": zi, "z_out": zo}
        for w in (96, 288)
        for zi in (2.0, 2.5)
        for zo in (0.0, 0.5)
    ]
    _defaults = {"W": 96, "z_in": 2.0, "z_out": 0.5}

    @property
    def params_id(self) -> str:
        p = self.params
        return f"W{p['W']}_zin{float(p['z_in'])}_zout{float(p['z_out'])}"

    def _max_hold(self) -> int:
        return 4 * self.params["W"]

    def _gate(self, regime: pd.Series) -> pd.Series:
        return regime == "RANGE"

    def signals(self, panel: dict[str, pd.DataFrame], keys: list[str]) -> dict[str, SignalFrame]:
        out = {}
        for key in keys:
            bars = panel[key]
            close = bars["close"]
            w = self.params["W"]
            z = ewma_zscore(close, w)
            sigma = close.ewm(span=w, min_periods=w, adjust=False).std()
            out[key] = self._frame(key, close, z, sigma / close, None, classify_regime(bars))
        return out

    def _frame(self, key, level, z, rel_sigma, beta, regime) -> SignalFrame:
        """z-score frame on `level` (close or spread); stop_distance = STOP_DZ * rel_sigma, with
        rel_sigma the EWMA std as a fraction of gross notional."""
        p = self.params
        active = z.abs() > p["z_in"]
        if self.use_regime:
            active &= self._gate(regime)
        return SignalFrame(
            key=key,
            legs_beta=beta,
            entry=edge_trigger(active, -np.sign(z)),
            exit_long=(z >= -p["z_out"]).fillna(False),
            exit_short=(z <= p["z_out"]).fillna(False),
            stop_distance=STOP_DZ * rel_sigma,
            stop_kind="zscore",
            stop_z=None,  # per entry: StopSpec.level = z_entry -/+ STOP_DZ
            z=z,
            atr=None,
            exit_spec=ExitSpec(max_hold_bars=self._max_hold(), take_profit_z=p["z_out"]),
            regime=regime,
            ref=level,
        )

    def _stop(self, sf, i, d, ref, dist):
        # long (d=+1) entered at z<0 stops STOP_DZ lower; short mirrored
        return StopSpec("zscore", float(sf.z.iloc[i]) - d * STOP_DZ, dist)

    def _reason(self, d, f):
        side = "long" if d > 0 else "short"
        return (f"mean reversion {side}: z={f['z']:.2f} beyond z_in={self.params['z_in']}, "
                f"close={f['ref']:.4f}, stop z={f['z'] - d * STOP_DZ:.2f}")

    def _stop_hit(self, sf: SignalFrame, i: int, pos: Position, bar_close: dict[str, float]) -> bool:
        z = float(sf.z.iloc[i])
        return math.isfinite(z) and pos.direction * (z - pos.stop.level) <= 0
