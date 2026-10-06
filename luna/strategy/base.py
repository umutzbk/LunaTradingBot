"""Strategy protocol and the vectorized SignalFrame every strategy emits.

Signals are evaluated on CLOSED bar t (row i uses only bars <= i); the engine enters on bar t+1.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

import pandas as pd

from luna.portfolio.portfolio import Position
from luna.types import Candidate, ExitSpec


@dataclass
class SignalFrame:
    key: str
    legs_beta: pd.Series | None  # pairs: beta series for leg B (None -> single asset)
    entry: pd.Series  # int8: +1 long / -1 short / 0, evaluated at bar close (edge-triggered)
    exit_long: pd.Series  # bool
    exit_short: pd.Series  # bool
    stop_distance: pd.Series  # fraction of gross notional lost if stop hit, at entry
    stop_kind: Literal["price", "zscore"]
    stop_z: float | None  # zscore stops: |z| level
    z: pd.Series | None
    atr: pd.Series | None
    exit_spec: ExitSpec
    regime: pd.Series  # str per bar ("NONE" when not used)
    ref: pd.Series | None = None  # entry reference: close (single) or spread (pairs)


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
