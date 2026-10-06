from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from datetime import date

import pandas as pd
import pytest
from sqlalchemy import text

from luna.portfolio.portfolio import Position
from luna.risk.state import RiskState
from luna.store.db import Db
from luna.types import ExitSpec, Fill, Leg, OrderRequest, StopSpec

TS = pd.Timestamp("2026-03-01 12:00:00.123456789", tz="UTC")


@pytest.fixture
def db(tmp_path):
    d = Db(f"sqlite:///{(tmp_path / 'luna.db').as_posix()}")
    yield d
    d.close()


def _pos(pid="p1") -> Position:
    return Position(
        id=pid, strategy="pairs", key="A/B", legs=(Leg("A", 1.0), Leg("B", -0.7312345678901234)),
        direction=-1, qty={"A": -1.1234567890123457, "B": 0.1 + 0.2},
        entry_price={"A": 100.1, "B": 1e-7 / 3}, entry_ts=TS, entry_ref=0.30000000000000004,
        stop=StopSpec("zscore", 3.5, 0.0123456789), exit=ExitSpec(48, 0.5, None),
        risk_amount=12.345678901234567, regime="range", trail_level=99.99, bars_held=7,
        pnl={"gross": 1.5, "fees": 0.25, "spread_slip": 0.1 + 0.2, "funding": -0.3,
             "funding_unknown_bars": 2.0},
        entry_ref_price={"A": 100.0, "B": 2.5e-8},
    )


def test_wal_enabled(db):
    with db.engine.connect() as c:
        assert c.execute(text("PRAGMA journal_mode")).scalar() == "wal"
        assert c.execute(text("PRAGMA busy_timeout")).scalar() == 5000
        assert c.execute(text("PRAGMA foreign_keys")).scalar() == 1


def test_lock_exclusive_and_stale_takeover(db):
    assert db.acquire_lock("perp", "A")
    assert not db.acquire_lock("perp", "B")
    assert db.acquire_lock("perp", "A")  # re-entrant for same owner
    assert db.acquire_lock("spot", "B")  # per account
    assert db.heartbeat("perp", "A")
    assert not db.heartbeat("perp", "B")
    assert not db.acquire_lock("perp", "B", stale_after_s=1800)
    assert db.acquire_lock("perp", "B", stale_after_s=-1)  # stale -> takeover
    assert not db.heartbeat("perp", "A")
    db.release_lock("perp", "A")  # not the owner: no-op
    assert not db.acquire_lock("perp", "A")
    db.release_lock("perp", "B")
    assert db.acquire_lock("perp", "A")


def test_lock_exclusive_across_processes(tmp_path):
    url = f"sqlite:///{(tmp_path / 'x.db').as_posix()}"
    db = Db(url)
    script = textwrap.dedent(f"""
        import sys
        from luna.store.db import Db
        d = Db({url!r})
        sys.stdout.write("1" if d.acquire_lock("perp", sys.argv[1]) else "0")
    """)
    procs = [subprocess.Popen([sys.executable, "-c", script, f"o{i}"], stdout=subprocess.PIPE, text=True)
             for i in range(4)]
    results = [p.communicate()[0] for p in procs]
    db.close()
    assert sorted(results) == ["0", "0", "0", "1"]


def test_risk_state_roundtrip(db):
    assert db.load_risk_state("perp") == RiskState()
    s = RiskState(True, True, date(2026, 3, 1), 1000.123456789, 1100.5, 7, date(2026, 3, 1))
    db.save_risk_state("perp", s)
    assert db.load_risk_state("perp") == s
    assert db.load_risk_state("spot") == RiskState()
    db.set_flag("perp", "kill_switch", False)
    db.set_flag("perp", "paused", False)
    loaded = db.load_risk_state("perp")
    assert not loaded.kill_switch and not loaded.paused and loaded.peak_equity == 1100.5
    db.save_risk_state("perp", RiskState())
    assert db.load_risk_state("perp") == RiskState()


def test_position_roundtrip_preserves_stop_and_legs(db):
    p = _pos()
    db.upsert_position("perp", p, "open")
    db.upsert_position("spot", _pos("other"), "open")
    got = db.open_positions("perp")
    assert got == [p]
    assert got[0].entry_ts.tzinfo is not None and got[0].entry_ts == TS
    p.bars_held = 8
    p.qty["A"] = -0.5
    db.upsert_position("perp", p, "open")
    assert db.open_positions("perp") == [p]
    db.upsert_position("perp", p, "closed", exit_reason="stop", closed_at=TS)
    assert db.open_positions("perp") == []
    bare = _pos("bare")
    bare.trail_level = None
    bare.exit = ExitSpec(10)
    db.upsert_position("perp", bare, "open")
    assert db.open_positions("perp") == [bare]


def test_candidates_store_rejected_with_reason(db):
    stop, ex = StopSpec("price", 95.0, 0.05), ExitSpec(24, None, 3.0)
    db.record_candidates("r1", [
        {"ts": TS, "strategy": "trend", "key": "BTC", "direction": 1, "score": 1.5, "edge_bps": 20.0,
         "regime": "trend", "selected": True, "reject_reason": None, "features": {"z": 1.2},
         "legs": (Leg("BTC", 1.0),), "stop": stop, "exit": ex},
        {"ts": TS, "strategy": "trend", "key": "ETH", "direction": -1, "score": 0.5, "edge_bps": 3.0,
         "regime": "range", "selected": False, "reject_reason": "ranker", "features": {"z": -0.4},
         "legs": (Leg("ETH", -1.0),), "stop": stop, "exit": ex},
    ], account="perp")
    with db.engine.connect() as c:
        rows = c.execute(text("SELECT * FROM candidates ORDER BY id")).mappings().all()
    assert [r["id"] for r in rows] == [1, 2]
    assert [r["selected"] for r in rows] == [1, 0]
    assert rows[1]["reject_reason"] == "ranker" and rows[1]["account"] == "perp"
    assert json.loads(rows[1]["features"]) == {"z": -0.4}
    assert json.loads(rows[1]["stop"]) == {"kind": "price", "level": 95.0, "distance_frac": 0.05}
    assert json.loads(rows[1]["legs"]) == [{"asset": "ETH", "weight": -1.0}]
    db.record_outcome(2, -1.0, "stop")
    with pytest.raises(Exception, match="UNIQUE"):
        db.record_outcome(2, 0.5, "tp")
    with pytest.raises(Exception, match="FOREIGN"):
        db.record_outcome(99, 0.5, "tp")


def test_orders_idempotent_and_open_filter(db):
    o = OrderRequest("c1", TS, "BTC", "buy", 0.5, 100.5, "POST_ONLY", "open", "p1", "g1")
    db.record_order("perp", o)
    db.record_order("perp", o)
    o2 = OrderRequest("c2", TS, "ETH", "sell", 2.0, 10.25, "IOC", "close", "p1", "g1")
    db.record_order("perp", o2, "PARTIAL")
    o3 = OrderRequest("c3", TS, "ETH", "sell", 1.0, 1.0, "IOC", "close", "p", "g")
    db.record_order("perp", o3, "FILLED")
    db.record_order("spot", o)
    assert db.open_orders("perp") == [o, o2]
    db.record_order("perp", o, "CANCELLED")
    assert db.open_orders("perp") == [o2]
    db.set_order_status("perp", "c2", "FILLED")
    assert db.open_orders("perp") == []
    assert db.open_orders("spot") == [o]


def test_records_fill_equity_event_funding_run(db):
    db.start_run("r1", "perp", "paper", {"a": 1}, git_sha="abc")
    db.start_run("r1", "perp", "paper", {"a": 1}, git_sha="abc")
    db.record_fill("perp", Fill("c1", TS, "BTC", "buy", 0.5, 100.0, 0.1, "maker"), "p1", "r1")
    db.record_equity("perp", TS, 1000.0, 1.5, 20.0, {"p1": 1}, "r1")
    db.record_risk_event("perp", TS, "R3", "dd", "block", "r1")
    db.record_funding("perp", TS, "BTC", 0.5, None, 0.0, "r1")
    with db.engine.connect() as c:
        counts = {t: c.execute(text(f"SELECT COUNT(*) FROM {t}")).scalar()
                  for t in ("runs", "fills", "equity", "risk_events", "funding")}
    assert counts == {"runs": 1, "fills": 1, "equity": 1, "risk_events": 1, "funding": 1}


def test_order_status_and_book_snapshot(db):
    o = OrderRequest("c1", TS, "BTC", "buy", 1.0, 100.0, "IOC", "open", "p1", "g1")
    assert db.order_status("perp", "c1") is None
    db.record_order("perp", o)
    assert db.order_status("perp", "c1") == "NEW"
    db.set_order_status("perp", "c1", "FILLED")
    assert db.order_status("perp", "c1") == "FILLED"
    assert db.order_status("spot", "c1") is None
    db.record_book_snapshot(TS, "BTC", [(100.0, 1.0)], [(101.0, 2.0)])
    with db.engine.connect() as c:
        row = c.execute(text("SELECT ts, symbol, bids, asks FROM book_snapshots")).one()
    assert row[1] == "BTC"
    assert json.loads(row[2]) == [[100.0, 1.0]] and json.loads(row[3]) == [[101.0, 2.0]]
    assert pd.Timestamp(row[0]) == TS
