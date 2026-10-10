"""Paper engine: persistence, restart idempotence, reconciliation, lock, loop cadence."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy import func, select

from luna.carry import store as cstore
from luna.carry.engine import CarryEngine, EngineLocked
from luna.carry.funding import FundingStore
from luna.carry.market import MarketSnapshot, parse_tickers
from luna.config import CostConfig
from luna.execution.costs import CostModel
from luna.execution.paper import PaperBroker
from luna.store import models as m
from luna.store.db import Db
from tests.carry.helpers import (
    T0,
    carry_config,
    flat_history,
    funding_envelope,
    instrument,
    ticker,
    tickers_envelope,
)

FL = {"A": 5e-5, "B": 4e-5, "C": 1e-5, "D": 0.0, "E": -1e-5, "F": -3e-5}
MIN = pd.Timedelta("1min")


class FakeSource:
    def __init__(self, fl=FL) -> None:
        self.fl = dict(fl)
        self.mids = {a: 100.0 for a in fl}
        self.snap_ts: pd.Timestamp | None = None

    def set(self, ts: pd.Timestamp, **mids: float) -> None:
        self.snap_ts = ts
        self.mids.update(mids)

    def latest_tickers(self, now: pd.Timestamp) -> MarketSnapshot | None:
        ts = self.snap_ts or now
        return parse_tickers(tickers_envelope(ts, [ticker(a, self.mids[a], 2.0) for a in self.fl]))

    def has_new_tickers(self, now, last_ts) -> bool:
        ts = self.snap_ts or now
        return last_ts is None or ts > last_ts

    def _fill(self, store: FundingStore, now: pd.Timestamp) -> int:
        n = 0
        for a, r in self.fl.items():
            n += store.merge_envelope(funding_envelope(a, flat_history(r, now.floor("h"))))
        return n

    def refresh_funding(self, store: FundingStore, now: pd.Timestamp) -> int:
        return self._fill(store, now)

    def bootstrap_funding(self, store: FundingStore, now: pd.Timestamp, keep_hours: int) -> int:
        return self._fill(store, now)

    def latest_books(self, now: pd.Timestamp) -> dict:
        return {}


class FakeBook:
    """PaperBroker feed: a deep two-sided book around the source's mid (1 bp half spread)."""

    def __init__(self, source: FakeSource) -> None:
        self.source = source

    def book(self, asset: str):
        mid = self.source.mids[asset]
        return [(mid * (1 - 1e-4), 1e6)], [(mid * (1 + 1e-4), 1e6)]


class Clock:
    def __init__(self, t: pd.Timestamp) -> None:
        self.t = t

    def __call__(self) -> pd.Timestamp:
        return self.t


def make_engine(
    path: Path, source: FakeSource, clock: Clock, owner: str = "test-1", **cfg_over
) -> CarryEngine:
    cfg = carry_config(universe=sorted(source.fl), **cfg_over)
    db = Db(f"sqlite:///{(path / 'luna_carry.db').as_posix()}")
    costs = CostModel(
        CostConfig(maker_fee=0.0002, taker_fee=0.0005, half_spread_bps={"default": 1.0}, slippage_bps=2.0)
    )
    broker = PaperBroker("perp", costs, FakeBook(source), db, cfg.account, band=cfg.risk.max_price_band)
    return CarryEngine(
        cfg,
        source,
        broker,
        db,
        {a: instrument(a) for a in source.fl},
        0.0005,
        clock=clock,
        sleep=lambda s: None,
        owner=owner,
    )


def count(db: Db, table) -> int:
    with db.engine.connect() as c:
        return int(c.execute(select(func.count()).select_from(table)).scalar() or 0)


def test_first_cycle_opens_the_book_and_persists_everything(tmp_path: Path) -> None:
    src, clock = FakeSource(), Clock(T0)
    e = make_engine(tmp_path, src, clock)
    e.start()
    res = e.cycle(T0)
    assert {p.key: p.direction for p in e.core.portfolio.positions.values()} == {
        "A": -1,
        "B": -1,
        "E": 1,
        "F": 1,
    }
    assert {(p.key, p.direction) for p in e.db.open_positions("carry")} == {
        ("A", -1),
        ("B", -1),
        ("E", 1),
        ("F", 1),
    }
    assert count(e.db, m.fills) == 4 and count(e.db, m.orders) == 4
    assert count(e.db, cstore.evaluations) == 1
    with e.db.engine.connect() as c:
        rows = c.execute(select(cstore.decisions)).mappings().all()
    enters = [r for r in rows if r["decision"] == "ENTER"]
    assert len(enters) == 2 and all(r["expected_carry"] > 0 and r["switch_cost"] > 0 for r in enters)
    assert res is not None and res.equity < 265.0  # fees and spread paid
    e.close()


def test_restart_reloads_without_duplicate_orders_or_churn(tmp_path: Path) -> None:
    src, clock = FakeSource(), Clock(T0)
    e1 = make_engine(tmp_path, src, clock)
    e1.start()
    e1.cycle(T0)
    ids = sorted(e1.core.portfolio.positions)
    e1.close()
    orders, fills = count(e1.db, m.orders), count(e1.db, m.fills)

    clock.t = T0 + 3 * MIN
    src.set(T0 + 3 * MIN)
    e2 = make_engine(tmp_path, src, clock, owner="test-2")
    e2.start()
    assert sorted(e2.core.portfolio.positions) == ids  # same legs, same entry times
    e2.cycle(T0 + 3 * MIN)
    assert count(e2.db, m.orders) == orders and count(e2.db, m.fills) == fills
    assert not e2.core.risk_state.paused
    e2.close()


def test_fill_recorded_before_a_crash_is_booked_exactly_once(tmp_path: Path) -> None:
    src, clock = FakeSource(), Clock(T0)
    e1 = make_engine(tmp_path, src, clock)
    e1.start()

    real_submit = e1.broker.submit
    calls = {"n": 0}

    def crash_after_second(order, ref_mid=None):
        real_submit(order, ref_mid=ref_mid)
        calls["n"] += 1
        if calls["n"] == 2:  # the broker recorded the fill; the process dies before booking it
            raise KeyboardInterrupt

    e1.broker.submit = crash_after_second
    with pytest.raises(KeyboardInterrupt):
        e1.cycle(T0)
    e1.close()

    e2 = make_engine(tmp_path, src, clock, owner="test-2")
    e2.start()
    held = {p.key: p.qty[p.key] for p in e2.core.portfolio.positions.values()}
    assert len(held) == 2  # the two filled legs of the first pair, booked once
    assert sum(abs(q) for q in held.values()) == pytest.approx(2 * 0.6625, rel=1e-3)
    assert not e2.core.risk_state.paused  # holdings == positions == fills
    e2.cycle(T0 + MIN)
    assert len(e2.core.portfolio.positions) == 4  # the rest of the book follows, no duplicates
    assert count(e2.db, m.fills) == 4
    e2.close()


def test_second_engine_cannot_take_the_lock(tmp_path: Path) -> None:
    src, clock = FakeSource(), Clock(T0)
    e1 = make_engine(tmp_path, src, clock)
    e1.start()
    e2 = make_engine(tmp_path, src, clock, owner="other")
    with pytest.raises(EngineLocked):
        e2.start()
    e1.close()


def test_reconciliation_mismatch_pauses_entries(tmp_path: Path) -> None:
    src, clock = FakeSource(), Clock(T0)
    e1 = make_engine(tmp_path, src, clock)
    e1.start()
    e1.cycle(T0)
    e1.close()
    st = e1.db.get_state("carry", "carry_core")
    st["holdings"]["A"] = st["holdings"]["A"] * 2  # tampered account state
    e1.db.save_snapshot("carry", {"carry_core": st}, [])
    e2 = make_engine(tmp_path, src, clock, owner="test-2")
    e2.start()
    assert e2.core.risk_state.paused and e2.db.get_state("carry", "paused") is True
    e2.close()


def test_external_kill_flag_flattens_the_book(tmp_path: Path) -> None:
    src, clock = FakeSource(), Clock(T0)
    e = make_engine(tmp_path, src, clock)
    e.start()
    e.cycle(T0)
    e.db.set_flag("carry", "kill_switch", True)
    src.set(T0 + MIN)
    e.cycle(T0 + MIN)
    assert e.core.portfolio.positions == {}
    assert {p.key for p in e.db.open_positions("carry")} == set()
    e.close()


def test_loop_evaluates_on_new_snapshots_and_at_least_every_max_interval(tmp_path: Path) -> None:
    src, clock = FakeSource(), Clock(T0)
    e = make_engine(tmp_path, src, clock, evaluation__poll_s=5, evaluation__max_interval_s=60)
    e.start()
    src.set(T0)
    seen: list[pd.Timestamp] = []
    real = e.cycle

    def spy(now):
        seen.append(now)
        return real(now)

    e.cycle = spy
    ticks = iter(range(0, 200, 5))

    def sleep(_s: float) -> None:
        try:
            clock.t = T0 + pd.Timedelta(seconds=next(ticks))
        except StopIteration:
            e.stop()
        if clock.t == T0 + pd.Timedelta(seconds=30):
            src.set(T0 + pd.Timedelta(seconds=30))  # a new collector snapshot

    e.sleep = sleep
    e.run_forever()
    secs = [(t - T0).total_seconds() for t in seen]
    assert secs[:3] == [0.0, 30.0, 90.0]  # start, new snapshot at :30, then the 60 s floor
    e.close()


def test_evaluation_rows_carry_the_dashboard_fields(tmp_path: Path) -> None:
    src, clock = FakeSource(), Clock(T0)
    e = make_engine(tmp_path, src, clock)
    e.start()
    e.cycle(T0)
    src.set(T0 + MIN)
    e.cycle(T0 + MIN)
    with e.db.engine.connect() as c:
        ev = c.execute(select(cstore.evaluations).order_by(cstore.evaluations.c.id.desc())).mappings().first()
    import json

    sig = {r["asset"]: r for r in json.loads(ev["signals"])}
    assert sig["A"]["rank"] == 1 and sig["F"]["rank"] == 6 and sig["A"]["fl"] == pytest.approx(5e-5)
    hold = json.loads(ev["holdings"])
    assert {h["asset"] for h in hold} == {"A", "B", "E", "F"}
    assert json.loads(ev["ideal"]) == {"long": ["F", "E"], "short": ["A", "B"]}
    assert ev["summary"] == "HOLD" and ev["book_carry_h"] > 0
    e.close()


def _book(e: CarryEngine) -> dict[str, float]:
    return {p.key: p.qty[p.key] for p in e.core.portfolio.positions.values()}


def _fills_net(e: CarryEngine) -> dict[str, float]:
    with e.db.engine.connect() as c:
        rows = c.execute(select(m.fills.c.symbol, m.fills.c.side, m.fills.c.qty)).all()
    out: dict[str, float] = {}
    for sym, side, q in rows:
        out[sym] = out.get(sym, 0.0) + (q if side == "buy" else -q)
    return {a: q for a, q in out.items() if abs(q) > 1e-12}


def test_failure_mid_execution_is_recovered_from_the_db_without_doubling(tmp_path: Path) -> None:
    """Review M1: an exception after the broker recorded a fill must not leave a stale book."""
    src, clock = FakeSource(), Clock(T0)
    e = make_engine(tmp_path, src, clock)
    e.start()
    real_submit = e.broker.submit
    calls = {"n": 0}

    def flaky(order, ref_mid=None):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("sqlite hiccup")
        real_submit(order, ref_mid=ref_mid)

    e.broker.submit = flaky
    assert e.cycle(T0) is None  # failed, recovered from the Db
    assert _book(e) == pytest.approx(_fills_net(e))  # the recorded fill is booked, exactly once
    e.broker.submit = real_submit
    src.set(T0 + MIN)
    e.cycle(T0 + MIN)
    book = _book(e)
    assert book == pytest.approx(_fills_net(e))
    assert sum(1 for q in book.values() if q > 0) == sum(1 for q in book.values() if q < 0)  # balanced
    assert len(book) == len({p.key for p in e.core.portfolio.positions.values()})  # one leg per asset
    assert not e.core.risk_state.paused
    e.close()


def test_leg_that_never_fills_is_closed_in_the_db(tmp_path: Path) -> None:
    """Review M2: no phantom open rows."""

    class NoAsk(FakeBook):
        def book(self, asset: str):
            bids, asks = super().book(asset)
            return (bids, []) if asset == "F" else (bids, asks)

    src, clock = FakeSource(), Clock(T0)
    e = make_engine(tmp_path, src, clock)
    e.broker.feed = NoAsk(src)
    e.start()
    e.cycle(T0)
    open_rows = {p.id: p.qty[p.key] for p in e.db.open_positions("carry")}
    assert all(q != 0.0 for q in open_rows.values())
    with e.db.engine.connect() as c:
        reasons = dict(
            c.execute(
                select(m.positions.c.id, m.positions.c.exit_reason).where(m.positions.c.status == "closed")
            ).all()
        )
    assert any(pid.startswith("carry:F:L") and r == "unfilled" for pid, r in reasons.items())
    e.close()


def test_resume_clears_a_drawdown_kill_and_resets_the_peak(tmp_path: Path) -> None:
    """Review M3."""
    from luna.carry.engine import request_resume

    src, clock = FakeSource(), Clock(T0)
    e = make_engine(tmp_path, src, clock)
    e.start()
    e.cycle(T0)
    e.core.risk_state.peak_equity = 400.0  # equity ~265: 34% below the peak -> R4 kill
    src.set(T0 + MIN)
    e.cycle(T0 + MIN)
    assert e.core.portfolio.positions == {} and e.db.get_state("carry", "kill_switch") is True
    src.set(T0 + 2 * MIN)
    e.cycle(T0 + 2 * MIN)  # clearing only the flag is not enough: R4 fires again
    assert e.db.get_state("carry", "kill_switch") is True
    request_resume(e.db, "carry")
    src.set(T0 + 3 * MIN)
    e.cycle(T0 + 3 * MIN)
    assert e.db.get_state("carry", "kill_switch") is False
    assert e.core.risk_state.peak_equity < 300 and len(e.core.portfolio.positions) == 4
    e.close()


def test_funding_rows_are_written_with_the_state(tmp_path: Path) -> None:
    src, clock = FakeSource(), Clock(T0)
    e = make_engine(tmp_path, src, clock)
    e.start()
    e.cycle(T0)
    clock.t = T0 + pd.Timedelta(hours=1, minutes=1)
    src.set(clock.t)
    res = e.cycle(clock.t)
    n = sum(len(s.amounts) for s in res.settlements)
    assert n == 4 and count(e.db, m.funding) == 4 and e.core.pending_settlements == []
    with e.db.engine.connect() as c:
        total = c.execute(select(func.sum(m.funding.c.amount))).scalar()
    assert total == pytest.approx(sum(sum(s.amounts.values()) for s in res.settlements))
    assert total > 0  # shorts in positive and longs in negative funding are paid
    e.close()


def test_resume_issued_during_a_cycle_is_not_overwritten(tmp_path: Path) -> None:
    """Review round 2, Minor 1: an operator clearing the kill flag mid-cycle wins."""
    from luna.carry.engine import request_resume

    src, clock = FakeSource(), Clock(T0)
    e = make_engine(tmp_path, src, clock)
    e.start()
    e.cycle(T0)
    e.core.risk_state.peak_equity = 400.0
    src.set(T0 + MIN)
    e.cycle(T0 + MIN)  # R4 kill raised by the core
    assert e.db.get_state("carry", "kill_switch") is True
    real = e.core.run_cycle

    def resume_mid_cycle(*a, **kw):
        res = real(*a, **kw)
        request_resume(e.db, "carry")  # the operator acts while the cycle is running
        return res

    e.core.run_cycle = resume_mid_cycle
    src.set(T0 + 2 * MIN)
    e.cycle(T0 + 2 * MIN)
    assert e.db.get_state("carry", "kill_switch") is False  # not re-raised by the finishing cycle
    src.set(T0 + 3 * MIN)
    e.cycle(T0 + 3 * MIN)  # applies the resume
    assert e.db.get_state("carry", "kill_switch") is False and e.core.risk_state.peak_equity < 300
    e.close()


def test_resume_requested_while_down_does_not_override_a_reconciliation_mismatch(tmp_path: Path) -> None:
    from luna.carry.engine import request_resume

    src, clock = FakeSource(), Clock(T0)
    e1 = make_engine(tmp_path, src, clock)
    e1.start()
    e1.cycle(T0)
    e1.close()
    st = e1.db.get_state("carry", "carry_core")
    st["holdings"]["A"] = st["holdings"]["A"] * 2
    e1.db.save_snapshot("carry", {"carry_core": st}, [])
    request_resume(e1.db, "carry")  # operator resumes while the bot is down
    e2 = make_engine(tmp_path, src, clock, owner="test-2")
    e2.start()
    src.set(T0 + MIN)
    e2.cycle(T0 + MIN)
    assert e2.db.get_state("carry", "paused") is True and e2.core.risk_state.paused
    e2.close()


def test_a_long_blocked_forced_exit_raises_one_risk_event(tmp_path: Path) -> None:
    src, clock = FakeSource(), Clock(T0)
    e = make_engine(tmp_path, src, clock)
    e.start()
    e.cycle(T0)
    e.core.risk_state.kill_switch = True
    e.db.set_flag("carry", "kill_switch", True)
    e.core.instruments = {a: i for a, i in e.core.instruments.items() if a != "A"}  # A cannot be ordered
    for minute in (1, 20, 40, 60):
        clock.t = T0 + minute * MIN
        src.set(clock.t)
        e.cycle(clock.t)
        e.core.instruments = {a: i for a, i in e.core.instruments.items() if a != "A"}
    with e.db.engine.connect() as c:
        n = c.execute(
            select(func.count())
            .select_from(m.risk_events)
            .where(m.risk_events.c.rule == "forced_exit_blocked")
        ).scalar()
    assert n == 1
    e.close()
