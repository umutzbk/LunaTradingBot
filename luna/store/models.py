"""SQLAlchemy Core tables (spec section 9) and Position (de)serialization helpers.

Timestamps are stored as UTC ISO-8601 strings (nanosecond precision preserved), structured values
as JSON text. Floats round-trip exactly through Python's ``json`` (shortest repr).
"""

from __future__ import annotations

import json
from typing import Any

import pandas as pd
from sqlalchemy import (
    Boolean,
    Column,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
)

from luna.portfolio.portfolio import Position
from luna.types import ExitSpec, Leg, OrderRequest, StopSpec

metadata = MetaData()

runs = Table(
    "runs", metadata,
    Column("id", String, primary_key=True),
    Column("account", String, nullable=False),
    Column("mode", String, nullable=False),
    Column("config", Text, nullable=False),
    Column("started_at", String, nullable=False),
    Column("git_sha", String),
)

candidates = Table(
    "candidates", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("run_id", String, nullable=False, index=True),
    Column("account", String, nullable=False),
    Column("ts", String, nullable=False),
    Column("strategy", String, nullable=False),
    Column("key", String, nullable=False),
    Column("direction", Integer, nullable=False),
    Column("score", Float),
    Column("edge_bps", Float),
    Column("regime", String),
    Column("selected", Boolean, nullable=False),
    Column("reject_reason", String),
    Column("features", Text),
    Column("legs", Text),
    Column("stop", Text),
    Column("exit", Text),
)
Index("ix_candidates_account_ts", candidates.c.account, candidates.c.ts)

candidate_outcomes = Table(
    "candidate_outcomes", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("candidate_id", Integer, ForeignKey("candidates.id"), nullable=False),
    Column("outcome_r", Float, nullable=False),
    Column("exit_reason", String),
    Column("labeled_at", String, nullable=False),
    UniqueConstraint("candidate_id"),
)

positions = Table(
    "positions", metadata,
    Column("account", String, primary_key=True),
    Column("id", String, primary_key=True),
    Column("run_id", String),
    Column("strategy", String, nullable=False),
    Column("key", String, nullable=False),
    Column("legs", Text, nullable=False),
    Column("status", String, nullable=False),  # open | closed
    Column("opened_at", String, nullable=False),
    Column("closed_at", String),
    Column("entry", Text, nullable=False),  # JSON: qty / entry_price / entry_ref_price / entry_ref
    Column("stop", Text, nullable=False),
    Column("exit_reason", String),
    Column("pnl_net", Float, nullable=False),
    Column("pnl_breakdown", Text, nullable=False),
    Column("data", Text, nullable=False),  # full Position JSON (source of truth for reload)
)

orders = Table(
    "orders", metadata,
    Column("account", String, primary_key=True),
    Column("client_id", String, primary_key=True),
    Column("run_id", String),
    Column("ts", String, nullable=False),
    Column("position_id", String, nullable=False),
    Column("leg_group", String, nullable=False),
    Column("symbol", String, nullable=False),
    Column("side", String, nullable=False),
    Column("qty", Float, nullable=False),
    Column("price", Float, nullable=False),
    Column("tif", String, nullable=False),
    Column("intent", String, nullable=False),
    Column("status", String, nullable=False),
)

fills = Table(
    "fills", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("account", String, nullable=False),
    Column("run_id", String),
    Column("client_id", String, nullable=False, index=True),
    Column("position_id", String),
    Column("ts", String, nullable=False),
    Column("symbol", String, nullable=False),
    Column("side", String, nullable=False),
    Column("qty", Float, nullable=False),
    Column("price", Float, nullable=False),
    Column("fee", Float, nullable=False),
    Column("liquidity", String, nullable=False),
)

funding = Table(
    "funding", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("account", String, nullable=False),
    Column("run_id", String),
    Column("ts", String, nullable=False),
    Column("symbol", String, nullable=False),
    Column("position", Float),
    Column("rate", Float),  # NULL: funding rate unknown
    Column("amount", Float, nullable=False),
)

equity = Table(
    "equity", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("account", String, nullable=False),
    Column("run_id", String),
    Column("ts", String, nullable=False),
    Column("equity", Float, nullable=False),
    Column("gross_leverage", Float),
    Column("open_risk", Float),
    Column("positions", Text),
)

risk_events = Table(
    "risk_events", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("account", String, nullable=False),
    Column("run_id", String),
    Column("ts", String, nullable=False),
    Column("rule", String, nullable=False),
    Column("detail", Text),
    Column("action", String),
)

state = Table(
    "state", metadata,
    Column("account", String, primary_key=True),
    Column("key", String, primary_key=True),
    Column("value", Text, nullable=False),  # JSON
)

book_snapshots = Table(
    "book_snapshots", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("ts", String, nullable=False),
    Column("symbol", String, nullable=False),
    Column("bids", Text, nullable=False),
    Column("asks", Text, nullable=False),
)


# --- helpers ------------------------------------------------------------------------------------


def ts_to_str(ts: pd.Timestamp) -> str:
    t = pd.Timestamp(ts)
    t = t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")
    return t.isoformat()


def ts_from_str(s: str) -> pd.Timestamp:
    return pd.Timestamp(s)


def dumps(obj: Any) -> str:
    return json.dumps(obj, allow_nan=True)


def position_to_row(
    account: str, pos: Position, status: str, exit_reason: str | None = None,
    run_id: str | None = None, closed_at: pd.Timestamp | None = None,
) -> dict[str, Any]:
    data = {
        "id": pos.id, "strategy": pos.strategy, "key": pos.key,
        "legs": [[leg.asset, leg.weight] for leg in pos.legs],
        "direction": pos.direction, "qty": dict(pos.qty), "entry_price": dict(pos.entry_price),
        "entry_ts": ts_to_str(pos.entry_ts), "entry_ref": pos.entry_ref,
        "stop": {"kind": pos.stop.kind, "level": pos.stop.level, "distance_frac": pos.stop.distance_frac},
        "exit": {
            "max_hold_bars": pos.exit.max_hold_bars, "take_profit_z": pos.exit.take_profit_z,
            "trailing_atr_mult": pos.exit.trailing_atr_mult,
        },
        "risk_amount": pos.risk_amount, "regime": pos.regime, "trail_level": pos.trail_level,
        "bars_held": pos.bars_held, "pnl": dict(pos.pnl), "entry_ref_price": dict(pos.entry_ref_price),
    }
    return {
        "account": account, "id": pos.id, "run_id": run_id, "strategy": pos.strategy, "key": pos.key,
        "legs": dumps(data["legs"]), "status": status, "opened_at": data["entry_ts"],
        "closed_at": ts_to_str(closed_at) if closed_at is not None else None,
        "entry": dumps({
            "qty": data["qty"], "entry_price": data["entry_price"],
            "entry_ref_price": data["entry_ref_price"], "entry_ref": pos.entry_ref,
        }),
        "stop": dumps(data["stop"]), "exit_reason": exit_reason, "pnl_net": pos.net_pnl(),
        "pnl_breakdown": dumps(data["pnl"]), "data": dumps(data),
    }


def position_from_row(row: Any) -> Position:
    d = json.loads(row["data"])
    return Position(
        id=d["id"], strategy=d["strategy"], key=d["key"],
        legs=tuple(Leg(a, w) for a, w in d["legs"]), direction=d["direction"], qty=d["qty"],
        entry_price=d["entry_price"], entry_ts=ts_from_str(d["entry_ts"]), entry_ref=d["entry_ref"],
        stop=StopSpec(**d["stop"]), exit=ExitSpec(**d["exit"]), risk_amount=d["risk_amount"],
        regime=d["regime"], trail_level=d["trail_level"], bars_held=d["bars_held"], pnl=d["pnl"],
        entry_ref_price=d["entry_ref_price"],
    )


def order_to_row(account: str, o: OrderRequest, status: str, run_id: str | None) -> dict[str, Any]:
    return {
        "account": account, "client_id": o.client_id, "run_id": run_id, "ts": ts_to_str(o.ts),
        "position_id": o.position_id, "leg_group": o.leg_group, "symbol": o.asset, "side": o.side,
        "qty": o.qty, "price": o.limit_price, "tif": o.tif, "intent": o.intent, "status": status,
    }


def order_from_row(row: Any) -> OrderRequest:
    return OrderRequest(
        client_id=row["client_id"], ts=ts_from_str(row["ts"]), asset=row["symbol"], side=row["side"],
        qty=row["qty"], limit_price=row["price"], tif=row["tif"], intent=row["intent"],
        position_id=row["position_id"], leg_group=row["leg_group"],
    )
