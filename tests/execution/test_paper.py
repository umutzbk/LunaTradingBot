import json

import pandas as pd
import pytest
from sqlalchemy import select

from luna.config import CostConfig
from luna.engine.feeds import FeedError
from luna.execution.costs import CostModel
from luna.execution.paper import PaperBroker
from luna.store import models as m
from luna.store.db import Db
from luna.types import OrderRequest

T0 = pd.Timestamp("2026-10-06 12:00:20", tz="UTC")
MIN = pd.Timedelta("1min")
COSTS = CostModel(CostConfig(maker_fee=0.0016, taker_fee=0.0005, half_spread_bps={"default": 3},
                             slippage_bps=2))


class FakeFeed:
    def __init__(self, db=None, account=None):
        self.books = {}
        self.trades = {}  # asset -> list of (time, price, volume)
        self.since_calls = []
        self.fail = False
        self.db, self.account = db, account
        self.status_at_book = []

    def book(self, asset):
        if self.db is not None:
            self.status_at_book.append(
                {o.client_id for o in self.db.open_orders(self.account)})
        if self.fail or asset not in self.books:
            raise FeedError("boom")
        return self.books[asset]

    def trades_since(self, asset, since_ns):
        self.since_calls.append((asset, since_ns))
        if self.fail:
            raise FeedError("boom")
        rows = [r for r in self.trades.get(asset, []) if r[0].value > since_ns]
        df = pd.DataFrame({
            "price": pd.Series([r[1] for r in rows], dtype="float64"),
            "volume": pd.Series([r[2] for r in rows], dtype="float64"),
            "time": pd.to_datetime([r[0] for r in rows], utc=True),
        })
        last = max([r[0].value for r in rows], default=since_ns)
        return df, last


@pytest.fixture
def db(tmp_path):
    d = Db(f"sqlite:///{(tmp_path / 'luna.db').as_posix()}")
    yield d
    d.close()


def order(cid="o1", side="buy", qty=1.0, limit=101.0, tif="IOC", asset="BTC", ts=T0):
    return OrderRequest(client_id=cid, ts=ts, asset=asset, side=side, qty=qty, limit_price=limit,
                        tif=tif, intent="open", position_id="p1", leg_group="g1")


def order_row(db, cid):
    with db.engine.connect() as c:
        return c.execute(select(m.orders).where(m.orders.c.client_id == cid)).mappings().one()


def fill_rows(db):
    with db.engine.connect() as c:
        return c.execute(select(m.fills).order_by(m.fills.c.id)).mappings().all()


# --- IOC ---------------------------------------------------------------------------------------


def test_ioc_walks_book_vwap(db):
    feed = FakeFeed(db, "perp")
    feed.books["BTC"] = ([(99.0, 5.0)], [(100.0, 0.5), (101.0, 1.0), (102.0, 9.0)])
    br = PaperBroker("perp", COSTS, feed, db, "perp")
    br.submit(order(qty=1.0, limit=101.0))
    assert feed.status_at_book == [{"o1"}]  # persisted (NEW) before the book was walked
    fills = br.poll(T0 + MIN)
    assert len(fills) == 1
    f = fills[0]
    assert f.qty == pytest.approx(1.0)
    assert f.price == pytest.approx(100.5)
    assert f.fee == pytest.approx(1.0 * 100.5 * 0.0005)
    assert f.liquidity == "taker" and f.client_id == "o1" and f.side == "buy"
    assert order_row(db, "o1")["status"] == "FILLED"
    rows = fill_rows(db)
    assert len(rows) == 1 and rows[0]["price"] == pytest.approx(100.5)
    assert rows[0]["position_id"] == "p1"
    assert br.poll(T0 + 2 * MIN) == []  # delivered once


def test_ioc_respects_limit_partial(db):
    feed = FakeFeed()
    feed.books["ETH"] = ([(99.0, 0.3), (98.5, 1.0), (98.0, 5.0)], [(100.0, 5.0)])
    br = PaperBroker("perp", COSTS, feed, db, "perp")
    br.submit(order("s1", side="sell", qty=2.0, limit=98.5, asset="ETH"))
    [f] = br.poll(T0)
    assert f.qty == pytest.approx(1.3)
    assert f.price == pytest.approx((99.0 * 0.3 + 98.5 * 1.0) / 1.3)
    assert order_row(db, "s1")["status"] == "CANCELED"  # remainder cancelled


def test_ioc_nothing_within_limit_is_cancelled_without_fill(db):
    feed = FakeFeed()
    feed.books["BTC"] = ([(99.0, 5.0)], [(100.0, 5.0)])
    br = PaperBroker("perp", COSTS, feed, db, "perp")
    br.submit(order(limit=99.5))
    assert br.poll(T0) == []
    assert order_row(db, "o1")["status"] == "CANCELED"
    assert fill_rows(db) == []


def test_ioc_feed_error_rejects_order(db):
    feed = FakeFeed()
    feed.fail = True
    br = PaperBroker("perp", COSTS, feed, db, "perp")
    br.submit(order())
    assert br.poll(T0) == []
    assert order_row(db, "o1")["status"] == "REJECTED"


def test_duplicate_client_id_not_resubmitted(db):
    feed = FakeFeed()
    feed.books["BTC"] = ([(99.0, 5.0)], [(100.0, 5.0)])
    br = PaperBroker("perp", COSTS, feed, db, "perp")
    br.submit(order(qty=1.0))
    br2 = PaperBroker("perp", COSTS, feed, db, "perp")  # e.g. after a restart
    br2.submit(order(qty=1.0))
    br.submit(order(qty=1.0))
    assert len(br.poll(T0)) == 1
    assert br2.poll(T0) == []
    assert len(fill_rows(db)) == 1


def test_perp_post_only_refused(db):
    br = PaperBroker("perp", COSTS, FakeFeed(), db, "perp")
    with pytest.raises(ValueError):
        br.submit(order(tif="POST_ONLY"))
    assert db.order_status("perp", "o1") is None


# --- POST_ONLY (spot) ----------------------------------------------------------------------------


def test_post_only_fills_on_trade_through_and_cancels_after_15m(db):
    feed = FakeFeed()
    feed.books["BTC"] = ([(100.0, 1.0)], [(100.2, 1.0)])
    br = PaperBroker("spot", COSTS, feed, db, "spot")
    br.submit(order(tif="POST_ONLY", qty=1.0, limit=100.0))
    assert order_row(db, "o1")["status"] == "OPEN"
    assert feed.since_calls == []

    feed.trades["BTC"] = [
        (T0 - MIN, 90.0, 5.0),                # before submission: ignored
        (T0 + 1 * MIN, 100.0, 3.0),           # at the limit, not through: no fill
        (T0 + 2 * MIN, 99.9, 0.4),            # through: fills 0.4 at the limit
    ]
    [f] = br.poll(T0 + 3 * MIN)
    assert f.qty == pytest.approx(0.4) and f.price == 100.0
    assert f.liquidity == "maker" and f.fee == pytest.approx(0.4 * 100.0 * 0.0016)
    assert f.ts == T0 + 2 * MIN
    assert order_row(db, "o1")["status"] == "PARTIAL"
    assert feed.since_calls[0] == ("BTC", T0.value - 1)

    assert br.poll(T0 + 4 * MIN) == []  # no new trades
    feed.trades["BTC"].append((T0 + 15 * MIN, 50.0, 10.0))  # after expiry: ignored
    assert br.poll(T0 + 15 * MIN + MIN) == []
    assert order_row(db, "o1")["status"] == "CANCELED"
    assert br.poll(T0 + 20 * MIN) == []
    assert len(fill_rows(db)) == 1


def test_post_only_sell_fills_fully_across_trades(db):
    feed = FakeFeed()
    br = PaperBroker("spot", COSTS, feed, db, "spot")
    br.submit(order("s1", side="sell", tif="POST_ONLY", qty=1.0, limit=100.0))
    feed.trades["BTC"] = [(T0 + MIN, 100.1, 0.7), (T0 + 2 * MIN, 99.0, 9.0), (T0 + 3 * MIN, 100.5, 0.6)]
    [f] = br.poll(T0 + 4 * MIN)
    assert f.qty == pytest.approx(1.0) and f.price == 100.0 and f.side == "sell"
    assert order_row(db, "s1")["status"] == "FILLED"


def test_post_only_reprices_every_5m_at_most_twice(db):
    feed = FakeFeed()
    feed.books["BTC"] = ([(100.0, 1.0)], [(100.2, 1.0)])
    br = PaperBroker("spot", COSTS, feed, db, "spot")
    br.submit(order(tif="POST_ONLY", qty=1.0, limit=99.0))
    br.poll(T0 + 4 * MIN)
    assert order_row(db, "o1")["price"] == 99.0
    feed.books["BTC"] = ([(100.5, 1.0)], [(100.7, 1.0)])
    br.poll(T0 + 5 * MIN)
    assert order_row(db, "o1")["price"] == 100.5  # repriced to best bid
    feed.books["BTC"] = ([(101.0, 1.0)], [(101.2, 1.0)])
    br.poll(T0 + 6 * MIN)
    assert order_row(db, "o1")["price"] == 100.5  # next reprice only at +10m
    br.poll(T0 + 10 * MIN)
    assert order_row(db, "o1")["price"] == 101.0
    feed.books["BTC"] = ([(102.0, 1.0)], [(102.2, 1.0)])
    br.poll(T0 + 14 * MIN)
    assert order_row(db, "o1")["price"] == 101.0  # max 2 reprices
    # a trade through the repriced limit fills at the new limit
    feed.trades["BTC"] = [(T0 + 14 * MIN + pd.Timedelta("10s"), 100.9, 2.0)]
    [f] = br.poll(T0 + 14 * MIN + pd.Timedelta("30s"))
    assert f.price == 101.0 and f.qty == pytest.approx(1.0)


def test_post_only_survives_feed_errors_and_still_expires(db):
    feed = FakeFeed()
    feed.fail = True
    br = PaperBroker("spot", COSTS, feed, db, "spot")
    br.submit(order(tif="POST_ONLY"))
    assert br.poll(T0 + 6 * MIN) == []
    assert order_row(db, "o1")["status"] == "OPEN"
    assert br.poll(T0 + 15 * MIN) == []
    assert order_row(db, "o1")["status"] == "CANCELED"


def test_cancel_resting_order(db):
    br = PaperBroker("spot", COSTS, FakeFeed(), db, "spot")
    br.submit(order(tif="POST_ONLY"))
    br.cancel("o1")
    assert order_row(db, "o1")["status"] == "CANCELED"
    assert br.poll(T0 + MIN) == []


# --- book snapshots ----------------------------------------------------------------------------


def test_snapshot_books_writes_top10(db):
    feed = FakeFeed()
    feed.books["BTC"] = ([(100.0 - i, 1.0) for i in range(15)], [(101.0 + i, 2.0) for i in range(12)])
    feed.books["ETH"] = ([(10.0, 1.0)], [(10.1, 1.0)])
    br = PaperBroker("perp", COSTS, feed, db, "perp")
    br.snapshot_books(["BTC", "ETH", "MISSING"], now=T0)
    with db.engine.connect() as c:
        rows = c.execute(select(m.book_snapshots).order_by(m.book_snapshots.c.id)).mappings().all()
    assert [r["symbol"] for r in rows] == ["BTC", "ETH"]
    bids = json.loads(rows[0]["bids"])
    asks = json.loads(rows[0]["asks"])
    assert len(bids) == 10 and bids[0] == [100.0, 1.0]
    assert len(asks) == 10 and asks[0] == [101.0, 2.0]
    assert rows[0]["ts"] == m.ts_to_str(T0)
