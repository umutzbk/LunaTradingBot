"""LiveEngine: urgent exits, lock robustness, data misses, /resume, candidate outcomes."""

from __future__ import annotations

import dataclasses
from types import SimpleNamespace

import pandas as pd
import pytest

from luna.planner import plan_exit
from luna.store import models as m
from tests.engine.test_live import (
    Clock,
    FakeFeed,
    RecNotifier,
    Scripted,
    db,  # noqa: F401  (fixture)
    engine,
    insts,
    orders,
    panel_of,
    risk_events,
    rows,
    tick_at,
)


def _fill_through(feed, clock, order, assets=("BTC",)):
    """Make the spot feed print one trade through ``order``'s limit (fills a resting post-only)."""
    def trades(a, since):
        hit = a in assets
        return pd.DataFrame({
            "price": [order["price"] * (0.999 if order["side"] == "buy" else 1.001)] if hit else [],
            "volume": [1e6] if hit else [],
            "time": pd.to_datetime([clock.now + pd.Timedelta(seconds=30)] if hit else [], utc=True),
        }), since + 1
    feed.trades_since = trades


def _quiet(feed):
    feed.trades_since = FakeFeed.trades_since.__get__(feed)


def _open(db, *, kind="perp", exits=(), notifier=None, k=50):  # noqa: F811
    """Engine with one booked BTC long opened at bar k."""
    panel = panel_of(100)
    idx = panel["BTC"].index
    clock = Clock(tick_at(idx[k]))
    eng, feed, _ = engine(db, panel, clock, Scripted({("BTC", idx[k]): 1}, exits=exits), kind=kind,
                          notifier=notifier)
    eng.start()
    eng.tick(clock.now)
    if kind == "spot":
        (entry,) = orders(db)
        _fill_through(feed, clock, entry)
    eng.minute_check(clock.now + pd.Timedelta(seconds=40))
    _quiet(feed)
    assert len(eng.pf.positions) == 1
    return eng, feed, clock, idx


def _crash(eng, feed):
    (pos,) = eng.pf.positions.values()
    feed.mid_override["BTC"] = pos.entry_ref_price["BTC"] * 0.94  # loss > 2% of equity


def closes(db):  # noqa: F811
    return [o for o in orders(db) if o["intent"] == "close"]


def test_planner_attempt_suffix_keeps_historical_ids():
    p = SimpleNamespace(id="m:BTC:1", qty={"BTC": 1.0})
    ins = insts(["BTC"])
    ts = pd.Timestamp("2024-01-01 10:00:20", tz="UTC")
    (o0,) = plan_exit(p, {"BTC": 100.0}, ins, "perp", ts, 0.003).orders
    (o2,) = plan_exit(p, {"BTC": 100.0}, ins, "perp", ts, 0.003, attempt=2).orders
    assert o0.client_id == "m:BTC:1:exit:BTC:close:202401011000"
    assert o2.client_id == o0.client_id + ":a2"


def test_emergency_ioc_partial_fill_retried_next_minute(db):  # noqa: F811
    eng, feed, clock, _ = _open(db)
    (pos,) = eng.pf.positions.values()
    _crash(eng, feed)
    feed.depth = pos.qty["BTC"] * 0.4  # the book takes 40% of the position per order
    t0 = clock.now + pd.Timedelta(seconds=45)  # same minute as the booking minute check
    for k in range(4):
        eng.minute_check(t0 + k * pd.Timedelta(seconds=60))
    cl = closes(db)
    assert len(cl) == 3 and len({o["client_id"] for o in cl}) == 3
    assert eng.pf.positions == {} and eng.trades[-1]["exit_reason"] == "emergency_stop"
    assert not risk_events(db, "duplicate_client_id")


def test_emergency_ioc_rejected_retried_next_minute(db):  # noqa: F811
    eng, feed, clock, _ = _open(db)
    _crash(eng, feed)
    feed.book_fail = True  # IOC rejected: no book
    t0 = clock.now + pd.Timedelta(seconds=45)
    eng.minute_check(t0)
    eng.minute_check(t0 + pd.Timedelta(seconds=10))  # same minute: retried with a fresh client id
    assert [o["status"] for o in closes(db)] == ["REJECTED", "REJECTED"]
    feed.book_fail = False
    eng.minute_check(t0 + pd.Timedelta(seconds=60))
    eng.minute_check(t0 + pd.Timedelta(seconds=120))
    assert sorted(o["status"] for o in closes(db)) == ["FILLED", "REJECTED", "REJECTED"]
    assert eng.pf.positions == {} and eng.trades[-1]["exit_reason"] == "emergency_stop"
    assert not risk_events(db, "duplicate_client_id")


@pytest.mark.parametrize("kind", ["perp", "spot"])
def test_kill_shortly_after_strategy_exit(db, kind):  # noqa: F811
    eng, feed, clock, idx = _open(db, kind=kind, exits=[pd.Timestamp("2024-01-01 12:45", tz="UTC")])
    clock.now = tick_at(idx[51])
    eng.tick(clock.now)  # bar 51 = 12:45: the strategy's exit is submitted
    (strategy_exit,) = closes(db)
    clock.now = clock.now + pd.Timedelta(seconds=20)  # /kill within the same minute
    eng.kill()
    got = closes(db)
    if kind == "perp":  # the IOC exit had already filled: booked, nothing more to send
        assert len(got) == 1 and eng.pf.positions == {}
        assert eng.trades[-1]["exit_reason"] == "signal"
    else:  # the resting post-only exit is replaced by an IOC with a fresh client id
        assert {o["client_id"]: o["status"] for o in got}[strategy_exit["client_id"]] == "CANCELED"
        (ioc,) = [o for o in got if o["tif"] == "IOC"]
        assert ioc["client_id"] != strategy_exit["client_id"]
        eng.minute_check(clock.now + pd.Timedelta(seconds=60))
        assert eng.pf.positions == {} and eng.trades[-1]["exit_reason"] == "kill_switch"
    assert not risk_events(db, "duplicate_client_id")


def test_drawdown_kill_on_spot_exits_ioc(db):  # noqa: F811
    eng, feed, clock, idx = _open(db, kind="spot")
    eng.pipe.state = dataclasses.replace(eng.pipe.state, peak_equity=eng.pf.equity() * 2)
    clock.now = tick_at(idx[51])
    eng.tick(clock.now)  # R4: FLATTEN_AND_KILL
    assert db.get_state("spot", "kill_switch") is True
    (cl,) = closes(db)
    assert cl["tif"] == "IOC"


def test_duplicate_client_id_is_recorded(db):  # noqa: F811
    eng, feed, clock, _ = _open(db)
    (pos,) = eng.pf.positions.values()
    from luna.types import OrderRequest

    o = OrderRequest(orders(db)[0]["client_id"], clock.now, "BTC", "sell", 1.0, 1.0, "IOC", "close",
                     pos.id, "exit")
    eng.pipe.broker.submit(o)  # the engine's submitter: a known client id is never resubmitted
    assert risk_events(db, "duplicate_client_id")
    assert len(orders(db)) == 1


def test_lock_takeover_stops_submitting(db):  # noqa: F811
    panel = panel_of(100)
    idx = panel["BTC"].index
    clock = Clock(tick_at(idx[50]))
    note = RecNotifier()
    eng, _, _ = engine(db, panel, clock, Scripted({("BTC", idx[50]): 1}), notifier=note)
    eng.start()
    assert db.acquire_lock("perp", "B", stale_after_s=-1)  # our lock looked stale: B took over
    eng.tick(clock.now)
    assert orders(db) == []
    assert risk_events(db, "lock_lost") and eng._stop.is_set()
    assert any("lock" in s for s in note.sent)
    engine_state = db.get_state("perp", "engine")
    eng.minute_check(clock.now + pd.Timedelta(seconds=40))
    eng.close()
    assert db.get_state("perp", "lock")["owner"] == "B"  # never released someone else's lock
    assert db.get_state("perp", "engine") == engine_state  # never wrote state after the loss


def test_heartbeat_runs_first_in_loop_and_stops_on_loss(db):  # noqa: F811
    panel = panel_of(100)
    clock = Clock(pd.Timestamp("2024-01-01 12:14:50", tz="UTC"))
    eng, _, _ = engine(db, panel, clock, Scripted({}))
    eng.start()
    calls = []
    eng.tick = lambda now: calls.append("tick")
    eng.minute_check = lambda now: calls.append("min")

    def wait(seconds):
        clock.now = clock.now + pd.Timedelta(seconds=seconds)
        if clock.now >= pd.Timestamp("2024-01-01 12:16", tz="UTC"):
            db.acquire_lock("perp", "B", stale_after_s=-1)
        return eng._stop.is_set()

    eng._stop.wait = wait
    eng.run_forever()
    assert eng._stop.is_set() and risk_events(db, "lock_lost")
    assert calls == ["min", "tick", "min"]  # nothing ran after the heartbeat found the loss


def test_stale_ticks_with_positions_pause(db):  # noqa: F811
    eng, feed, clock, idx = _open(db)
    feed.lag = 4  # no new bar arrives (the feed is an hour behind)
    for k in (51, 52):
        clock.now = tick_at(idx[k])
        eng.tick(clock.now)
    assert db.get_state("perp", "paused") is True
    assert len(risk_events(db, "stale_data")) == 2


def test_resume_returns_killed_positions_to_strategy(db):  # noqa: F811
    eng, feed, clock, idx = _open(db, exits=[pd.Timestamp("2024-01-01 13:15", tz="UTC")])
    feed.book_fail = True  # kill exits are rejected: the position stays marked "kill_switch"
    clock.now = clock.now + pd.Timedelta(minutes=2)
    eng.kill()
    (pid,) = eng.pf.positions
    assert eng.pipe.exiting[pid] == "kill_switch"
    eng.resume()
    assert pid not in eng.pipe.exiting
    feed.book_fail = False
    n = len(closes(db))
    eng.minute_check(clock.now + pd.Timedelta(seconds=60))
    assert len(closes(db)) == n and len(eng.pf.positions) == 1  # no longer flattened
    for k in (51, 52, 53):  # bar 53 = 13:15: the strategy's own exit
        clock.now = tick_at(idx[k])
        eng.tick(clock.now)
    eng.minute_check(clock.now + pd.Timedelta(seconds=40))
    assert eng.pf.positions == {} and eng.trades[-1]["exit_reason"] == "signal"


def test_candidate_outcome_and_entry_notification_on_fill(db):  # noqa: F811
    note = RecNotifier()
    panel = panel_of(100)
    idx = panel["BTC"].index
    clock = Clock(tick_at(idx[50]))
    eng, feed, _ = engine(db, panel, clock, Scripted({("BTC", idx[50]): 1, ("ETH", idx[50]): 1}),
                          kind="spot", notifier=note)
    eng.start()
    eng.tick(clock.now)
    assert [c["status"] for c in rows(db, m.candidates)] == ["submitted", "submitted"]
    assert not any(s.startswith("[spot] entry") for s in note.sent)  # not on submit
    btc = next(o for o in orders(db) if o["symbol"] == "BTC")
    _fill_through(feed, clock, btc)
    eng.minute_check(clock.now + pd.Timedelta(seconds=60))
    _quiet(feed)
    eng.minute_check(clock.now + pd.Timedelta(minutes=16))  # ETH post-only expires unfilled
    status = {c["key"]: c["status"] for c in rows(db, m.candidates)}
    assert status == {"BTC": "filled", "ETH": "unfilled"}
    assert sum(s.startswith("[spot] entry filled: scripted BTC") for s in note.sent) == 1
    assert not any("ETH" in s and "entry" in s for s in note.sent)


@pytest.mark.parametrize("kind", ["perp", "spot"])
@pytest.mark.parametrize("trigger", ["drawdown", "external_flag"])
def test_kill_overrides_a_pending_strategy_exit(db, kind, trigger):  # noqa: F811
    """A position already exiting by signal when the kill switch turns on (R4 in a tick, or the
    Db flag set by someone else) is re-marked kill_switch: its resting post-only exit is
    cancelled and it is flattened as IOC within a minute or two."""
    eng, feed, clock, idx = _open(db, kind=kind, exits=[pd.Timestamp("2024-01-01 12:45", tz="UTC")])
    if kind == "perp":
        feed.book_fail = True  # the strategy's IOC exit is rejected: still exiting by "signal"
    clock.now = tick_at(idx[51])
    eng.tick(clock.now)
    (pid,) = eng.pf.positions
    assert eng.pipe.exiting[pid] == "signal"
    (strategy_exit,) = closes(db)
    if trigger == "drawdown":
        eng.pipe.state = dataclasses.replace(eng.pipe.state, peak_equity=eng.pf.equity() * 2)
        clock.now = tick_at(idx[52])
        eng.tick(clock.now)  # R4: FLATTEN_AND_KILL
    else:
        db.set_flag(kind, "kill_switch", True)
        clock.now = clock.now + pd.Timedelta(seconds=40)
        eng.minute_check(clock.now)
    assert db.get_state(kind, "kill_switch") is True
    assert eng.pipe.exiting.get(pid) == "kill_switch"
    feed.book_fail = False
    for k in range(3):
        eng.minute_check(clock.now + pd.Timedelta(seconds=60 * (k + 1)))
    got = {o["client_id"]: o for o in closes(db)}
    if kind == "spot":
        assert got[strategy_exit["client_id"]]["status"] == "CANCELED"
    later = [o for cid, o in got.items() if cid != strategy_exit["client_id"]]
    assert later and all(o["tif"] == "IOC" for o in later)
    assert eng.pf.positions == {} and eng.trades[-1]["exit_reason"] == "kill_switch"
    assert not risk_events(db, "duplicate_client_id")


def test_lock_lost_during_model_fit_stops(db):  # noqa: F811
    panel = panel_of(100)
    clock = Clock(tick_at(panel["BTC"].index[50]))
    eng, _, _ = engine(db, panel, clock, Scripted({}))
    eng.start()

    def refit(now):
        db.acquire_lock("perp", "B", stale_after_s=-1)  # taken over while fitting
        return eng.model

    eng._model_fn = refit
    eng._ensure_model(clock.now, force=True)
    assert eng._lock_lost and eng._stop.is_set() and risk_events(db, "lock_lost")
    n_events = len(risk_events(db))
    eng.tick(clock.now)
    eng._commit(clock.now, equity=True)
    assert len(risk_events(db)) == n_events and rows(db, m.equity) == []
