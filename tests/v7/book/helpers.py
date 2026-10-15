"""Synthetic fixtures for the book tests (no market data)."""

from __future__ import annotations

import pandas as pd

from luna.v7.book import Leg
from luna.v7.timeutil import MIN, H, to_ns
from luna.v7.universe import GateRow

T0 = to_ns("2024-03-04 08:05")  # a decision time
E = 1.0
E_REF = 10_000.0
BIG_QV = 1e12  # 24 h quote volume large enough that the liquidity cap never binds


def gate(**fail: bool) -> GateRow:
    """All gates pass unless overridden, e.g. ``gate(u4=False)``."""
    flags = {f"u{i}": True for i in range(1, 9)}
    for k, v in fail.items():
        flags[k] = bool(v)
    reasons = tuple(k.upper() for k, v in flags.items() if not v)
    return GateRow(**flags, reasons=reasons, max_known_at=T0 - MIN)


def leg(
    sym: str,
    side: int,
    notional: float = 0.1,
    avg: float = 100.0,
    age_h: float = 72.0,
    pending: str | None = None,
    t: int = T0,
) -> Leg:
    return Leg(
        symbol=sym,
        side=side,
        notional=notional,
        avg_entry=avg,
        entry_decision_ts=int(t - age_h * H),
        position_id=f"{sym}:{side}",
        pending_exit=pending,
    )


def scores(rows: list[tuple]) -> pd.DataFrame:
    """(symbol, side, rank_key, passes_hurdle[, vetoed]) rows, like ``arms.arm_scores`` (``vetoed``: the A1
    entry-only veto, DK-42; default False)."""
    rows = [tuple(r) + (False,) if len(r) == 4 else tuple(r) for r in rows]
    return pd.DataFrame(rows, columns=["symbol", "side", "rank_key", "passes_hurdle", "vetoed"])
