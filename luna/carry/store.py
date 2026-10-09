"""Carry-only tables in the carry account's own SQLite file (``var/luna_carry.db``).

The shared tables (positions, orders, fills, funding, equity, risk_events, state) come from
``luna.store``; these two hold the per-evaluation observability (decision + every alternative).
They live in their own MetaData so the perp/spot databases never get them.
"""

from __future__ import annotations

import json
import math
from typing import Any

import pandas as pd
from sqlalchemy import (
    Boolean,
    Column,
    Engine,
    Float,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    delete,
    update,
)

from luna.store import models as m

metadata = MetaData()

evaluations = Table(
    "carry_evaluations",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("account", String, nullable=False),
    Column("ts", String, nullable=False),
    Column("snapshot_ts", String),
    Column("snapshot_age_s", Float),
    Column("allow_rebalance", Boolean, nullable=False),
    Column("opens_allowed", Boolean, nullable=False),
    Column("equity", Float),
    Column("book_carry_h", Float),  # expected funding USD / hour of the book before the actions
    Column("n_eligible", Integer),
    Column("ideal", Text),  # {"long": [...], "short": [...]}
    Column("holdings", Text),  # [{asset, side, qty, notional, entry_ts, position_id}]
    Column("signals", Text),  # per-asset signal rows (NULL after eval_retention_days)
    Column("n_actions", Integer, nullable=False),
    Column("summary", Text),
)
Index("ix_carry_evaluations_account_ts", evaluations.c.account, evaluations.c.ts)

decisions = Table(
    "carry_decisions",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("account", String, nullable=False),
    Column("eval_id", Integer, nullable=False, index=True),
    Column("ts", String, nullable=False),
    Column("decision", String, nullable=False),  # HOLD | ENTER | EXIT | SWITCH | REJECT
    Column("reason", String),
    Column("side", Integer),
    Column("asset_out", String),
    Column("asset_in", String),
    Column("expected_carry", Float),
    Column("current_carry", Float),
    Column("incremental_carry", Float),
    Column("switch_cost", Float),
    Column("safety_buffer", Float),
    Column("value", Float),
)
Index("ix_carry_decisions_account_ts", decisions.c.account, decisions.c.ts)


def ensure_tables(engine: Engine) -> None:
    metadata.create_all(engine)


def _num(x: Any) -> float | None:
    return None if x is None or not isinstance(x, (int, float)) or not math.isfinite(x) else float(x)


def record_evaluation(engine: Engine, account: str, row: dict[str, Any], records: list[Any]) -> int:
    """One evaluation row and its decision records, in one transaction. Returns the eval id."""
    ts = m.ts_to_str(row["ts"])
    with engine.begin() as c:
        eid = int(
            c.execute(
                evaluations.insert().values(
                    account=account,
                    ts=ts,
                    snapshot_ts=m.ts_to_str(row["snapshot_ts"])
                    if row.get("snapshot_ts") is not None
                    else None,
                    snapshot_age_s=_num(row.get("snapshot_age_s")),
                    allow_rebalance=bool(row["allow_rebalance"]),
                    opens_allowed=bool(row["opens_allowed"]),
                    equity=_num(row.get("equity")),
                    book_carry_h=_num(row.get("book_carry_h")),
                    n_eligible=row.get("n_eligible"),
                    ideal=json.dumps(row.get("ideal")),
                    holdings=json.dumps(row.get("holdings")),
                    signals=json.dumps(row.get("signals")),
                    n_actions=int(row.get("n_actions", 0)),
                    summary=row.get("summary"),
                )
            ).inserted_primary_key[0]
        )
        if records:
            c.execute(
                decisions.insert(),
                [
                    {
                        "account": account,
                        "eval_id": eid,
                        "ts": ts,
                        "decision": r.decision,
                        "reason": r.reason,
                        "side": r.side,
                        "asset_out": r.asset_out,
                        "asset_in": r.asset_in,
                        "expected_carry": _num(r.expected_carry),
                        "current_carry": _num(r.current_carry),
                        "incremental_carry": _num(r.incremental_carry),
                        "switch_cost": _num(r.switch_cost),
                        "safety_buffer": _num(r.safety_buffer),
                        "value": _num(r.value),
                    }
                    for r in records
                ],
            )
    return eid


def prune_old(engine: Engine, account: str, before: pd.Timestamp) -> None:
    """Older than ``before``: drop the per-asset signal JSON of evaluations (rows stay) and the HOLD
    alternatives (ENTER / EXIT / SWITCH / REJECT stay forever)."""
    with engine.begin() as c:
        c.execute(
            delete(decisions).where(
                decisions.c.account == account,
                decisions.c.ts < m.ts_to_str(before),
                decisions.c.decision == "HOLD",
            )
        )
        c.execute(
            update(evaluations)
            .where(
                evaluations.c.account == account,
                evaluations.c.ts < m.ts_to_str(before),
                evaluations.c.signals.is_not(None),
            )
            .values(signals=None)
        )
