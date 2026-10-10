"""The long-running paper engine of the adaptive carry bot (Docker service ``bot-carry``).

Loop (no cron, no OS scheduler): every ``evaluation.poll_s`` seconds look for a new collector
``fut_tickers`` snapshot (stat only); evaluate when one arrived or ``max_interval_s`` passed since
the last evaluation. An evaluation is one ``CarryCore.run_cycle`` (monitoring always; economic
changes only when the trigger allows) plus persistence. The account's Db lock is refreshed every
60 s; a lost lock stops the engine (exit code 1).

Paper only: orders go to ``PaperBroker`` (IOC walk of the live public Kraken book, taker fee); the
risk profile has ``live_enabled: false`` and the engine runs in mode "paper".

Restart safety (``start``):

- the core state (portfolio, positions incl. legs registered for an order in flight, funding
  ledger, cooldowns, risk state) is committed in one transaction BEFORE any order is handed to the
  broker (``CarryCore.on_submit``) and again after the cycle;
- on start: take the lock, reload the state, book every fill the Db recorded after the last booked
  fill id (each exactly once), cancel orders still NEW/OPEN (IOC never rests), drop legs that never
  filled, reconcile holdings = sum of positions = net of all recorded fills (R18; a mismatch pauses
  entries and is logged). Held legs keep their entry time, so a restart never closes and reopens
  them; client ids are unique (position id + sequence) and the broker ignores a known id.
"""

from __future__ import annotations

import json
import logging
import math
import os
import socket
import time
import uuid
from collections.abc import Callable
from dataclasses import replace
from typing import Any

import pandas as pd
from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert

from luna.carry import store as cstore
from luna.carry.config import CarryConfig
from luna.carry.core import CarryCore, CycleResult, Submission
from luna.carry.funding import FundingStore
from luna.carry.market import MarketSnapshot
from luna.execution.paper import PaperBroker
from luna.store import models as m
from luna.store.db import Db
from luna.types import Fill, Instrument

log = logging.getLogger("luna.carry")

STATE_KEY = "carry_core"
RESUME_KEY = "carry_resume"  # set by `luna carry-resume`, consumed by the engine
EQUITY_EVERY = pd.Timedelta(minutes=5)  # equity rows: at most this often (and after any fill)
LOCK_STALE_S = 300
HEARTBEAT_S = 60
FUNDING_REFRESH = pd.Timedelta(minutes=5)
BOOK_REFRESH = pd.Timedelta(minutes=5)
MAX_CONSECUTIVE_ERRORS = 10
ERROR_EXIT_CODE = 3
_HOLDINGS_TOL = 1e-6


class EngineLocked(RuntimeError):
    """Another process holds the carry account's Db lock."""


class PaperExecutor:
    """Hands orders to the PaperBroker and returns the fills the Db recorded for them."""

    def __init__(self, engine: CarryEngine) -> None:
        self.e = engine

    def execute(self, batch: list[Submission], snap: MarketSnapshot) -> list[tuple[Fill, str]]:
        for o, ref in batch:
            if not self.e.heartbeat(force=True):
                raise RuntimeError("carry lock lost before an order")
            self.e.broker.submit(o, ref_mid=ref)
        self.e.broker.poll(self.e.clock())  # IOC fills are already in the Db; drain the broker
        out = []
        for fid, fill, pid in self.e.db.fills_after(self.e.account, self.e.last_fill_id):
            self.e.pending_fill_id = fid  # becomes last_fill_id only once the core booked them
            out.append((fill, pid or ""))
        return out


class CarryEngine:
    def __init__(
        self,
        cfg: CarryConfig,
        source: Any,  # CollectorSource-like (latest_tickers, latest_books, has_new_tickers, *_funding)
        broker: PaperBroker,
        db: Db,
        instruments: dict[str, Instrument],
        fee_rate: float,
        clock: Callable[[], pd.Timestamp] = lambda: pd.Timestamp.now(tz="UTC"),
        sleep: Callable[[float], None] = time.sleep,
        owner: str | None = None,
    ) -> None:
        self.cfg = cfg
        self.account = cfg.account
        self.source = source
        self.broker = broker
        self.db = db
        self.instruments = instruments
        self.fee_rate = fee_rate
        self.clock = clock
        self.sleep = sleep
        self.owner = owner or f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        self.core = CarryCore(cfg, instruments, fee_rate, mode="paper")
        self.funding = FundingStore()
        self.last_fill_id = 0
        self.pending_fill_id = 0
        self.inflight: dict[str, float] = {}  # client id -> reference mid
        self.last_equity_ts: pd.Timestamp | None = None
        self.last_eval: pd.Timestamp | None = None
        self.last_snapshot_ts: pd.Timestamp | None = None
        self.last_funding_refresh: pd.Timestamp | None = None
        self.books: dict = {}
        self.last_book_refresh: pd.Timestamp | None = None
        self.last_prune_day: str | None = None
        self._last_heartbeat = 0.0
        self._stopped = False
        self._started = False
        self.errors = 0
        self.exit_code = 0
        self.exit_reason = ""
        cstore.ensure_tables(db.engine)

    # --- lifecycle -------------------------------------------------------------------------------

    def start(self) -> None:
        if not self.db.acquire_lock(self.account, self.owner, stale_after_s=LOCK_STALE_S):
            raise EngineLocked(f"account {self.account!r} is locked by another process")
        self._last_heartbeat = time.monotonic()
        self._started = True
        now = self.clock()
        self.db.start_run(self.owner, self.account, "paper", self.cfg.model_dump(), started_at=now)
        booked = self._restore(now)
        self.source.bootstrap_funding(self.funding, now, keep_hours=self.cfg.signal_hours + 24 * 7)
        self.last_funding_refresh = now
        log.info(
            "carry engine started: %d legs, equity %.2f, %d fills booked on start",
            len(self.core.portfolio.positions),
            self.core.portfolio.equity(),
            booked,
        )

    def _restore(self, now: pd.Timestamp) -> int:
        """Rebuild the core from the Db (start, and after a failed evaluation): reload the state,
        book the fills recorded after the last booked one (exactly once), cancel orders still open
        (IOC never rests), close legs that never filled, reconcile, commit. Returns fills booked."""
        st = self.db.get_state(self.account, STATE_KEY)
        if st is not None:
            self.core = CarryCore.from_state(st, self.cfg, self.instruments, self.fee_rate, mode="paper")
            self.last_fill_id = int(st.get("last_fill_id", 0))
            self.inflight = {k: float(v) for k, v in st.get("inflight", {}).items()}
        else:
            self.core = CarryCore(self.cfg, self.instruments, self.fee_rate, mode="paper")
            self.last_fill_id, self.inflight = 0, {}
        self.pending_fill_id = self.last_fill_id
        self.core.on_submit = self._before_submit
        self.core.on_booked = self._after_booked
        self._load_flags()
        booked = self._book_unbooked_fills()
        for o in self.db.open_orders(self.account):
            self.db.set_order_status(self.account, o.client_id, "CANCELED")
        for pid in list(self.core.portfolio.positions):
            self.core._drop_empty(pid)
        self.core.exit_reason.clear()
        self.inflight.clear()
        self._reconcile(now)
        self._save(now, closed=[])
        return booked

    def stop(self) -> None:
        self._stopped = True

    def close(self) -> None:
        if self._started:
            self.db.release_lock(self.account, self.owner)
            self._started = False

    def heartbeat(self, force: bool = False) -> bool:
        if not force and time.monotonic() - self._last_heartbeat < HEARTBEAT_S:
            return True
        if not self.db.heartbeat(self.account, self.owner):
            log.error("carry lock lost; stopping")
            self.db.record_risk_event(self.account, self.clock(), "lock_lost", self.owner, "stop")
            self.exit_code, self.exit_reason = 1, "lock_lost"
            self._stopped = True
            return False
        self._last_heartbeat = time.monotonic()
        return True

    def run_forever(self) -> None:
        ev = self.cfg.evaluation
        while not self._stopped:
            if not self.heartbeat():
                break
            now = self.clock()
            due = self.last_eval is None or (now - self.last_eval).total_seconds() >= ev.max_interval_s
            try:
                new = self.source.has_new_tickers(now, self.last_snapshot_ts)
            except Exception as e:  # noqa: BLE001 - a broken source must not stop the loop
                log.warning("collector source check failed: %s", e)
                new = False
            if new or due:
                self.cycle(now)
            if self._stopped:
                break
            self.sleep(ev.poll_s)

    # --- one evaluation --------------------------------------------------------------------------

    def cycle(self, now: pd.Timestamp) -> CycleResult | None:
        try:
            res = self._cycle(now)
        except Exception as e:  # noqa: BLE001 - logged, counted; repeated failures restart the process
            self.errors += 1
            log.exception("carry cycle failed (%d in a row)", self.errors)
            try:
                self.db.record_risk_event(self.account, now, "cycle_error", f"{type(e).__name__}: {e}"[:500])
            except Exception:  # noqa: BLE001
                pass
            if self.errors >= MAX_CONSECUTIVE_ERRORS:
                self.exit_code, self.exit_reason = ERROR_EXIT_CODE, "cycle_errors"
                self._stopped = True
            self.last_eval = now
            if self._started and not self._stopped:
                try:  # the in-memory book may be half-updated: rebuild it from the Db
                    self._restore(now)
                except Exception:  # noqa: BLE001 - cannot trust the book: let Docker restart us
                    log.exception("carry recovery after a failed cycle failed; exiting")
                    self.exit_code, self.exit_reason = ERROR_EXIT_CODE, "recovery_failed"
                    self._stopped = True
            return None
        self.errors = 0
        return res

    def _cycle(self, now: pd.Timestamp) -> CycleResult:
        self._load_flags()
        self._apply_resume(now)
        if (
            self.last_funding_refresh is None
            or now - self.last_funding_refresh >= FUNDING_REFRESH
            or now.floor("h") != self.last_funding_refresh.floor("h")
        ):
            self.source.refresh_funding(self.funding, now)
            self.funding.prune(now - pd.Timedelta(hours=self.cfg.signal_hours + 24 * 7))
            self.last_funding_refresh = now
        if self.last_book_refresh is None or now - self.last_book_refresh >= BOOK_REFRESH:
            fresh = self.source.latest_books(now)
            if fresh:
                self.books.update(fresh)
            self.last_book_refresh = now
        snap = self.source.latest_tickers(now)
        if snap is not None and self.books:
            snap = replace(snap, books=dict(self.books))
        allow = self.core.trigger.due(now, self.core.last_rebalance)
        res = self.core.run_cycle(now, snap, self.funding, PaperExecutor(self), allow)
        self.last_eval = now
        if snap is not None:
            self.last_snapshot_ts = snap.ts
        self._persist(now, res, snap)
        return res

    # --- persistence -----------------------------------------------------------------------------

    def _before_submit(self, batch: list[Submission]) -> None:
        """Commit the state (legs registered for these orders included) before the broker sees them."""
        for o, ref in batch:
            self.inflight[o.client_id] = ref
        self._save(self.clock(), closed=[])

    def _after_booked(self) -> None:
        self.last_fill_id = max(self.last_fill_id, self.pending_fill_id)

    def _state(self) -> dict[str, Any]:
        st = self.core.to_state()
        st["last_fill_id"] = self.last_fill_id
        st["inflight"] = dict(self.inflight)
        return st

    def _save(self, now: pd.Timestamp, closed: list[tuple[Any, str]]) -> None:
        rows: list[tuple[Any, str, str | None, pd.Timestamp | None]] = [
            (p, "open", None, None) for p in self.core.portfolio.positions.values()
        ]
        rows += [(p, "closed", reason, t) for p, reason, t in closed]
        rows += [(p, "closed", "unfilled", now) for p in self.core.dropped]
        settled = list(self.core.pending_settlements)
        with self.db._tx() as c:  # state, legs and funding rows commit together (or not at all)
            self.db._set_state(c, self.account, STATE_KEY, self._state())
            for pos, status, reason, closed_at in rows:
                row = m.position_to_row(self.account, pos, status, reason, self.owner, closed_at)
                stmt = insert(m.positions).values(row)
                c.execute(
                    stmt.on_conflict_do_update(
                        index_elements=["account", "id"],
                        set_={k: stmt.excluded[k] for k in row if k not in ("account", "id")},
                    )
                )
            for s in settled:
                for pid, amt in s.amounts.items():
                    pos = (
                        self.core.portfolio.positions.get(pid) or self.core.recent_closed.get(pid, (None,))[0]
                    )
                    c.execute(
                        m.funding.insert().values(
                            account=self.account,
                            run_id=self.owner,
                            ts=m.ts_to_str(s.period_start),
                            symbol=s.asset,
                            position=pos.qty.get(s.asset, 0.0) if pos is not None else None,
                            rate=s.funding_rel if s.known else None,
                            amount=amt,
                        )
                    )
        del self.core.pending_settlements[: len(settled)]
        self.core.dropped.clear()

    def _persist(self, now: pd.Timestamp, res: CycleResult, snap: MarketSnapshot | None) -> None:
        self.inflight.clear()
        self._sync_flags(now)
        rc = self.core.recent_closed
        closed = [(p, r, rc[p.id][1] if p.id in rc else now) for p, r in res.closed]
        done = {c[0].id for c in closed}
        closed += [
            (p, rc[p.id][2], rc[p.id][1]) for p in res.touched_closed if p.id not in done and p.id in rc
        ]
        self._save(now, closed)
        for r in res.records:
            if r.decision == "REJECT" and r.reason and r.reason[:1] == "R" and "_" in r.reason[:4]:
                self.db.record_risk_event(
                    self.account, now, r.reason.split(":")[0], r.reason[:500], "veto", run_id=self.owner
                )
        pf = self.core.portfolio
        holdings = [
            {
                "asset": p.key,
                "side": p.direction,
                "qty": p.qty.get(p.key, 0.0),
                "notional": abs(p.qty.get(p.key, 0.0)) * pf.marks.get(p.key, p.entry_ref),
                "entry_ts": m.ts_to_str(p.entry_ts),
                "position_id": p.id,
            }
            for p in pf.positions.values()
            if p.qty.get(p.key, 0.0) != 0.0
        ]
        d = res.decision
        n_actions = sum(1 for g in res.groups if g.status == "planned")
        cstore.record_evaluation(
            self.db.engine,
            self.account,
            {
                "ts": now,
                "snapshot_ts": res.snapshot_ts,
                "snapshot_age_s": snap.age_s(now) if snap is not None else None,
                "allow_rebalance": res.allow_rebalance,
                "opens_allowed": res.opens_allowed,
                "equity": res.equity,
                "book_carry_h": d.book_carry_h if d is not None else None,
                "n_eligible": sum(1 for s in res.signals.values() if s.eligible),
                "ideal": {"long": d.ideal_long, "short": d.ideal_short} if d is not None else None,
                "holdings": holdings,
                "signals": [s.to_row() for s in res.signals.values()],
                "n_actions": n_actions,
                "summary": "; ".join(
                    f"{g.action.decision} {g.action.reason} {g.action.asset_out or ''}->"
                    f"{g.action.asset_in or ''}"
                    for g in res.groups
                    if g.status == "planned"
                )
                or "HOLD",
            },
            res.records,
        )
        if (
            res.fills
            or res.settlements
            or self.last_equity_ts is None
            or now - self.last_equity_ts >= EQUITY_EVERY
        ):
            self.db.record_equity(
                self.account,
                now,
                res.equity,
                pf.gross_leverage(),
                pf.open_risk(),
                holdings,
                run_id=self.owner,
            )
            self.last_equity_ts = now
        day = now.strftime("%Y-%m-%d")
        if day != self.last_prune_day:
            cstore.prune_old(
                self.db.engine, self.account, now - pd.Timedelta(days=self.cfg.eval_retention_days)
            )
            self.last_prune_day = day

    # --- restart helpers -------------------------------------------------------------------------

    def _sync_flags(self, now: pd.Timestamp) -> None:
        """Persist a kill switch / pause the core raised itself (R4 drawdown, R18) as Db flags, which
        are the source of truth reloaded every cycle (also settable from outside)."""
        rs = self.core.risk_state
        loaded = getattr(self, "_loaded_flags", {})
        for key, on in (("kill_switch", rs.kill_switch), ("paused", rs.paused)):
            # only a flag this cycle raised (loaded off, now on): never re-write one an operator
            # cleared while the cycle ran
            if on and not loaded.get(key, False) and not self.db.get_state(self.account, key, False):
                self.db.set_flag(self.account, key, True)
                self.db.record_risk_event(self.account, now, key, "raised by the carry core", key)

    def _apply_resume(self, now: pd.Timestamp) -> None:
        """`luna carry-resume`: flags were cleared by the command; reset the drawdown peak (and the
        day's loss reference) to the current equity so R4/R3 do not fire again at once."""
        if not self.db.get_state(self.account, RESUME_KEY, False):
            return
        eq = self.core.portfolio.equity()
        rs = self.core.risk_state
        rs.peak_equity, rs.day_start_equity, rs.entries_blocked_day = eq, eq, None
        rs.kill_switch = rs.paused = False
        self.core.reconciliation_ok = True
        with self.db._tx() as c:  # reset state, flags and request in one commit
            self.db._set_state(c, self.account, STATE_KEY, self._state())
            self.db._set_state(c, self.account, "kill_switch", False)
            self.db._set_state(c, self.account, "paused", False)
            self.db._set_state(c, self.account, RESUME_KEY, False)
        self._loaded_flags = {"kill_switch": False, "paused": False}
        self.db.record_risk_event(self.account, now, "resume", f"peak reset to {eq:.2f}", "resume")
        log.warning("carry resumed by operator: peak and day start reset to %.2f", eq)

    def _load_flags(self) -> None:
        rs = self.core.risk_state
        rs.kill_switch = bool(self.db.get_state(self.account, "kill_switch", False))
        rs.paused = bool(self.db.get_state(self.account, "paused", False))
        self._loaded_flags = {"kill_switch": rs.kill_switch, "paused": rs.paused}

    def _book_unbooked_fills(self) -> int:
        n = 0
        for fid, fill, pid in self.db.fills_after(self.account, self.last_fill_id):
            if pid in self.core.portfolio.positions:
                self.core.book_fill(fill, pid, self.inflight.get(fill.client_id, fill.price))
                n += 1
            else:
                log.error("fill %s (%s) belongs to no known leg %s", fid, fill.client_id, pid)
                self.db.record_risk_event(
                    self.account,
                    fill.ts,
                    "R18_reconciliation",
                    f"orphan fill {fill.client_id} for {pid}",
                    "pause",
                )
                self.core.reconciliation_ok = False
            self.last_fill_id = fid
        return n

    def _fills_net(self) -> dict[str, float]:
        with self.db.engine.connect() as c:
            rows = c.execute(
                select(m.fills.c.symbol, m.fills.c.side, m.fills.c.qty).where(
                    m.fills.c.account == self.account
                )
            ).all()
        net: dict[str, float] = {}
        for sym, side, qty in rows:
            net[sym] = net.get(sym, 0.0) + (qty if side == "buy" else -qty)
        return {a: q for a, q in net.items() if abs(q) > _HOLDINGS_TOL}

    def _reconcile(self, now: pd.Timestamp) -> None:
        pf = self.core.portfolio
        by_pos: dict[str, float] = {}
        for p in pf.positions.values():
            q = p.qty.get(p.key, 0.0)
            if q:
                by_pos[p.key] = by_pos.get(p.key, 0.0) + q
        fills = self._fills_net()
        assets = set(pf.holdings) | set(by_pos) | set(fills)
        bad = {
            a: (pf.holdings.get(a, 0.0), by_pos.get(a, 0.0), fills.get(a, 0.0))
            for a in assets
            if not (
                math.isclose(pf.holdings.get(a, 0.0), by_pos.get(a, 0.0), abs_tol=_HOLDINGS_TOL)
                and math.isclose(by_pos.get(a, 0.0), fills.get(a, 0.0), abs_tol=_HOLDINGS_TOL)
            )
        }
        if bad or not self.core.reconciliation_ok:
            self.core.reconciliation_ok = False
            self.db.set_flag(self.account, "paused", True)
            self.core.risk_state.paused = True
            detail = json.dumps({a: list(v) for a, v in bad.items()})[:500]
            self.db.record_risk_event(self.account, now, "R18_reconciliation", detail, "pause")
            log.error("carry reconciliation mismatch (holdings, positions, fills): %s", detail)


def request_resume(db: Db, account: str) -> None:
    """Clear the kill switch / pause flags and ask the engine (running or next start) to reset its
    drawdown peak to the current equity (the carry equivalent of the live engine's /resume)."""
    db.set_flag(account, "kill_switch", False)
    db.set_flag(account, "paused", False)
    db.save_snapshot(account, {RESUME_KEY: True}, [])
