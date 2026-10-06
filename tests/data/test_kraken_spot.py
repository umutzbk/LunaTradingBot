import json

import httpx
import pandas as pd
import pytest

from luna.data import kraken_spot as ks
from luna.data.store import BAR_COLUMNS
from luna.http import make_client

T0 = 1700000100.0  # 2023-11-14 22:15:00 UTC is 1700000100
BAR0 = pd.Timestamp(int(T0) // 900 * 900, unit="s", tz="UTC")


def trades_df(rows):
    return pd.DataFrame(
        {
            "price": [r[0] for r in rows],
            "volume": [r[1] for r in rows],
            "time": [pd.Timestamp(r[2], unit="s", tz="UTC") for r in rows],
        }
    )


def test_aggregate_trades_ohlcv():
    b = BAR0.timestamp()
    df = trades_df(
        [
            (101.0, 1.0, b + 10),
            (105.0, 2.0, b + 20),
            (99.0, 0.5, b + 30),
            (100.0, 1.5, b + 899),
            (110.0, 1.0, b + 900 + 5),
            (108.0, 3.0, b + 900 + 50),
        ]
    ).sample(frac=1, random_state=1)  # order must not matter
    out = ks.aggregate_trades(df)
    assert list(out.columns) == BAR_COLUMNS
    assert list(out.index) == [BAR0, BAR0 + pd.Timedelta("15min")]
    r0, r1 = out.iloc[0], out.iloc[1]
    assert (r0.open, r0.high, r0.low, r0.close) == (101.0, 105.0, 99.0, 100.0)
    assert r0.volume == 5.0 and r0.trades == 4 and not r0.filled
    assert (r1.open, r1.high, r1.low, r1.close) == (110.0, 110.0, 108.0, 108.0)
    assert r1.volume == 4.0 and r1.trades == 2


def test_aggregate_trades_empty():
    out = ks.aggregate_trades(trades_df([]))
    assert out.empty and list(out.columns) == BAR_COLUMNS


def _trade_row(price, t, i):
    return [str(price), "0.5", t, "b", "l", "", i]


def test_backfill_resumes_from_checkpoint(tmp_path):
    b = BAR0.timestamp()
    cp = tmp_path / "cp.json"
    cp.write_text(json.dumps({"since": 777}))
    pages = {
        777: ([_trade_row(100, b + 1, 1), _trade_row(101, b + 2, 2)], 888),
        888: ([_trade_row(102, b + 3, 3), _trade_row(103, b + 5000, 4)], 999),
    }
    seen, cps = [], []

    def handler(req):
        since = int(req.url.params["since"])
        seen.append(since)
        if seen[1:]:
            cps.append(json.loads(cp.read_text())["since"])
        rows, last = pages[since]
        return httpx.Response(200, json={"error": [], "result": {"XXBTZUSD": rows, "last": str(last)}})

    client = make_client(httpx.MockTransport(handler))
    end = BAR0 + pd.Timedelta("1h")
    out = ks.backfill_trades(client, "XBTUSD", BAR0, end, cp, sleep_s=0)
    assert seen == [777, 888]
    assert cps == [888]  # checkpoint updated after first page
    assert json.loads(cp.read_text()) == {"since": 999}
    assert out.iloc[0].trades == 3 and out.iloc[0].close == 102.0


def test_backfill_start_without_checkpoint_and_no_progress(tmp_path):
    seen = []

    def handler(req):
        seen.append(int(req.url.params["since"]))
        return httpx.Response(200, json={"error": [], "result": {"XXBTZUSD": [], "last": "5"}})

    client = make_client(httpx.MockTransport(handler))
    end = BAR0 + pd.Timedelta("1h")
    out = ks.backfill_trades(client, "XBTUSD", BAR0, end, tmp_path / "c.json", sleep_s=0)
    assert seen == [int(BAR0.value)]
    assert out.empty


def test_fetch_trades_page_error_raises():
    client = make_client(
        httpx.MockTransport(lambda r: httpx.Response(200, json={"error": ["EGeneral:Invalid arguments"]}))
    )
    with pytest.raises(RuntimeError):
        ks.fetch_trades_page(client, "XBTUSD", 0)


def test_fetch_ohlc_drops_open_bar():
    rows = [
        [1700000100 // 900 * 900 + 900 * i, "1", "2", "0.5", "1.5", "1.2", "3.0", 7] for i in range(3)
    ]

    def handler(req):
        assert req.url.params["interval"] == "15"
        return httpx.Response(200, json={"error": [], "result": {"XXBTZUSD": rows, "last": 1}})

    out = ks.fetch_ohlc(make_client(httpx.MockTransport(handler)), "XBTUSD")
    assert len(out) == 2
    assert list(out.columns) == BAR_COLUMNS
    assert out.index[0] == pd.Timestamp(rows[0][0], unit="s", tz="UTC")
    assert out.iloc[0].close == 1.5 and out.iloc[0].trades == 7 and not out.iloc[0].filled


def test_fetch_depth_parses():
    res = {
        "XXBTZUSD": {
            "asks": [["100.5", "1.0", 1], ["100.6", "2.0", 2]],
            "bids": [["100.0", "3.0", 1], ["99.9", "4.0", 2]],
        }
    }
    body = {"error": [], "result": res}
    client = make_client(httpx.MockTransport(lambda r: httpx.Response(200, json=body)))
    bids, asks = ks.fetch_depth(client, "XBTUSD", count=2)
    assert bids == [(100.0, 3.0), (99.9, 4.0)]
    assert asks == [(100.5, 1.0), (100.6, 2.0)]
