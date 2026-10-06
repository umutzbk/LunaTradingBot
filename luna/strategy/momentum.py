"""Momentum / trend (spec 5.1), single asset, long/short.

Entry at the close of bar t when trend_strength = (EMA(fast) - EMA(slow)) / ATR(96) has |.| > 0.5
and the close breaks the Donchian(N) channel of the previous N bars (high for long, low for short)
in the same direction; with ``use_regime`` the bar's regime must be TREND_UP (long) / TREND_DOWN
(short) or HIGH_VOL (either side, direction from the signed trend strength; the constructor halves
the risk budget there). RANGE and UNKNOWN block entries. Edge-triggered: fires when that condition
turns on.

Stop price = close -/+ k_atr * ATR(96) (``stop_distance = k_atr * ATR / close``). Exits: ATR
trailing stop (level = max(previous level, close - k_atr * ATR) for a long, mirrored for a short,
starting from the entry stop; kept on ``Position.trail_level``) and a 2,880-bar time stop.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

from luna.features.core import atr, trend_strength
from luna.portfolio.portfolio import Position
from luna.regime.rules import classify_regime
from luna.strategy.base import GridStrategy, SignalFrame, edge_trigger
from luna.types import ExitSpec, StopSpec


class Momentum(GridStrategy):
    name = "momentum"
    GRID = [
        {"fast": f, "slow": s, "donchian": n, "k_atr": k}
        for f, s in ((48, 192), (96, 480))
        for n in (96, 288)
        for k in (2, 3)
    ]
    _defaults = {
        "fast": 96, "slow": 480, "donchian": 96, "k_atr": 2,
        "ts_threshold": 0.5, "atr_n": 96, "max_hold": 2880,
    }

    @property
    def params_id(self) -> str:
        p = self.params
        return f"F{p['fast']}_S{p['slow']}_N{p['donchian']}_k{p['k_atr']:g}"

    def signals(self, panel: dict[str, pd.DataFrame], keys: list[str]) -> dict[str, SignalFrame]:
        p = self.params
        out = {}
        for key in keys:
            bars = panel[key]
            close = bars["close"]
            ts = trend_strength(close, bars, p["fast"], p["slow"], p["atr_n"])
            a = atr(bars, p["atr_n"])
            n = p["donchian"]
            don_hi = bars["high"].rolling(n, min_periods=n).max().shift(1)
            don_lo = bars["low"].rolling(n, min_periods=n).min().shift(1)
            regime = classify_regime(bars)
            up = (ts > p["ts_threshold"]) & (close > don_hi)
            dn = (ts < -p["ts_threshold"]) & (close < don_lo)
            if self.use_regime:
                # HIGH_VOL takes precedence in classify_regime but does not block momentum (spec
                # 5.4: only its risk budget is halved, by the constructor's regime_mult)
                up &= regime.isin(["TREND_UP", "HIGH_VOL"])
                dn &= regime.isin(["TREND_DOWN", "HIGH_VOL"])
            direction = pd.Series(np.where(up, 1, -1), index=close.index)
            never = pd.Series(False, index=close.index)
            out[key] = SignalFrame(
                key=key,
                legs_beta=None,
                entry=edge_trigger(up | dn, direction),
                exit_long=never,
                exit_short=never,
                stop_distance=p["k_atr"] * a / close,
                stop_kind="price",
                stop_z=None,
                z=None,
                atr=a,
                exit_spec=ExitSpec(max_hold_bars=p["max_hold"], trailing_atr_mult=p["k_atr"]),
                regime=regime,
                ref=close,
                features={"trend_strength": ts, "donchian_high": don_hi, "donchian_low": don_lo},
            )
        return out

    def _stop(self, sf, i, d, ref, dist):
        # dist = k*ATR/close, so level = close -/+ k*ATR
        return StopSpec("price", ref * (1.0 - d * dist), dist)

    def _reason(self, d, f):
        if d > 0:
            brk = f"close={f['ref']:.4f} > donchian({self.params['donchian']}) high={f['donchian_high']:.4f}"
        else:
            brk = f"close={f['ref']:.4f} < donchian({self.params['donchian']}) low={f['donchian_low']:.4f}"
        side = "long" if d > 0 else "short"
        return (f"momentum {side}: trend_strength={f['trend_strength']:.2f} "
                f"(|.|>{self.params['ts_threshold']}), {brk}, atr={f['atr']:.4f}")

    def _stop_exit(
        self, sf: SignalFrame, i: int, pos: Position, bar_close: dict[str, float]
    ) -> str | None:
        """Ratchet the trailing level with this bar's close/ATR, then exit if the close is at or
        beyond it: "stop" while the level is still the entry stop, "trailing" once it has moved."""
        px = bar_close.get(pos.key)
        if px is None:
            return None
        d = pos.direction
        level = pos.trail_level if pos.trail_level is not None else pos.stop.level
        a = float(sf.atr.iloc[i])
        k = pos.exit.trailing_atr_mult
        if k is not None and math.isfinite(a):
            cand = px - d * k * a
            level = max(level, cand) if d > 0 else min(level, cand)
        pos.trail_level = level
        if d * (px - level) <= 0:
            return "stop" if level == pos.stop.level else "trailing"
        return None
