"""LiveEngine (paper mode): fake clock, fake feed from synthetic bars, SQLite Db in tmp_path."""

from __future__ import annotations

import dataclasses
import json
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import select

from luna.config import load_settings
from luna.engine.backtest import run_backtest
from luna.engine.feeds import FeedError
from luna.engine.live import EngineLocked, LiveEngine
from luna.execution.costs import CostModel
from luna.execution.paper import PaperBroker
from luna.execution.sim import SimBroker
from luna.ranking.ranker import AcceptAllRanker
from luna.store import models as m
from luna.store.db import Db
from luna.strategy.base import SignalFrame
from luna.types import Candidate, ExitSpec, Instrument, Leg, OrderRequest, StopSpec
from tests.helpers import synthetic_bars

SETTINGS = load_settings()
BAR = pd.Timedelta("15min")
OFF = pd.Timedelta(seconds=20)
START = pd.Timestamp("2000-01-01", tz="UTC")
END = pd.Timestamp("2100-01-01", tz="UTC")


def insts(assets, kind="perp"):
    venue = "kraken_futures" if kind == "perp" else "kraken_spot"
    return {a: Instrument(a, a, venue, kind, 1e-6, 0.01, 1e-6, 0.0, 0.0002, 0.0005, 0.02, 0.01)
            for a in assets}


def panel_of(n=400, seed=0, assets=("BTC", "ETH")):
    return {a: synthetic_bars(n, "random_walk", seed=seed + k, sigma=0.001, start_price=100.0 * (k + 1))
            for k, a in enumerate(assets)}


def tick_at(bar_open: pd.Timestamp) -> pd.Timestamp:
    """The live tick that decides bar ``bar_open`` (its close + 20 s)."""
    return bar_open + BAR + OFF


class Scripted:
    """Entries at given (key, bar ts) -> direction; exits (signal) at given bar ts. Works on any
    prefix of the panel (the live buffer)."""

    name = "scripted"

    def __init__(self, entries: dict[tuple[str, pd.Timestamp], int], exits=(), stop=0.01):
        self.params = {}
        self.entries, self.exits, self.stop = entries, set(exits), stop

    def signals(self, panel, keys):
        out = {}
        for key in keys:
            idx = panel[key].index
            entry = pd.Series(np.zeros(len(idx), dtype="int8"), index=idx)
            for (k, ts), d in self.entries.items():
                if k == key and ts in idx:
                    entry[ts] = d
            ex = pd.Series(idx.isin(list(self.exits)), index=idx)
            out[key] = SignalFrame(
                key=key, legs_beta=None, entry=entry, exit_long=ex, exit_short=ex,
                stop_distance=pd.Series(self.stop, index=idx), stop_kind="price", stop_z=None,
                z=pd.Series(np.float32(0.5), index=idx), atr=None, exit_spec=ExitSpec(10_000),
                regime=pd.Series("NONE", index=idx), ref=panel[key]["close"],
            )
        return out

    def candidate_at(self, sf, i, mids):
        d = int(sf.entry.iloc[i])
        if d == 0:
            return None
        ref = float(sf.ref.iloc[i])
        return Candidate(sf.entry.index[i], self.name, sf.key, (Leg(sf.key, 1.0),), d, ref,
                         StopSpec("price", ref * (1 - d * self.stop), self.stop), sf.exit_spec,
                         "NONE", "scripted", {"z": sf.z.iloc[i], "n": np.int64(3)})

    def should_exit(self, sf, i, pos, bar_close):
        return "signal" if bool(sf.exit_long.iloc[i]) else None


def model_of(strat, keys):
    return SimpleNamespace(strategies=[strat], keys={strat.name: list(keys)}, ranker=AcceptAllRanker(),
                           table={}, window=None)


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


class FakeFeed:
    """Serves the closed bars of a synthetic panel as of ``now``; book / mids around the last close."""

    def __init__(self, panel, clock, spread=0.0002):
        self.panel = panel
        self.clock = clock
        self.assets = sorted(panel)
        self.spread = spread
        self.fail = False
        self.lag = 0  # serve bars this many bars older than the newest closed one (stale feed)
        self.leak = False  # also serve the still-forming bar (a buggy feed)
        self.rates: dict[str, float | None] = {}
        self.mid_override: dict[str, float] = {}
        self.depth = 1e9  # size of the single book level on each side
        self.book_fail = False

    def _closed(self, a, now):
        df = self.panel[a]
        cut = now - self.lag * BAR
        keep = df.index + BAR <= cut
        if self.leak:
            keep = df.index <= now
        return df[keep]

    def refresh(self, now):
        if self.fail:
            raise FeedError("exchange unavailable (HTTP 503)")
        return {a: self._closed(a, now) for a in self.assets if a in self.panel}

    def funding_rates(self):
        return dict(self.rates)

    def _mid(self, a):
        if a in self.mid_override:
            return self.mid_override[a]
        return float(self._closed(a, self.clock())["close"].iloc[-1])

    def mids(self):
        return {a: self._mid(a) for a in self.panel}

    def book(self, a):
        if self.book_fail:
            raise FeedError("orderbook unavailable")
        mid = self._mid(a)
        return [(mid * (1 - self.spread), self.depth)], [(mid * (1 + self.spread), self.depth)]

    def trades_since(self, a, since_ns):
        return pd.DataFrame({"price": pd.Series(dtype="float64"), "volume": pd.Series(dtype="float64"),
                             "time": pd.to_datetime(pd.Series([], dtype="int64"), utc=True)}), since_ns


class RecNotifier:
    def __init__(self, queued=None):
        self.sent: list[str] = []
        self.queue: list[list[tuple[str, str]]] = [list(queued or [])]

    def send(self, text):
        self.sent.append(text)

    def commands(self):
        return self.queue.pop(0) if self.queue else []


class ReplayBroker:
    """SimBroker semantics (fills on the bar after the decision bar), with PaperBroker-style
    persistence (order NEW -> FILLED / CANCELED, fill rows) so the engine books from the Db."""

    def __init__(self, kind, panel, db, account, instruments):
        self.sim = SimBroker(kind, CostModel(SETTINGS.costs[kind]), instruments)
        self.panel, self.db, self.account = panel, db, account
        self.pending: dict[str, tuple[OrderRequest, pd.Timestamp]] = {}
        self.last = None

    def submit(self, o, ref_mid=None):
        self.db.record_order(self.account, o, "NEW")
        decided = o.ts.floor("15min") - BAR
        self.sim.submit(dataclasses.replace(o, ts=decided))
        self.pending[o.client_id] = (o, decided)

    def poll(self, now):
        b = now.floor("15min") - BAR
        if self.last is not None and b <= self.last:
            return []
        self.last = b
        bars = {a: {k: df.loc[b, k] for k in ("open", "high", "low", "close", "filled")}
                for a, df in self.panel.items() if b in df.index}
        fills = self.sim.on_bar(b, bars)
        done = {f.client_id for f in fills}
        for f in fills:
            self.db.record_fill(self.account, f, position_id=self.pending[f.client_id][0].position_id)
        for cid, (_, dec) in list(self.pending.items()):
            if dec < b:
                self.db.set_order_status(self.account, cid, "FILLED" if cid in done else "CANCELED")
                del self.pending[cid]
        return fills

    def cancel(self, cid):
        self.pending.pop(cid, None)


@pytest.fixture
def db(tmp_path):
    d = Db(f"sqlite:///{(tmp_path / 'luna.db').as_posix()}")
    yield d
    d.close()


def engine(db, panel, clock, strat, *, kind="perp", mode="paper", broker=None, feed=None, notifier=None,
           settings=SETTINGS):
    feed = feed or FakeFeed(panel, clock)
    instruments = insts(panel, kind)
    broker = broker or PaperBroker(kind, CostModel(settings.costs[kind]), feed, db, kind,
                                   band=settings.accounts[kind].max_price_band)
    model = model_of(strat, sorted(panel))
    eng = LiveEngine(kind, settings, feed, broker, db, notifier or RecNotifier(), clock, mode,
                     instruments=instruments, model_fn=lambda now: model)
    return eng, feed, broker


def rows(db, table):
    with db.engine.connect() as c:
        return c.execute(select(table)).mappings().all()


def orders(db):
    return rows(db, m.orders)


def risk_events(db, rule=None):
    return [r for r in rows(db, m.risk_events) if rule is None or r["rule"] == rule]


# --- the key test: live == backtest on a replay ---------------------------------------------------


@pytest.mark.parametrize("kind", ["perp", "spot"])
def test_paper_equals_backtest_on_replay(db, kind):
    panel = panel_of(400, seed=3)
    idx = panel["BTC"].index
    entries = {("BTC", idx[30]): 1, ("ETH", idx[45]): 1, ("BTC", idx[140]): -1,
               ("ETH", idx[150]): -1, ("BTC", idx[260]): 1, ("ETH", idx[330]): 1}
    exits = [idx[100], idx[230], idx[300]]
    strat = Scripted(entries, exits)
    keys = {"scripted": ["BTC", "ETH"]}
    bt = run_backtest(panel, {}, [strat], keys, AcceptAllRanker(), kind, SETTINGS, START, END,
                      instruments=insts(panel, kind))
    assert len(bt.trades) >= 3 and "emergency_stop" not in set(bt.trades["exit_reason"])

    clock = Clock(tick_at(idx[0]) - pd.Timedelta(minutes=5))
    broker = ReplayBroker(kind, panel, db, kind, insts(panel, kind))
    eng, _, _ = engine(db, panel, clock, Scripted(entries, exits), kind=kind, broker=broker)
    eng.start()
    for ts in idx:
        clock.now = tick_at(ts)
        eng.tick(clock.now)

    cols = ["strategy", "key", "direction", "entry_ts", "exit_ts", "exit_reason", "gross_notional",
            "risk_amount", "pnl_gross", "fees", "spread_slip", "funding", "funding_unknown_bars",
            "pnl_net"]
    live = pd.DataFrame(eng.trades)[cols].sort_values(["entry_ts", "key"]).reset_index(drop=True)
    back = bt.trades[cols].sort_values(["entry_ts", "key"]).reset_index(drop=True)
    pd.testing.assert_frame_equal(live, back, check_dtype=False, rtol=1e-12)
    assert eng.pf.equity() == pytest.approx(float(bt.equity.iloc[-1]), rel=1e-12)
    assert set(eng.pf.positions) and len(eng.pf.positions) == len(bt.portfolio.positions)
    if kind == "perp":  # funding hours booked hourly (unknown: no published rate)
        assert back["funding_unknown_bars"].sum() > 0
    # every order went through the Db exactly once; closed positions persisted
    assert len({o["client_id"] for o in orders(db)}) == len(orders(db))
    closed = [r for r in rows(db, m.positions) if r["status"] == "closed"]
    assert len(closed) == len(bt.trades)


# --- clock / data hygiene -------------------------------------------------------------------------


def test_tick_uses_only_closed_bars(db):
    panel = panel_of(200)
    idx = panel["BTC"].index
    k = 120
    forming = idx[k + 1]
    # BTC fires on the still-forming bar (must be invisible), ETH on the last closed bar
    strat = Scripted({("BTC", forming): 1, ("ETH", idx[k]): 1})
    clock = Clock(tick_at(idx[k]))
    eng, feed, _ = engine(db, panel, clock, strat)
    feed.leak = True
    eng.start()
    eng.tick(clock.now)
    assert eng._last_bar == idx[k]
    got = orders(db)
    assert [o["symbol"] for o in got] == ["ETH"]
    close = float(panel["ETH"].loc[idx[k], "close"])
    assert got[0]["price"] == pytest.approx(close * (1 + SETTINGS.accounts["perp"].max_price_band))
    assert pd.Timestamp(got[0]["ts"]) == clock.now  # stamped with the real tick time
    with pytest.raises(ValueError, match="tz-aware"):
        eng.tick(pd.Timestamp("2024-01-02 10:00:20"))


def test_tick_skips_on_feed_error(db):
    panel = panel_of(200)
    idx = panel["BTC"].index
    strat = Scripted({("BTC", idx[50]): 1, ("ETH", idx[60]): 1})
    clock = Clock(tick_at(idx[50]))
    note = RecNotifier()
    eng, feed, _ = engine(db, panel, clock, strat, notifier=note)
    eng.start()
    eng.tick(clock.now)  # BTC entry submitted (IOC, filled at the book)
    clock.now = tick_at(idx[51])
    eng.tick(clock.now)
    assert len(eng.pf.positions) == 1
    n_orders = len(orders(db))

    feed.fail = True
    clock.now = tick_at(idx[52])
    eng.tick(clock.now)  # no crash, tick skipped
    assert len(risk_events(db, "feed_error")) == 1
    assert not db.get_state("perp", "paused", False)
    clock.now = tick_at(idx[53])
    eng.tick(clock.now)  # 2nd in a row with an open position -> paused
    assert db.get_state("perp", "paused", False) is True
    assert any("paused" in s for s in note.sent)
    for b in (54, 55):
        clock.now = tick_at(idx[b])
        eng.tick(clock.now)
    assert len(risk_events(db, "feed_error")) == 4
    assert any("feed failing" in s for s in note.sent)
    assert len(orders(db)) == n_orders  # nothing traded on missing data
    # data back: the ETH signal (bar 60) is vetoed by the pause (R2), exits would still run
    feed.fail = False
    for b in range(56, 61):
        clock.now = tick_at(idx[b])
        eng.tick(clock.now)
    cands = rows(db, m.candidates)
    eth = [c for c in cands if c["key"] == "ETH"]
    assert eth and eth[-1]["reject_reason"].startswith("R2_pause")
    assert len(orders(db)) == n_orders


def test_dead_feed_exits_for_restart_after_8_ticks(db):
    """feed_error ticks (stale DNS): pause with positions after 2, exit code 3 + risk event
    feed_dead_exit after 8 in a row; state persisted, lock released, a restart resumes."""
    panel = panel_of(200)
    idx = panel["BTC"].index
    clock = Clock(tick_at(idx[40]))
    eng, feed, _ = engine(db, panel, clock, Scripted({("BTC", idx[40]): 1}))
    eng.start()
    eng.tick(clock.now)
    clock.now = tick_at(idx[41])
    eng.tick(clock.now)
    assert len(eng.pf.positions) == 1
    feed.fail = True
    for k in range(42, 49):  # 7 dead ticks: paused, still running
        clock.now = tick_at(idx[k])
        eng.tick(clock.now)
    assert eng.exit_code == 0 and not eng._stop.is_set()
    assert db.get_state("perp", "paused") is True
    feed.fail = False
    clock.now = tick_at(idx[49])
    eng.tick(clock.now)  # the feed answers again: the dead counter resets
    feed.fail = True
    for k in range(50, 57):
        clock.now = tick_at(idx[k])
        eng.tick(clock.now)
    assert eng.exit_code == 0
    clock.now = tick_at(idx[57])
    eng.tick(clock.now)  # 8th dead tick in a row
    assert eng.exit_code == 3 and eng.exit_reason == "feed_dead_exit" and eng._stop.is_set()
    assert len(risk_events(db, "feed_dead_exit")) == 1
    eng.run_forever()  # returns at once (stopped)
    eng.close()
    assert db.get_state("perp", "lock") is None  # released cleanly

    feed.fail = False
    b, _, _ = engine(db, panel, clock, Scripted({}))
    b.start()  # the restarted process reloads the position
    assert len(b.pf.positions) == 1 and b.exit_code == 0
    b.close()


def test_cli_paper_exits_nonzero_when_engine_requests_it(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    import luna.engine.live as live_mod
    import luna.instruments
    from luna.cli import app
    from luna.engine.backtest import default_instruments

    def fake_start(self):
        self._started = True

    def fake_run(self):
        self.exit_code, self.exit_reason = 3, "feed_dead_exit"

    monkeypatch.setenv("LUNA_VAR_DIR", str(tmp_path))
    monkeypatch.setattr(luna.instruments, "load_instruments",
                        lambda settings, kind, *a, **k: default_instruments(["BTC"], kind, SETTINGS))
    monkeypatch.setattr(live_mod.LiveEngine, "start", fake_start)
    monkeypatch.setattr(live_mod.LiveEngine, "run_forever", fake_run)
    monkeypatch.setattr(live_mod.LiveEngine, "close", lambda self: None)
    import logging

    level = logging.getLogger("httpx").level
    try:
        res = CliRunner().invoke(app, ["paper", "--account", "perp"])
    finally:
        logging.getLogger("httpx").setLevel(level)  # the CLI sets it globally
    assert res.exit_code == 3 and "feed_dead_exit" in res.output


def test_stale_bars_block_entries(db):
    panel = panel_of(200)
    idx = panel["BTC"].index
    k = 100
    stale_last = idx[k - 4]  # the feed only has bars up to 1 hour before the newest closed one
    strat = Scripted({("BTC", stale_last): 1})
    clock = Clock(tick_at(idx[k]))
    eng, feed, _ = engine(db, panel, clock, strat)
    feed.lag = 4
    eng.start()
    eng.tick(clock.now)
    assert orders(db) == []
    (c,) = rows(db, m.candidates)
    assert c["key"] == "BTC" and not c["selected"] and c["reject_reason"].startswith("R16_stale_data")
    # the same (stale) bar again: no second decision, recorded as stale
    clock.now = clock.now + pd.Timedelta(minutes=15)
    feed.lag = 5
    eng.tick(clock.now)
    assert len(rows(db, m.candidates)) == 1
    assert risk_events(db, "stale_data")


# --- restart safety -------------------------------------------------------------------------------


def test_restart_resumes_positions_without_duplicate_orders(db):
    panel = panel_of(200)
    idx = panel["BTC"].index
    entries = {("BTC", idx[40]): 1, ("ETH", idx[40]): -1}
    exits = [idx[60]]
    clock = Clock(tick_at(idx[40]))
    a, _, _ = engine(db, panel, clock, Scripted(entries, exits))
    a.start()
    a.tick(clock.now)  # both IOC entries filled by the paper broker at submit, not yet booked
    assert len(orders(db)) == 2 and len(rows(db, m.fills)) == 2
    assert a.pf.positions == {}
    db.release_lock("perp", a.owner)  # crash: the lock goes stale, nothing else is cleaned up

    clock.now = clock.now + pd.Timedelta(minutes=3)  # restart inside the same bar
    b, _, _ = engine(db, panel, clock, Scripted(entries, exits))
    b.start()
    assert sorted(p.key for p in b.pf.positions.values()) == ["BTC", "ETH"]
    fills = {f["symbol"]: f for f in rows(db, m.fills)}
    for p in b.pf.positions.values():
        q = p.qty[p.key]
        assert abs(q) == pytest.approx(fills[p.key]["qty"])
        assert (q > 0) == (fills[p.key]["side"] == "buy")
    assert b.pf.holdings == {p.key: p.qty[p.key] for p in b.pf.positions.values()}
    b.tick(clock.now)  # same bar: already decided before the crash -> no new orders
    assert len(orders(db)) == 2
    b.close()

    c, _, _ = engine(db, panel, clock, Scripted(entries, exits))  # clean restart, same bar again
    c.start()
    assert len(c.pf.positions) == 2
    for k in range(41, 63):
        clock.now = tick_at(idx[k])
        c.tick(clock.now)
    got = orders(db)
    assert len(got) == 4  # 2 entries + 2 exits (bar 60), nothing duplicated
    assert sorted(o["intent"] for o in got) == ["close", "close", "open", "open"]
    assert len({o["client_id"] for o in got}) == 4
    assert c.pf.positions == {} and len(c.trades) == 2
    eq_fill_count = len(rows(db, m.fills))
    assert eq_fill_count == 4


def test_restart_cancels_working_post_only_orders(db):
    panel = panel_of(200)
    idx = panel["BTC"].index
    clock = Clock(tick_at(idx[40]))
    a, _, _ = engine(db, panel, clock, Scripted({("BTC", idx[40]): 1}), kind="spot")
    a.start()
    a.tick(clock.now)
    (o,) = orders(db)
    assert o["tif"] == "POST_ONLY" and o["status"] == "OPEN"
    db.release_lock("spot", a.owner)  # crash with a resting order

    clock.now = clock.now + pd.Timedelta(minutes=2)
    b, _, _ = engine(db, panel, clock, Scripted({("BTC", idx[40]): 1}), kind="spot")
    b.start()
    (o,) = orders(db)
    assert o["status"] == "CANCELED"  # cancelled, never resubmitted
    assert b.pf.positions == {} and b.pipe.entry_groups == {} and b.pipe.submitted == {}
    assert risk_events(db, "restart_cancel")
    for k in range(41, 45):
        clock.now = tick_at(idx[k])
        b.tick(clock.now)
    assert len(orders(db)) == 1


def test_spot_entry_resting_order_carries_risk_reference_mid(db):
    """The engine hands the broker the mid the risk check used: reprices are band-bounded by it."""
    panel = panel_of(200)
    idx = panel["BTC"].index
    clock = Clock(tick_at(idx[40]))
    eng, _, broker = engine(db, panel, clock, Scripted({("BTC", idx[40]): 1}), kind="spot")
    eng.start()
    eng.tick(clock.now)
    ((cid, (o, ref)),) = eng.pipe.submitted.items()
    assert o.tif == "POST_ONLY"
    assert broker._resting[cid].ref_mid == pytest.approx(ref)
    assert broker.band == SETTINGS.accounts["spot"].max_price_band


def test_working_spot_entry_blocks_same_bucket_entry(db):
    """A resting (unfilled) spot entry counts in construct: a second strategy's same-asset,
    same-direction entry on the next bar is rejected (corr_bucket) instead of bypassing it."""

    class Other(Scripted):
        name = "other"

    panel = panel_of(200)
    idx = panel["BTC"].index
    clock = Clock(tick_at(idx[40]))
    first, second = Scripted({("BTC", idx[40]): 1}), Other({("BTC", idx[41]): 1})
    model = SimpleNamespace(strategies=[first, second], keys={"scripted": ["BTC"], "other": ["BTC"]},
                            ranker=AcceptAllRanker(), table={}, window=None)
    feed = FakeFeed(panel, clock)
    broker = PaperBroker("spot", CostModel(SETTINGS.costs["spot"]), feed, db, "spot",
                         band=SETTINGS.accounts["spot"].max_price_band)
    eng = LiveEngine("spot", SETTINGS, feed, broker, db, RecNotifier(), clock, "paper",
                     instruments=insts(panel, "spot"), model_fn=lambda now: model)
    eng.start()
    eng.tick(clock.now)
    assert len(eng.pipe.entry_groups) == 1 and eng.pf.positions == {}  # resting, unfilled
    clock.now = tick_at(idx[41]) - pd.Timedelta(seconds=1)  # entry still inside its 15-min TTL
    eng.tick(clock.now)
    assert len(orders(db)) == 1 and len(eng.pipe.entry_groups) == 1
    (c,) = [r for r in rows(db, m.candidates) if r["strategy"] == "other"]
    assert c["reject_reason"] == "corr_bucket"


def test_day_start_set_after_downtime(db):
    panel = panel_of(4 * 96 + 50)
    idx = panel["BTC"].index  # starts 2024-01-01 00:00
    strat = Scripted({("BTC", idx[40]): 1})
    clock = Clock(tick_at(idx[40]))
    a, feed, _ = engine(db, panel, clock, strat)
    a.start()
    a.tick(clock.now)
    clock.now = tick_at(idx[41])
    a.tick(clock.now)
    assert len(a.pf.positions) == 1
    st = db.load_risk_state("perp")
    assert str(st.day) == "2024-01-01"
    a.close()

    # down for two midnights; first tick after downtime on 2024-01-03
    clock.now = pd.Timestamp("2024-01-03 05:00:20", tz="UTC")
    b, _, _ = engine(db, panel, clock, strat)
    b.start()
    b.tick(clock.now)
    st = db.load_risk_state("perp")
    assert str(st.day) == "2024-01-03"
    eq0 = b.pf.equity()
    assert st.day_start_equity == pytest.approx(eq0) and st.orders_today == 0
    for k in range(1, 8):  # later ticks on the same day never reset it
        clock.now = pd.Timestamp("2024-01-03 05:00:20", tz="UTC") + k * BAR
        b.tick(clock.now)
    assert b.pf.equity() != eq0
    assert db.load_risk_state("perp").day_start_equity == pytest.approx(eq0)
    # the first tick after the next midnight (00:00:20, deciding the 23:45 bar) rolls once
    clock.now = pd.Timestamp("2024-01-04 00:00:20", tz="UTC")
    b.tick(clock.now)
    st = db.load_risk_state("perp")
    assert str(st.day) == "2024-01-04" and st.day_start_equity == pytest.approx(b.pf.equity())


def test_second_process_refused_by_lock(db):
    panel = panel_of(100)
    clock = Clock(tick_at(panel["BTC"].index[50]))
    a, _, _ = engine(db, panel, clock, Scripted({}))
    a.start()
    b, _, _ = engine(db, panel, clock, Scripted({}))
    with pytest.raises(EngineLocked):
        b.start()
    a.close()
    b.start()
    b.close()


def test_live_mode_vetoed_when_disabled(db):
    panel = panel_of(100)
    idx = panel["BTC"].index
    assert SETTINGS.accounts["perp"].live_enabled is False
    clock = Clock(tick_at(idx[50]))
    eng, _, _ = engine(db, panel, clock, Scripted({("BTC", idx[50]): 1}), mode="live")
    eng.start()
    eng.tick(clock.now)
    assert orders(db) == []
    (c,) = rows(db, m.candidates)
    assert c["reject_reason"].startswith("R19_live_disabled")
    assert any(r["detail"] and "R19" in r["detail"] for r in risk_events(db, "veto"))


# --- flags, telegram, candidates, stops, funding --------------------------------------------------


def test_flags_reloaded_each_tick_and_never_clobbered(db):
    panel = panel_of(100)
    idx = panel["BTC"].index
    clock = Clock(tick_at(idx[50]))
    eng, _, _ = engine(db, panel, clock, Scripted({("BTC", idx[52]): 1}))
    eng.start()
    eng.tick(clock.now)
    db.set_flag("perp", "paused", True)  # e.g. another process / the dashboard
    clock.now = tick_at(idx[51])
    eng.tick(clock.now)
    eng.minute_check(clock.now + pd.Timedelta(seconds=40))
    assert db.get_state("perp", "paused") is True  # snapshots never write flags
    clock.now = tick_at(idx[52])
    eng.tick(clock.now)
    assert orders(db) == []
    assert rows(db, m.candidates)[-1]["reject_reason"].startswith("R2_pause")


def test_telegram_commands(db):
    panel = panel_of(100)
    idx = panel["BTC"].index
    clock = Clock(tick_at(idx[50]))
    note = RecNotifier(queued=[("/kill", "")])  # queued while down: drained, not executed
    eng, _, _ = engine(db, panel, clock, Scripted({("BTC", idx[50]): 1}), notifier=note)
    eng.start()
    assert not db.get_state("perp", "kill_switch", False)
    eng.tick(clock.now)
    clock.now = tick_at(idx[51])
    eng.tick(clock.now)
    assert len(eng.pf.positions) == 1

    note.queue = [[("/status", ""), ("/positions", "")]]
    eng.minute_check(clock.now + pd.Timedelta(seconds=30))
    assert any("equity" in s for s in note.sent) and any("BTC" in s and "stop" in s for s in note.sent)

    n_sent = len(note.sent)
    note.queue = [[("/kill", "spot"), ("/pause", "spot")]]  # addressed to the other bot: ignored
    eng.minute_check(clock.now + pd.Timedelta(seconds=40))
    assert not db.get_state("perp", "kill_switch", False) and not db.get_state("perp", "paused", False)
    assert len(note.sent) == n_sent  # silent

    note.queue = [[("/kill", "perp")]]
    eng.minute_check(clock.now + pd.Timedelta(seconds=50))
    assert db.get_state("perp", "kill_switch") is True
    closes = [o for o in orders(db) if o["intent"] == "close"]
    assert len(closes) == 1 and closes[0]["tif"] == "IOC"
    eng.minute_check(clock.now + pd.Timedelta(seconds=110))  # books the exit fill
    assert eng.pf.positions == {}
    assert eng.trades[-1]["exit_reason"] == "kill_switch"

    note.queue = [[("/resume", "")]]
    eng.minute_check(clock.now + pd.Timedelta(seconds=170))
    assert db.get_state("perp", "kill_switch") is False and db.get_state("perp", "paused") is False
    assert eng.pipe.state.peak_equity == pytest.approx(eng.pf.equity())
    assert db.load_risk_state("perp").peak_equity == pytest.approx(eng.pf.equity())

    def boom():
        raise RuntimeError("telegram down")

    note.commands = boom
    eng.minute_check(clock.now + pd.Timedelta(seconds=230))  # never crashes the loop


def test_candidates_recorded_with_reasons_and_plain_types(db):
    panel = panel_of(100)
    idx = panel["BTC"].index
    clock = Clock(tick_at(idx[50]))
    strat = Scripted({("BTC", idx[50]): 1, ("ETH", idx[51]): 1})
    strat.params = {"k": 2}
    eng, _, _ = engine(db, panel, clock, strat)
    eng.start()
    eng.tick(clock.now)
    clock.now = tick_at(idx[51])
    eng.tick(clock.now)
    db.set_flag("perp", "kill_switch", True)
    clock.now = tick_at(idx[52])
    eng.tick(clock.now)
    got = rows(db, m.candidates)
    assert [(c["key"], c["selected"]) for c in got] == [("BTC", True), ("ETH", True)]
    assert [json.loads(c["params"]) for c in got] == [{"k": 2}, {"k": 2}]  # owning instance's params
    f = json.loads(got[0]["features"])
    assert f == {"z": 0.5, "n": 3}
    assert json.loads(got[0]["legs"]) == [["BTC", 1.0]]
    assert json.loads(got[0]["stop"])["distance_frac"] == 0.01


def test_rejected_candidates_recorded(db):
    panel = panel_of(100)
    idx = panel["BTC"].index
    clock = Clock(tick_at(idx[50]))
    eng, _, _ = engine(db, panel, clock, Scripted({("BTC", idx[50]): -1}), kind="spot")
    eng.start()
    eng.tick(clock.now)
    (c,) = rows(db, m.candidates)
    assert not c["selected"] and c["reject_reason"] == "short_not_allowed"


def test_emergency_stop_from_live_mids(db):
    panel = panel_of(100)
    idx = panel["BTC"].index
    clock = Clock(tick_at(idx[50]))
    eng, feed, _ = engine(db, panel, clock, Scripted({("BTC", idx[50]): 1}))
    eng.start()
    eng.tick(clock.now)
    eng.minute_check(clock.now + pd.Timedelta(seconds=40))
    (pos,) = eng.pf.positions.values()
    eq = eng.pf.equity()
    q = pos.qty["BTC"]
    # a live mid where the loss reaches emergency_stop_frac * equity
    feed.mid_override["BTC"] = pos.entry_ref_price["BTC"] - 1.05 * 0.02 * eq / q
    eng.minute_check(clock.now + pd.Timedelta(seconds=100))
    closes = [o for o in orders(db) if o["intent"] == "close"]
    assert len(closes) == 1
    assert risk_events(db, "emergency_stop")
    eng.minute_check(clock.now + pd.Timedelta(seconds=160))
    assert eng.pf.positions == {} and eng.trades[-1]["exit_reason"] == "emergency_stop"


def test_hourly_funding_recorded(db):
    panel = panel_of(100)
    idx = panel["BTC"].index
    k = 50  # bar 12:30 -> tick 12:45:20
    clock = Clock(tick_at(idx[k]))
    eng, feed, _ = engine(db, panel, clock, Scripted({("BTC", idx[k]): 1}))
    eng.start()
    eng.tick(clock.now)
    eng.minute_check(clock.now + pd.Timedelta(seconds=40))
    assert len(eng.pf.positions) == 1
    feed.rates = {"BTC": 0.0001}
    t = clock.now
    eng.minute_check(pd.Timestamp("2024-01-01 13:00:05", tz="UTC"))
    eng.minute_check(pd.Timestamp("2024-01-01 13:01:05", tz="UTC"))  # same hour: once
    fund = rows(db, m.funding)
    assert len(fund) == 1 and fund[0]["rate"] == 0.0001 and fund[0]["amount"] < 0
    feed.rates = {"BTC": None}
    eng.minute_check(pd.Timestamp("2024-01-01 14:00:05", tz="UTC"))
    fund = rows(db, m.funding)
    assert len(fund) == 2 and fund[1]["rate"] is None and fund[1]["amount"] == 0
    (pos,) = eng.pf.positions.values()
    assert pos.pnl["funding_unknown_bars"] == 1
    assert t < pd.Timestamp("2024-01-01 13:00", tz="UTC")


def test_run_forever_schedule_and_stop(db):
    panel = panel_of(100)
    clock = Clock(pd.Timestamp("2024-01-01 12:14:50", tz="UTC"))
    eng, _, _ = engine(db, panel, clock, Scripted({}))
    eng.start()
    assert LiveEngine.next_tick(clock.now) == pd.Timestamp("2024-01-01 12:15:20", tz="UTC")
    assert LiveEngine.next_tick(pd.Timestamp("2024-01-01 12:15:20", tz="UTC")) == pd.Timestamp(
        "2024-01-01 12:30:20", tz="UTC")
    calls = []

    def wait(seconds):  # fake sleep: advance the clock
        clock.now = clock.now + pd.Timedelta(seconds=seconds)
        if clock.now > pd.Timestamp("2024-01-01 12:47", tz="UTC"):
            eng.stop()
        return eng._stop.is_set()

    eng._stop.wait = wait
    eng.tick = lambda now: calls.append(("tick", now))
    eng.minute_check = lambda now: calls.append(("min", now))
    eng.run_forever()
    ticks = [t for k, t in calls if k == "tick"]
    assert ticks == [pd.Timestamp(f"2024-01-01 12:{mm}:20", tz="UTC") for mm in ("15", "30", "45")]
    mins = [t for k, t in calls if k == "min"]
    assert len(mins) >= 30
    assert all((b - a) >= pd.Timedelta(seconds=60) for a, b in zip(mins, mins[1:], strict=False))
    eng.close()
    assert db.get_state("perp", "lock") is None


def test_emergency_replaces_resting_spot_exit_with_ioc(db):
    panel = panel_of(100)
    idx = panel["BTC"].index
    clock = Clock(tick_at(idx[50]))
    eng, feed, broker = engine(db, panel, clock, Scripted({("BTC", idx[50]): 1}, exits=[idx[52]]),
                               kind="spot")
    eng.start()
    eng.tick(clock.now)
    (entry,) = orders(db)
    # the resting post-only buy fills on a trade through its limit
    feed.trades_since = lambda a, since: (pd.DataFrame({
        "price": [entry["price"] * 0.999], "volume": [1e6],
        "time": [clock.now + pd.Timedelta(seconds=30)]}), since + 1)
    eng.minute_check(clock.now + pd.Timedelta(seconds=60))
    assert len(eng.pf.positions) == 1
    feed.trades_since = FakeFeed.trades_since.__get__(feed)
    for k in (51, 52):
        clock.now = tick_at(idx[k])
        eng.tick(clock.now)
    (exit_order,) = [o for o in orders(db) if o["intent"] == "close"]
    assert exit_order["tif"] == "POST_ONLY" and exit_order["status"] == "OPEN"
    (pos,) = eng.pf.positions.values()
    feed.mid_override["BTC"] = pos.entry_ref_price["BTC"] * 0.94  # -6% on <= 0.5x equity
    eng.minute_check(clock.now + pd.Timedelta(seconds=30))  # same minute as the resting exit
    got = {o["client_id"]: o for o in orders(db)}
    assert got[exit_order["client_id"]]["status"] == "CANCELED"
    urgent = [o for o in got.values() if o["intent"] == "close" and o["tif"] == "IOC"]
    assert len(urgent) == 1
    eng.minute_check(clock.now + pd.Timedelta(seconds=120))
    assert eng.pf.positions == {} and eng.trades[-1]["exit_reason"] == "emergency_stop"


def test_tick_requires_start(db):
    panel = panel_of(50)
    clock = Clock(tick_at(panel["BTC"].index[20]))
    eng, _, _ = engine(db, panel, clock, Scripted({}))
    with pytest.raises(RuntimeError, match="start"):
        eng.tick(clock.now)
