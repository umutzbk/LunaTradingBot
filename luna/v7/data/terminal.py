"""Terminal events of ceasing contracts: classification A/B/C and R12 validation (spec S-5.4, M3-1, DEC-43,
EC-7; R12).

A: official scheduled time from a matched immutable notice AND an exact official R12 settlement row.
B: official time known (consistent with the observed cessation) but no exact price.
C: no notice / no time, or a time conflicting with the observed cessation. Consistency rule (M3-1):
``sched <= last traded 15m close <= sched + 15 min``. No fill is ever invented; the engine treats a held
B / C event as INVALID RUN (later task) - this module only classifies and validates R12.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any, Literal

import pandas as pd

from luna.v7.data.instances import Instance
from luna.v7.data.notices import NoticeVersion, confident_match, scheduled_ts
from luna.v7.data.schema import IntegrityBlocker, validate
from luna.v7.timeutil import MIN

R12_EMPTY_OK = True  # EC-7: the table is optional and valid with zero rows
CESSATION_TOL_NS = 15 * MIN
# Only exact official values or exact reproductions of the official formula (S-3 R12).
EXACT_SOURCES = frozenset({"official_historical", "official_formula_exact"})
_REQUIRED = ("settlement_ts", "settlement_price", "source", "source_timestamp", "known_at",
             "retrieved_at", "sha256")


@dataclass(frozen=True)
class TerminalEvent:
    instance: Instance
    cessation_ts: int  # observed last traded 15m close
    klass: Literal["A", "B", "C"]
    settle_ts: int | None
    settle_price: float | None
    source: str | None


def _missing(x: Any) -> bool:
    return x is None or x is pd.NA or x is pd.NaT or (isinstance(x, float) and math.isnan(x))


def reject_approximate(row: Mapping[str, Any]) -> None:
    """EC-7: raise unless the row is an exact official value (no placeholder / approximate / synthetic)."""
    for c in _REQUIRED:
        if c not in row or _missing(row[c]) or (isinstance(row[c], str) and row[c] == ""):
            raise IntegrityBlocker(f"R12: missing {c}; no placeholder rows")
    if row["source"] not in EXACT_SOURCES:
        raise IntegrityBlocker(f"R12: source {row['source']!r} is not an exact official source")
    p = float(row["settlement_price"])
    if not math.isfinite(p) or p <= 0:
        raise IntegrityBlocker("R12: settlement_price must be finite and > 0")


def validate_r12(df: pd.DataFrame) -> None:
    """Schema check plus ``reject_approximate`` on every row; zero rows are valid."""
    validate("R12", df)
    for _, r in df.iterrows():
        reject_approximate(r.to_dict())


def classify(
    event: TerminalEvent,
    notice: NoticeVersion | None,
    settlement_source: Mapping[str, Any] | None,
    symbol: str,
) -> TerminalEvent:
    """Classify a terminal event (``event.cessation_ts`` = observed last traded 15m close)."""
    sched = None
    if notice is not None and notice.kind == "delisting" and confident_match(notice, symbol):
        sched = scheduled_ts(notice, symbol)
    if sched is None or not (sched <= event.cessation_ts <= sched + CESSATION_TOL_NS):
        return replace(event, klass="C", settle_ts=None, settle_price=None, source=None)
    if settlement_source is not None:
        reject_approximate(settlement_source)
        if int(settlement_source["settlement_ts"]) == sched:
            return replace(
                event,
                klass="A",
                settle_ts=sched,
                settle_price=float(settlement_source["settlement_price"]),
                source=str(settlement_source["source"]),
            )
    return replace(event, klass="B", settle_ts=sched, settle_price=None, source=None)
