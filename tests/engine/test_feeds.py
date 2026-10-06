import json

import httpx
import numpy as np
import pandas as pd
import pytest

from luna.data.store import BarStore
from luna.engine.feeds import FeedError, KrakenFuturesLiveFeed, KrakenSpotLiveFeed
from luna.http import make_client

STEP = pd.Timedelta("15min")


def bars(start, n, price=100.0):
    idx = pd.date_range(start, periods=n, freq="15min", tz="UTC")
    close = price + np.arange(n, dtype="float64")
    return pd.DataFrame(
        {"open": close, "high": close + 1, "low": close - 1, "close": close, "volume": 1.0,
         "trades": 3, "filled": False},
        index=idx,
    )


def candle(ts, price):
    return {"time": int(pd.Timestamp(ts).timestamp()) * 1000, "open": str(price), "high": str(price + 1),
            "low": str(price - 1), "close": str(price), "volume": "2.5"}


def client_for(routes, calls=None):
    """routes: {path: callable(request) -> response json (dict) or httpx.Response}."""

    def handler(req: httpx.Request):
        if calls is not None:
            calls.append(req)
        fn = routes.get(req.url.path)
        if fn is None:
            return httpx.Response(404)
        out = fn(req)
        return out if isinstance(out, httpx.Response) else httpx.Response(200, json=out)

    return make_client(httpx.MockTransport(handler))


# --- futures -----------------------------------------------------------------------------------


def test_feed_refresh_appends_only_closed_bars(tmp_path):
    store = BarStore(tmp_path)
    store.write("kraken_futures", "BTC", bars("2026-10-06 10:00", 4))  # last stored 10:45
    now = pd.Timestamp("2026-10-06 11:31:20", tz="UTC")  # closed: 11:00, 11:15; forming: 11:30
    calls = []

    def charts(req):
        assert int(req.url.params["from"]) == int(pd.Timestamp("2026-10-06 11:00", tz="UTC").timestamp())
        # the server may include the still-forming bar even if asked for less: feed must clip it
        return {"candles": [candle("2026-10-06 11:00", 200), candle("2026-10-06 11:15", 201),
                            candle("2026-10-06 11:30", 999)], "more_candles": False}

    feed = KrakenFuturesLiveFeed(["BTC"], store, client=client_for(
        {"/api/charts/v1/trade/PF_XBTUSD/15m": charts}, calls))
    out = feed.refresh(now)

    stored = store.read("kraken_futures", "BTC")
    assert stored.index[-1] == pd.Timestamp("2026-10-06 11:15", tz="UTC")
    assert len(stored) == 6
    assert (stored.index + STEP <= now).all()
    assert out["BTC"].index[-1] == pd.Timestamp("2026-10-06 11:15", tz="UTC")
    assert out["BTC"]["close"].iloc[-1] == 201.0
    assert len(calls) == 1

    # same tick again: nothing new is closed -> no request, no store re-read (in-memory cache)
    store.read = lambda *a, **k: pytest.fail("refresh re-read the parquet file")
    out2 = feed.refresh(now)
    assert len(calls) == 1
    pd.testing.assert_frame_equal(out2["BTC"], out["BTC"])


def test_feed_refresh_rolling_buffer_and_fill_missing(tmp_path):
    store = BarStore(tmp_path)
    b = bars("2026-10-06 00:00", 20)
    store.write("kraken_futures", "ETH", b.drop(b.index[15]))  # one hole
    now = pd.Timestamp("2026-10-06 05:00:30", tz="UTC")  # last closed = 04:45 (= stored last)
    feed = KrakenFuturesLiveFeed(["ETH"], store, client=client_for({}), buffer_bars=8)
    out = feed.refresh(now)["ETH"]
    assert len(out) == 8
    assert out.index[-1] == pd.Timestamp("2026-10-06 04:45", tz="UTC")
    assert bool(out.loc[b.index[15], "filled"]) is True
    assert list(out.columns) == ["open", "high", "low", "close", "volume", "trades", "filled"]


def test_feed_refresh_bootstraps_empty_store(tmp_path):
    store = BarStore(tmp_path)
    now = pd.Timestamp("2026-10-06 11:31", tz="UTC")
    seen = {}

    def charts(req):
        seen["from"] = int(req.url.params["from"])
        seen["to"] = int(req.url.params["to"])
        return {"candles": [candle("2026-10-06 11:15", 50)], "more_candles": False}

    feed = KrakenFuturesLiveFeed(["BTC"], store, buffer_bars=4,
                                 client=client_for({"/api/charts/v1/trade/PF_XBTUSD/15m": charts}))
    out = feed.refresh(now)
    assert seen["to"] == int(pd.Timestamp("2026-10-06 11:15", tz="UTC").timestamp())
    assert seen["from"] == int(pd.Timestamp("2026-10-06 10:30", tz="UTC").timestamp())
    assert len(out["BTC"]) == 1
    assert len(store.read("kraken_futures", "BTC")) == 1


def test_feed_error_raises_feed_error(tmp_path, monkeypatch):
    monkeypatch.setattr("luna.http.time.sleep", lambda s: None)
    calls = []
    client = client_for({"/api/charts/v1/trade/PF_XBTUSD/15m": lambda r: httpx.Response(500)}, calls)
    feed = KrakenFuturesLiveFeed(["BTC"], BarStore(tmp_path), client=client)
    with pytest.raises(FeedError):
        feed.refresh(pd.Timestamp("2026-10-06 11:31", tz="UTC"))
    assert len(calls) == 4  # first try + 3 retries (luna.http.get_json defaults)
    with pytest.raises(FeedError):
        feed.mids()  # tickers route missing -> 404, not retried


def test_feed_malformed_payload_raises_feed_error(tmp_path):
    client = client_for({
        "/derivatives/api/v3/orderbook": lambda r: {"unexpected": {}},
        "/derivatives/api/v3/tickers": lambda r: {
            "tickers": [{"symbol": "PF_XBTUSD", "markPrice": 1.0, "fundingRate": "x"}]},
    })
    feed = KrakenFuturesLiveFeed(["BTC"], BarStore(tmp_path), client=client)
    with pytest.raises(FeedError):
        feed.book("BTC")
    with pytest.raises(FeedError):
        feed.funding_rates()


def test_futures_book_sorted_best_first(tmp_path):
    def ob(req):
        assert req.url.params["symbol"] == "PF_ETHUSD"
        return {"result": "success", "orderBook": {"bids": [[99, 1], [100, 2], [98, 3]],
                                                   "asks": [[102, 1], [101, 2]]}}

    feed = KrakenFuturesLiveFeed(["ETH"], BarStore(tmp_path),
                                 client=client_for({"/derivatives/api/v3/orderbook": ob}))
    bids, asks = feed.book("ETH")
    assert bids == [(100.0, 2.0), (99.0, 1.0), (98.0, 3.0)]
    assert asks == [(101.0, 2.0), (102.0, 1.0)]


def test_futures_funding_rates_relative_and_mids(tmp_path):
    tickers = {"tickers": [
        {"symbol": "PF_XBTUSD", "markPrice": 80000.0, "bid": 79999.0, "ask": 80001.0, "fundingRate": 0.8},
        {"symbol": "PF_ETHUSD", "markPrice": 3000.0},  # no funding rate, no bid/ask
        {"symbol": "PF_SOLUSD", "markPrice": 150.0, "bid": 149.9, "ask": 150.1, "fundingRate": -0.0015},
    ]}
    feed = KrakenFuturesLiveFeed(["BTC", "ETH", "SOL", "DOGE"], BarStore(tmp_path),
                                 client=client_for({"/derivatives/api/v3/tickers": lambda r: tickers}))
    rates = feed.funding_rates()
    assert rates["BTC"] == pytest.approx(0.8 / 80000.0)
    assert rates["SOL"] == pytest.approx(-0.0015 / 150.0)
    assert rates["ETH"] is None
    assert rates["DOGE"] is None
    mids = feed.mids()
    assert mids == {"BTC": pytest.approx(80000.0), "ETH": 3000.0, "SOL": pytest.approx(150.0)}


# --- spot --------------------------------------------------------------------------------------


def ohlc_row(ts, price):
    t = int(pd.Timestamp(ts).timestamp())
    p = str(price)
    return [t, p, str(price + 1), str(price - 1), p, p, "3.5", 7]


def test_spot_refresh_appends_only_new_closed_bars(tmp_path):
    store = BarStore(tmp_path)
    old = bars("2026-10-06 10:00", 4)  # 10:00..10:45, close 100..103
    store.write("kraken_spot", "BTC", old)
    now = pd.Timestamp("2026-10-06 11:30:20", tz="UTC")

    def ohlc(req):
        assert req.url.params["pair"] == "XBTUSD"
        rows = [ohlc_row("2026-10-06 10:45", 555), ohlc_row("2026-10-06 11:00", 300),
                ohlc_row("2026-10-06 11:15", 301), ohlc_row("2026-10-06 11:30", 999)]  # last = forming
        return {"error": [], "result": {"XXBTZUSD": rows, "last": 0}}

    feed = KrakenSpotLiveFeed(["BTC"], store, client=client_for({"/0/public/OHLC": ohlc}))
    out = feed.refresh(now)["BTC"]
    stored = store.read("kraken_spot", "BTC")
    assert stored.index[-1] == pd.Timestamp("2026-10-06 11:15", tz="UTC")
    assert stored.loc[pd.Timestamp("2026-10-06 10:45", tz="UTC"), "close"] == 103.0  # not overwritten
    assert len(stored) == 6
    assert out["close"].tolist()[-2:] == [300.0, 301.0]
    assert feed.funding_rates() == {}


def test_spot_mids_map_kraken_keys_and_book(tmp_path):
    def ticker(req):
        assert req.url.params["pair"] == "XBTUSD,SOLUSD"
        return {"error": [], "result": {
            "XXBTZUSD": {"a": ["80001.0", "1", "1.0"], "b": ["79999.0", "1", "1.0"], "c": ["80000.5", "0.1"]},
            "SOLUSD": {"a": ["150.2", "1", "1.0"], "b": ["150.0", "1", "1.0"], "c": ["150.1", "0.1"]},
        }}

    def depth(req):
        assert req.url.params["pair"] == "SOLUSD"
        return {"error": [], "result": {"SOLUSD": {
            "bids": [["150.0", "2.0", 1], ["149.9", "1.0", 1]],
            "asks": [["150.2", "1.5", 1], ["150.3", "4.0", 1]]}}}

    feed = KrakenSpotLiveFeed(["BTC", "SOL"], BarStore(tmp_path),
                              client=client_for({"/0/public/Ticker": ticker, "/0/public/Depth": depth}))
    assert feed.mids() == {"BTC": pytest.approx(80000.0), "SOL": pytest.approx(150.1)}
    assert feed.book("SOL") == ([(150.0, 2.0), (149.9, 1.0)], [(150.2, 1.5), (150.3, 4.0)])


def test_spot_trades_since(tmp_path):
    def trades(req):
        assert req.url.params["pair"] == "XBTUSD"
        assert req.url.params["since"] == "123"
        return {"error": [], "result": {"XXBTZUSD": [["100.5", "0.25", 1759750000.5, "b", "l", ""]],
                                        "last": "1759750000500000000"}}

    feed = KrakenSpotLiveFeed(["BTC"], BarStore(tmp_path), client=client_for({"/0/public/Trades": trades}))
    df, last = feed.trades_since("BTC", 123)
    assert last == 1759750000500000000
    assert df["price"].tolist() == [100.5]
    assert df["volume"].tolist() == [0.25]


def test_spot_kraken_error_raises_feed_error(tmp_path):
    feed = KrakenSpotLiveFeed(["BTC"], BarStore(tmp_path), client=client_for(
        {"/0/public/OHLC": lambda r: {"error": ["EGeneral:Too many requests"]}}))
    with pytest.raises(FeedError):
        feed.refresh(pd.Timestamp("2026-10-06 11:30", tz="UTC"))


# --- live smoke tests (skipped by default) -----------------------------------------------------


@pytest.mark.network
def test_network_futures_feed(tmp_path):
    feed = KrakenFuturesLiveFeed(["BTC", "ETH"], BarStore(tmp_path), buffer_bars=8)
    now = pd.Timestamp.now(tz="UTC")
    out = feed.refresh(now)
    assert (out["BTC"].index + STEP <= now).all() and len(out["BTC"]) > 0
    bids, asks = feed.book("BTC")
    assert bids[0][0] < asks[0][0]
    mids = feed.mids()
    assert bids[0][0] <= mids["BTC"] <= asks[0][0] or abs(mids["BTC"] / bids[0][0] - 1) < 0.01
    rates = feed.funding_rates()
    hist = feed.client.get("https://futures.kraken.com/derivatives/api/v4/historicalfundingrates",
                           params={"symbol": "PF_XBTUSD"}).json()["rates"][-1]["relativeFundingRate"]
    print(json.dumps({"ticker_relative": rates["BTC"], "last_historical_relative": hist}))
    assert rates["BTC"] is not None and abs(rates["BTC"]) < 0.01


@pytest.mark.network
def test_network_spot_feed(tmp_path):
    feed = KrakenSpotLiveFeed(["BTC", "DOGE"], BarStore(tmp_path))
    now = pd.Timestamp.now(tz="UTC")
    out = feed.refresh(now)
    assert (out["BTC"].index + STEP <= now).all() and len(out["BTC"]) > 0
    assert set(feed.mids()) == {"BTC", "DOGE"}
    bids, asks = feed.book("BTC")
    assert bids[0][0] < asks[0][0]
    df, last = feed.trades_since("BTC", int((now - pd.Timedelta("5min")).value))
    assert last > 0
