"""V2 signal strategies on causally resampled bars (plan P1-P3).

Each family/timeframe pair is one ``GridStrategy`` class built once per timeframe (``make_tsmom``,
``make_breakout``) with ``name = f"{family}_{tf}"`` and the timeframe's pre-committed grid. Signals
are computed on ``resample_causal`` bars and returned as a SPARSE ``SignalFrame`` indexed by the
higher-timeframe decision stamps (open time of the last 15m bar of each bucket), so the 15m engine,
research kernel and ranker decide only at higher-timeframe closes and fill on the next 15m open.
``max_hold`` is in 15m bars (the engine counts held bars on the 15m index). There is no regime gate:
``use_regime`` is accepted and ignored, the regime column is "NONE".

Exit hooks are the very functions of V1 strategies, so ``luna.research.tradelists._exit_rule`` maps
them without edits: TSMOM = naive momentum's fixed price stop (``STOP_PRICE``), breakout = momentum's
ATR trailing stop (``STOP_TRAIL``).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from luna.features.core import atr, ema
from luna.strategy.base import GridStrategy, SignalFrame, edge_trigger
from luna.strategy.momentum import Momentum
from luna.strategy.naive import NaiveMomentum
from luna.types import AccountKind, ExitSpec
from luna.v2.params import FAMILY_TIMEFRAMES, breakout_grid, tsmom_grid
from luna.v2.timeframe import Timeframe, resample_causal


class _HTFStrategy(GridStrategy):
    TF: Timeframe
    # modules outside the class hierarchy whose code shapes the signals (trade-list cache key)
    SOURCE_MODULES = ("luna/v2/timeframe.py", "luna/v2/params.py")

    def __init__(self, params: dict | None = None, use_regime: bool = True):
        super().__init__(params, use_regime)

    def _bars(self, panel: dict[str, pd.DataFrame], key: str) -> pd.DataFrame:
        return resample_causal(panel[key], self.TF)


class _TSMOM(_HTFStrategy):
    """EMA(fast) vs EMA(slow) cross on resampled closes; entry on the sign change, exit when the
    trend flips, fixed stop at close -/+ stop_atr x ATR, time stop."""

    _stop = staticmethod(NaiveMomentum._stop)
    _stop_hit = staticmethod(NaiveMomentum._stop_hit)

    @property
    def params_id(self) -> str:
        p = self.params
        return f"F{p['fast']}_S{p['slow']}_k{p['stop_atr']:g}"

    def signals(self, panel: dict[str, pd.DataFrame], keys: list[str]) -> dict[str, SignalFrame]:
        p = self.params
        out = {}
        for key in keys:
            bars = self._bars(panel, key)
            close = bars["close"]
            state = np.sign(ema(close, p["fast"]) - ema(close, p["slow"]))
            prev = state.shift(1)
            cross = state.notna() & prev.notna() & (state != prev) & (state != 0)
            a = atr(bars, p["atr_n"])
            out[key] = SignalFrame(
                key=key,
                legs_beta=None,
                entry=state.where(cross, 0).fillna(0).astype("int8"),
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

    def _reason(self, d, f):
        side = "long" if d > 0 else "short"
        return f"{self.name} EMA cross {side}: close={f['ref']:.4f} atr={f['atr']:.4f}"


class _Breakout(_HTFStrategy):
    """Close breaks the previous N bars' Donchian channel (edge-triggered); initial stop k x ATR,
    ATR trailing exit (momentum semantics), time stop. No signal exit."""

    _stop = Momentum._stop
    _stop_exit = Momentum._stop_exit

    @property
    def params_id(self) -> str:
        p = self.params
        return f"N{p['donchian']}_k{p['k_atr']:g}"

    def signals(self, panel: dict[str, pd.DataFrame], keys: list[str]) -> dict[str, SignalFrame]:
        p = self.params
        n = p["donchian"]
        out = {}
        for key in keys:
            bars = self._bars(panel, key)
            close = bars["close"]
            don_hi = bars["high"].rolling(n, min_periods=n).max().shift(1)
            don_lo = bars["low"].rolling(n, min_periods=n).min().shift(1)
            up = close > don_hi
            dn = close < don_lo
            a = atr(bars, p["atr_n"])
            never = pd.Series(False, index=close.index)
            out[key] = SignalFrame(
                key=key,
                legs_beta=None,
                entry=edge_trigger(up | dn, pd.Series(np.where(up, 1, -1), index=close.index)),
                exit_long=never,
                exit_short=never,
                stop_distance=p["k_atr"] * a / close,
                stop_kind="price",
                stop_z=None,
                z=None,
                atr=a,
                exit_spec=ExitSpec(max_hold_bars=p["max_hold"], trailing_atr_mult=p["k_atr"]),
                regime=pd.Series("NONE", index=close.index),
                ref=close,
                features={"donchian_high": don_hi, "donchian_low": don_lo},
            )
        return out

    def _reason(self, d, f):
        side = "long" if d > 0 else "short"
        lvl = f["donchian_high"] if d > 0 else f["donchian_low"]
        return f"{self.name} breakout {side}: close={f['ref']:.4f} channel={lvl:.4f} atr={f['atr']:.4f}"


_CACHE: dict[tuple[str, str], type[GridStrategy]] = {}


def _make(family: str, base: type, grid: list[dict], tf: Timeframe) -> type[GridStrategy]:
    key = (family, tf)
    if key not in _CACHE:
        qual = f"{family.upper()}_{tf}"
        _CACHE[key] = type(qual, (base,), {
            "__qualname__": qual, "__module__": __name__, "name": f"{family}_{tf}", "TF": tf,
            "GRID": grid, "_defaults": dict(grid[0]),
        })
        globals()[qual] = _CACHE[key]
    return _CACHE[key]


def make_tsmom(tf: Timeframe) -> type[GridStrategy]:
    return _make("tsmom", _TSMOM, tsmom_grid(tf), tf)


def make_breakout(tf: Timeframe) -> type[GridStrategy]:
    return _make("breakout", _Breakout, breakout_grid(tf), tf)


_MAKERS = {"tsmom": make_tsmom, "breakout": make_breakout}


def V2_STRATEGY_CLASSES(kind: AccountKind) -> dict[str, type[GridStrategy]]:  # noqa: N802
    """name -> class for every signal family x timeframe of ``kind`` (plan P1/P2)."""
    out: dict[str, type[GridStrategy]] = {}
    for family, maker in _MAKERS.items():
        for tf in FAMILY_TIMEFRAMES[kind][family]:
            cls = maker(tf)
            out[cls.name] = cls
    return out


# build every class at import so pickled instances (worker processes, checkpoints) resolve by name
for _kind in ("perp", "spot"):
    V2_STRATEGY_CLASSES(_kind)
