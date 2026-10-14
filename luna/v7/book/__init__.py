"""Book decision objects shared by the constructor, the risk layer and the hedge (spec S-7.1, S-7.3, S-7.4).

Units: every notional is in book units (L-norm: fractions of E = 1, fractional, no rounding; §14). Notionals
are absolute (> 0); a leg's direction is ``side`` (+1 long, -1 short). Times are int64 ns UTC.
These modules are pure: no I/O, no wall-clock; the engine (Task 14b) owns the state and the event loop.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

LONG, SHORT = 1, -1
SIDES = (LONG, SHORT)

# Relative tolerance of every threshold comparison on computed sums / ratios (a strict "> x" holds only above
# x by more than EPS x scale), so float noise never flips an exact-boundary case (AMB-16 (k)).
EPS = 1e-12

OrderKind = Literal["open", "close", "reduce"]


def check_side(side: int) -> int:
    if side not in SIDES:
        raise ValueError(f"side must be +1 or -1, got {side!r}")
    return int(side)


def positive(x: float, name: str) -> float:
    v = float(x)
    if not (math.isfinite(v) and v > 0):
        raise ValueError(f"{name} must be finite and > 0, got {x!r}")
    return v


@dataclass(frozen=True)
class Leg:
    """A held perp leg at a decision (or tick).

    ``notional``: current |q| x price at the evaluation time (book units, > 0). ``avg_entry``: the average
    entry fill price (> 0; resizes are reduce-only, so it is the entry fill price). ``entry_decision_ts``: the
    decision that opened the leg (minimum hold is measured from it, AMB-16 (g)). ``pending_exit``: the reason
    of an exit already issued but not yet filled; such a leg gets no new order and still counts as held (slot,
    flip block, ``post``, hedge beta). Two uses: (1) a halted exit deferred to the first executable bar of the
    same instance (S-5.4); (2) a forced risk exit triggered between decisions (``RiskLayer.on_tick``: kill,
    leg stop) awaiting its S-4.2 fill on the first 1-minute bar after trigger + 1 min.
    """

    symbol: str
    side: int
    notional: float
    avg_entry: float
    entry_decision_ts: int
    position_id: str
    pending_exit: str | None = None

    def __post_init__(self) -> None:
        check_side(self.side)
        positive(self.notional, f"{self.symbol}.notional")
        positive(self.avg_entry, f"{self.symbol}.avg_entry")

    @property
    def signed(self) -> float:
        return self.side * self.notional


@dataclass(frozen=True)
class Order:
    """A target order intent (never a fill; Task 14b fills it).

    ``side`` is the leg's side, not the trade direction. ``notional``: open = the target size, close = the
    whole current leg, reduce = the amount removed. ``target``: the leg notional after the order (close: 0).
    ``forced``: a risk / forced exit (taker under every execution model, S-6.2). ``halted``: the instrument is
    halted at the decision time t (PIT, AMB-16 (j)); the engine still applies the S-5.4 check at the fill
    time. A halted exit is deferred (``Pending`` until the first executable bar of the same instance, S-5.4),
    never filled now. Only exits carry ``halted=True`` (opens / reduces of a
    halted instrument are never emitted: no order of any kind while halted, S-7.1). ``trigger_ts``: forced
    exits only, the risk trigger time (``ForcedExit.trigger_ts``) so the engine can apply S-4.2 (forced risk
    exit: first 1-minute bar after trigger + 1 min) versus a decision exit / U6 exit (t + 15 min); None
    otherwise. """

    symbol: str
    side: int
    kind: OrderKind
    notional: float
    target: float
    reason: str
    forced: bool
    halted: bool
    position_id: str | None
    trigger_ts: int | None = None
