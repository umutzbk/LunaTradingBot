"""Strategy protocol and the vectorized SignalFrame every strategy emits.

Signals are evaluated on CLOSED bar t (row i uses only bars <= i); the engine enters on bar t+1.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Literal, Protocol

import pandas as pd

from luna.portfolio.portfolio import Position
from luna.types import Candidate, ExitSpec, Leg, StopSpec


@dataclass
class SignalFrame:
    key: str
    legs_beta: pd.Series | None  # pairs: beta series for leg B (None -> single asset)
    entry: pd.Series  # int8: +1 long / -1 short / 0, evaluated at bar close (edge-triggered)
    exit_long: pd.Series  # bool
    exit_short: pd.Series  # bool
    stop_distance: pd.Series  # fraction of gross notional lost if stop hit, at entry
    stop_kind: Literal["price", "zscore"]
    stop_z: float | None  # zscore stops: fixed |z| level (None when the level is set per entry)
    z: pd.Series | None
    atr: pd.Series | None
    exit_spec: ExitSpec
    regime: pd.Series  # str per bar ("NONE" when not used)
    ref: pd.Series | None = None  # entry reference: close (single) or spread (pairs)
    features: dict[str, pd.Series] = field(default_factory=dict)  # extra per-bar candidate features


class Strategy(Protocol):
    name: str
    params: dict

    def signals(self, panel: dict[str, pd.DataFrame], keys: list[str]) -> dict[str, SignalFrame]:
        """Vectorized and causal."""
        ...

    def candidate_at(self, sf: SignalFrame, i: int, mids: dict[str, float]) -> Candidate | None:
        """Candidate from row i if entry != 0 and no NaN, else None."""
        ...

    def should_exit(
        self, sf: SignalFrame, i: int, pos: Position, bar_close: dict[str, float]
    ) -> str | None:
        """One of "signal" | "stop" | "time" | "trailing" | None."""
        ...


def edge_trigger(active: pd.Series, direction: pd.Series) -> pd.Series:
    """`direction` on bars where `active` turns True from a valid False; else 0 (int8)."""
    fired = active & (active.shift(1, fill_value=True) == False)  # noqa: E712
    return direction.where(fired, 0).fillna(0).astype("int8")


class SignalStrategy:
    """Shared candidate/exit logic over a SignalFrame. Subclasses provide `signals` and the hooks
    `_stop(sf, i, d, ref, dist)`, `_reason(d, feats)` and `_stop_hit(sf, i, pos, bar_close)` (or
    override `_stop_exit`), and `_legs` for multi-leg keys."""

    name: str
    params: dict

    def _legs(self, sf: SignalFrame, i: int) -> tuple[Leg, ...]:
        return (Leg(sf.key, 1.0),)

    def _stop(self, sf: SignalFrame, i: int, d: int, ref: float, dist: float) -> StopSpec:
        raise NotImplementedError

    def _reason(self, d: int, feats: dict[str, float]) -> str:
        raise NotImplementedError

    def _stop_hit(self, sf: SignalFrame, i: int, pos: Position, bar_close: dict[str, float]) -> bool:
        raise NotImplementedError

    def _stop_exit(
        self, sf: SignalFrame, i: int, pos: Position, bar_close: dict[str, float]
    ) -> str | None:
        return "stop" if self._stop_hit(sf, i, pos, bar_close) else None

    def candidate_at(self, sf: SignalFrame, i: int, mids: dict[str, float]) -> Candidate | None:
        d = int(sf.entry.iloc[i])
        if d == 0 or sf.ref is None:
            return None
        ref = float(sf.ref.iloc[i])
        dist = float(sf.stop_distance.iloc[i])
        feats = {"ref": ref, "stop_distance": dist}
        for name, ser in (("z", sf.z), ("atr", sf.atr), ("beta", sf.legs_beta), *sf.features.items()):
            if ser is not None:
                feats[name] = float(ser.iloc[i])
        if not all(math.isfinite(v) for v in feats.values()):
            return None
        return Candidate(
            ts=sf.entry.index[i],
            strategy=self.name,
            key=sf.key,
            legs=self._legs(sf, i),
            direction=d,
            entry_ref=ref,
            stop=self._stop(sf, i, d, ref, dist),
            exit=sf.exit_spec,
            regime=str(sf.regime.iloc[i]),
            reason=self._reason(d, feats),
            features=feats,
        )

    def should_exit(
        self, sf: SignalFrame, i: int, pos: Position, bar_close: dict[str, float]
    ) -> str | None:
        stop = self._stop_exit(sf, i, pos, bar_close)
        if stop:
            return stop
        flag = sf.exit_long if pos.direction > 0 else sf.exit_short
        if bool(flag.iloc[i]):
            return "signal"
        if pos.bars_held >= pos.exit.max_hold_bars:
            return "time"
        return None


class GridStrategy(SignalStrategy):
    """V1 strategy with a parameter grid (spec 5.1-5.3). ``params`` = ``_defaults`` updated with the
    given dict (normally one GRID point). ``use_regime=False`` skips the regime entry gate (the
    ``regime`` column is still filled for reporting): the "no_regime" ablation."""

    GRID: list[dict]
    _defaults: dict

    def __init__(self, params: dict | None = None, use_regime: bool = True):
        unknown = set(params or {}) - set(self._defaults)
        if unknown:
            raise ValueError(f"{self.name}: unknown params {sorted(unknown)}")
        self.params = {**self._defaults, **(params or {})}
        self.use_regime = use_regime

    @property
    def params_id(self) -> str:
        raise NotImplementedError
