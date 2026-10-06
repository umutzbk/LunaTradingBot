"""SQLite persistence (SQLAlchemy 2 Core): trading records, risk state, per-account process lock."""

from __future__ import annotations

import dataclasses
import json
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Literal

import pandas as pd
from sqlalchemy import Connection, Engine, create_engine, event, func, select
from sqlalchemy.dialects.sqlite import insert

from luna.portfolio.portfolio import Position
from luna.risk.state import RiskState, state_from_dict, state_to_dict
from luna.store import models as m
from luna.types import Fill, OrderRequest

_OPEN_ORDER_STATUSES = ("NEW", "OPEN", "PARTIAL")
_LOCK_KEY = "lock"


def default_url(var_dir: Path) -> str:
    return f"sqlite:///{Path(var_dir).as_posix()}/luna.db"


def _jsonable(v: Any) -> Any:
    if dataclasses.is_dataclass(v) and not isinstance(v, type):
        return dataclasses.asdict(v)
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    if isinstance(v, dict):
        return {k: _jsonable(x) for k, x in v.items()}
    if isinstance(v, pd.Timestamp):
        return m.ts_to_str(v)
    return v


def _dump_opt(v: Any) -> str | None:
    return None if v is None else m.dumps(_jsonable(v))


class Db:
    def __init__(self, url: str) -> None:
        if not url.startswith("sqlite"):
            raise ValueError(f"only sqlite urls are supported, got {url!r}")
        if url.startswith("sqlite:///") and ":memory:" not in url:
            Path(url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
        self.url = url
        self.engine: Engine = create_engine(url)

        @event.listens_for(self.engine, "connect")
        def _on_connect(dbapi: sqlite3.Connection, _rec: object) -> None:
            dbapi.isolation_level = None  # we emit BEGIN ourselves (see "begin" below)
            cur = dbapi.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA busy_timeout=5000")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()

        @event.listens_for(self.engine, "begin")
        def _on_begin(conn: Connection) -> None:
            conn.exec_driver_sql("BEGIN")

        m.metadata.create_all(self.engine)
        self._migrate()

    def _migrate(self) -> None:
        """Additive column migrations for databases created by an older schema."""
        with self.engine.begin() as c:
            cols = {r[1] for r in c.exec_driver_sql("PRAGMA table_info(candidates)")}
            if "status" not in cols:
                c.exec_driver_sql("ALTER TABLE candidates ADD COLUMN status VARCHAR")
            if "params" not in cols:
                c.exec_driver_sql("ALTER TABLE candidates ADD COLUMN params TEXT")
            out = {r[1]: r for r in c.exec_driver_sql("PRAGMA table_info(candidate_outcomes)")}
            if out.get("outcome_r") is not None and out["outcome_r"][3]:  # NOT NULL: relax (rebuild)
                c.exec_driver_sql("ALTER TABLE candidate_outcomes RENAME TO _candidate_outcomes_old")
                m.candidate_outcomes.create(c)
                c.exec_driver_sql(
                    "INSERT INTO candidate_outcomes (id, candidate_id, outcome_r, exit_reason, labeled_at) "
                    "SELECT id, candidate_id, outcome_r, exit_reason, labeled_at "
                    "FROM _candidate_outcomes_old")
                c.exec_driver_sql("DROP TABLE _candidate_outcomes_old")

    def close(self) -> None:
        self.engine.dispose()

    @contextmanager
    def _tx(self) -> Iterator[Connection]:
        with self.engine.begin() as conn:
            yield conn

    # --- runs -------------------------------------------------------------------------------------

    def start_run(
        self, run_id: str, account: str, mode: str, config: dict[str, Any],
        git_sha: str | None = None, started_at: pd.Timestamp | None = None,
    ) -> None:
        started = started_at if started_at is not None else pd.Timestamp.now(tz="UTC")
        with self._tx() as c:
            c.execute(insert(m.runs).values(
                id=run_id, account=account, mode=mode, config=m.dumps(_jsonable(config)),
                started_at=m.ts_to_str(started), git_sha=git_sha,
            ).on_conflict_do_nothing())

    # --- state / lock -----------------------------------------------------------------------------

    def _set_state(self, c: Connection, account: str, key: str, value: Any) -> None:
        stmt = insert(m.state).values(account=account, key=key, value=json.dumps(value))
        c.execute(stmt.on_conflict_do_update(
            index_elements=["account", "key"], set_={"value": stmt.excluded.value}))

    def get_state(self, account: str, key: str, default: Any = None) -> Any:
        with self.engine.connect() as c:
            v = c.execute(select(m.state.c.value).where(
                m.state.c.account == account, m.state.c.key == key)).scalar()
        return default if v is None else json.loads(v)

    def load_risk_state(self, account: str) -> RiskState:
        with self.engine.connect() as c:
            rows = c.execute(select(m.state.c.key, m.state.c.value).where(
                m.state.c.account == account)).all()
        names = set(state_to_dict(RiskState()))
        return state_from_dict({k: json.loads(v) for k, v in rows if k in names})

    def save_risk_state(self, account: str, s: RiskState) -> None:
        with self._tx() as c:
            for k, v in state_to_dict(s).items():
                self._set_state(c, account, k, v)

    def set_flag(self, account: str, key: Literal["kill_switch", "paused"], value: bool) -> None:
        if key not in ("kill_switch", "paused"):
            raise ValueError(f"unknown flag {key!r}")
        with self._tx() as c:
            self._set_state(c, account, key, bool(value))

    @contextmanager
    def _immediate(self) -> Iterator[sqlite3.Connection]:
        """Raw connection inside BEGIN IMMEDIATE: takes the write lock up front (atomic RMW)."""
        raw = self.engine.raw_connection()
        try:
            dbapi: sqlite3.Connection = raw.driver_connection  # type: ignore[assignment]
            dbapi.execute("BEGIN IMMEDIATE")
            try:
                yield dbapi
            except BaseException:
                dbapi.execute("ROLLBACK")
                raise
            else:
                dbapi.execute("COMMIT")
        finally:
            raw.close()

    def _lock_write(self, dbapi: sqlite3.Connection, account: str, owner: str, now: float) -> None:
        dbapi.execute(
            "INSERT INTO state (account, key, value) VALUES (?, ?, ?) "
            "ON CONFLICT(account, key) DO UPDATE SET value = excluded.value",
            (account, _LOCK_KEY, json.dumps({"owner": owner, "ts": now})),
        )

    def acquire_lock(self, account: str, owner: str, stale_after_s: int = 1800) -> bool:
        now = time.time()
        with self._immediate() as dbapi:
            row = dbapi.execute(
                "SELECT value FROM state WHERE account = ? AND key = ?", (account, _LOCK_KEY)
            ).fetchone()
            if row is not None:
                cur = json.loads(row[0])
                if cur["owner"] != owner and now - cur["ts"] <= stale_after_s:
                    return False
            self._lock_write(dbapi, account, owner, now)
            return True

    def heartbeat(self, account: str, owner: str) -> bool:
        """Refresh the lock heartbeat; False (nothing written) if ``owner`` does not hold it."""
        with self._immediate() as dbapi:
            row = dbapi.execute(
                "SELECT value FROM state WHERE account = ? AND key = ?", (account, _LOCK_KEY)
            ).fetchone()
            if row is None or json.loads(row[0])["owner"] != owner:
                return False
            self._lock_write(dbapi, account, owner, time.time())
            return True

    def release_lock(self, account: str, owner: str) -> None:
        with self._immediate() as dbapi:
            row = dbapi.execute(
                "SELECT value FROM state WHERE account = ? AND key = ?", (account, _LOCK_KEY)
            ).fetchone()
            if row is not None and json.loads(row[0])["owner"] == owner:
                dbapi.execute("DELETE FROM state WHERE account = ? AND key = ?", (account, _LOCK_KEY))

    # --- candidates -------------------------------------------------------------------------------

    def record_candidates(
        self, run_id: str, rows: list[dict[str, Any]], account: str | None = None,
    ) -> list[int]:
        """Insert candidate rows (selected and rejected); returns their ids in order. Row keys: ts,
        strategy, key, direction, score, edge_bps, regime, selected, reject_reason, status,
        features, legs, stop, exit, account."""
        if not rows:
            return []
        out = []
        for r in rows:
            acc = r.get("account", account)
            if acc is None:
                raise ValueError("candidate row needs an account")
            out.append({
                "run_id": run_id, "account": acc, "ts": m.ts_to_str(r["ts"]),
                "strategy": r["strategy"], "key": r["key"], "direction": int(r["direction"]),
                "score": r.get("score"), "edge_bps": r.get("edge_bps"), "regime": r.get("regime"),
                "selected": bool(r.get("selected", False)), "reject_reason": r.get("reject_reason"),
                "status": r.get("status"),
                "features": _dump_opt(r.get("features")), "legs": _dump_opt(r.get("legs")),
                "stop": _dump_opt(r.get("stop")), "exit": _dump_opt(r.get("exit")),
                "params": _dump_opt(r.get("params")),
            })
        with self._tx() as c:
            return [int(c.execute(m.candidates.insert().values(row)).inserted_primary_key[0])
                    for row in out]

    def set_candidate_status(self, candidate_id: int, status: str) -> None:
        with self._tx() as c:
            c.execute(m.candidates.update().where(m.candidates.c.id == candidate_id).values(status=status))

    def record_outcome(
        self, candidate_id: int, outcome_r: float | None, exit_reason: str | None,
        labeled_at: pd.Timestamp | None = None,
    ) -> None:
        when = labeled_at if labeled_at is not None else pd.Timestamp.now(tz="UTC")
        with self._tx() as c:
            c.execute(m.candidate_outcomes.insert().values(
                candidate_id=candidate_id, outcome_r=outcome_r, exit_reason=exit_reason,
                labeled_at=m.ts_to_str(when)))

    # --- positions / orders / fills ---------------------------------------------------------------

    def upsert_position(
        self, account: str, pos: Position, status: Literal["open", "closed"],
        exit_reason: str | None = None, run_id: str | None = None,
        closed_at: pd.Timestamp | None = None,
    ) -> None:
        if status not in ("open", "closed"):
            raise ValueError(f"bad status {status!r}")
        row = m.position_to_row(account, pos, status, exit_reason, run_id, closed_at)
        stmt = insert(m.positions).values(row)
        with self._tx() as c:
            c.execute(stmt.on_conflict_do_update(
                index_elements=["account", "id"],
                set_={k: stmt.excluded[k] for k in row if k not in ("account", "id")}))

    def open_positions(self, account: str) -> list[Position]:
        with self.engine.connect() as c:
            rows = c.execute(select(m.positions).where(
                m.positions.c.account == account, m.positions.c.status == "open"
            ).order_by(m.positions.c.opened_at, m.positions.c.id)).mappings().all()
        return [m.position_from_row(r) for r in rows]

    def record_order(
        self, account: str, order: OrderRequest, status: str = "NEW", run_id: str | None = None,
    ) -> None:
        """Idempotent upsert by (account, client_id)."""
        row = m.order_to_row(account, order, status, run_id)
        stmt = insert(m.orders).values(row)
        with self._tx() as c:
            c.execute(stmt.on_conflict_do_update(
                index_elements=["account", "client_id"],
                set_={k: stmt.excluded[k] for k in row if k not in ("account", "client_id")}))

    def set_order_status(self, account: str, client_id: str, status: str) -> None:
        with self._tx() as c:
            c.execute(m.orders.update().where(
                m.orders.c.account == account, m.orders.c.client_id == client_id
            ).values(status=status))

    def order_status(self, account: str, client_id: str) -> str | None:
        """Current status of an order, or None if it was never recorded."""
        with self.engine.connect() as c:
            return c.execute(select(m.orders.c.status).where(
                m.orders.c.account == account, m.orders.c.client_id == client_id)).scalar()

    def open_orders(self, account: str) -> list[OrderRequest]:
        with self.engine.connect() as c:
            rows = c.execute(select(m.orders).where(
                m.orders.c.account == account, m.orders.c.status.in_(_OPEN_ORDER_STATUSES)
            ).order_by(m.orders.c.ts, m.orders.c.client_id)).mappings().all()
        return [m.order_from_row(r) for r in rows]

    def record_fill(
        self, account: str, fill: Fill, position_id: str | None = None, run_id: str | None = None,
    ) -> None:
        with self._tx() as c:
            c.execute(m.fills.insert().values(
                account=account, run_id=run_id, client_id=fill.client_id, position_id=position_id,
                ts=m.ts_to_str(fill.ts), symbol=fill.asset, side=fill.side, qty=fill.qty,
                price=fill.price, fee=fill.fee, liquidity=fill.liquidity))

    def fills_after(self, account: str, after_id: int) -> list[tuple[int, Fill, str | None]]:
        """Fill rows with id > ``after_id`` in id (= recording) order: (row id, Fill, position_id)."""
        with self.engine.connect() as c:
            rows = c.execute(select(m.fills).where(
                m.fills.c.account == account, m.fills.c.id > after_id
            ).order_by(m.fills.c.id)).mappings().all()
        return [(int(r["id"]), Fill(
            client_id=r["client_id"], ts=m.ts_from_str(r["ts"]), asset=r["symbol"], side=r["side"],
            qty=r["qty"], price=r["price"], fee=r["fee"], liquidity=r["liquidity"],
        ), r["position_id"]) for r in rows]

    def last_fill_id(self, account: str) -> int:
        """Largest fill row id of the account (0 if none)."""
        with self.engine.connect() as c:
            v = c.execute(select(func.max(m.fills.c.id)).where(m.fills.c.account == account)).scalar()
        return int(v or 0)

    def count_orders_since(self, account: str, since: pd.Timestamp) -> int:
        """Number of orders of the account stamped at or after ``since``."""
        with self.engine.connect() as c:
            return int(c.execute(select(func.count()).select_from(m.orders).where(
                m.orders.c.account == account, m.orders.c.ts >= m.ts_to_str(since))).scalar() or 0)

    def save_snapshot(
        self, account: str, state: dict[str, Any],
        positions: list[tuple[Position, Literal["open", "closed"], str | None, pd.Timestamp | None]],
        run_id: str | None = None,
    ) -> None:
        """Atomically write state keys (JSON values) and position upserts in one transaction (the
        live engine's commit point). Flags (kill_switch / paused) are refused here: they are only
        written with ``set_flag`` so a snapshot never overwrites a flag set by someone else."""
        bad = {"kill_switch", "paused", _LOCK_KEY} & set(state)
        if bad:
            raise ValueError(f"save_snapshot may not write {sorted(bad)}")
        with self._tx() as c:
            for k, v in state.items():
                self._set_state(c, account, k, v)
            for pos, status, exit_reason, closed_at in positions:
                row = m.position_to_row(account, pos, status, exit_reason, run_id, closed_at)
                stmt = insert(m.positions).values(row)
                c.execute(stmt.on_conflict_do_update(
                    index_elements=["account", "id"],
                    set_={k: stmt.excluded[k] for k in row if k not in ("account", "id")}))

    # --- time series / events ---------------------------------------------------------------------

    def record_equity(
        self, account: str, ts: pd.Timestamp, equity: float, gross_leverage: float | None = None,
        open_risk: float | None = None, positions: Any = None, run_id: str | None = None,
    ) -> None:
        with self._tx() as c:
            c.execute(m.equity.insert().values(
                account=account, run_id=run_id, ts=m.ts_to_str(ts), equity=equity,
                gross_leverage=gross_leverage, open_risk=open_risk, positions=_dump_opt(positions)))

    def record_risk_event(
        self, account: str, ts: pd.Timestamp, rule: str, detail: str = "", action: str = "",
        run_id: str | None = None,
    ) -> None:
        with self._tx() as c:
            c.execute(m.risk_events.insert().values(
                account=account, run_id=run_id, ts=m.ts_to_str(ts), rule=rule, detail=detail,
                action=action))

    def record_book_snapshot(
        self, ts: pd.Timestamp, symbol: str,
        bids: list[tuple[float, float]], asks: list[tuple[float, float]],
    ) -> None:
        """Order book levels as JSON [[price, qty], ...], best first."""
        with self._tx() as c:
            c.execute(m.book_snapshots.insert().values(
                ts=m.ts_to_str(ts), symbol=symbol, bids=m.dumps([list(x) for x in bids]),
                asks=m.dumps([list(x) for x in asks])))

    def record_funding(
        self, account: str, ts: pd.Timestamp, symbol: str, position: float | None,
        rate: float | None, amount: float, run_id: str | None = None,
    ) -> None:
        with self._tx() as c:
            c.execute(m.funding.insert().values(
                account=account, run_id=run_id, ts=m.ts_to_str(ts), symbol=symbol,
                position=position, rate=rate, amount=amount))
