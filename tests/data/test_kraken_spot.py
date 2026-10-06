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


class FakeKraken:
    """Serves trades (ns, price): exclusive since, fixed page size; fails persistently from call N."""

    def __init__(self, trades, page=3, crash_on_call=None):
        self.trades, self.page, self.crash_on_call = trades, page, crash_on_call
        self.calls = []

    def __call__(self, req):
        since = int(req.url.params["since"])
        self.calls.append(since)
        if self.crash_on_call and len(self.calls) >= self.crash_on_call:
            raise httpx.ConnectError("boom")
        rows = [t for t in self.trades if t[0] > since][: self.page]
        last = rows[-1][0] if rows else since
        body = [[str(p), "0.5", ns / 1e9, "b", "l", "", i] for i, (ns, p) in enumerate(rows)]
        return httpx.Response(200, json={"error": [], "result": {"XXBTZUSD": body, "last": str(last)}})


def _mk_trades():
    b = int(BAR0.value)
    sec = 10**9
    # 9 trades over 4 bars; bar 1 spans a page boundary; one trade exactly on a bar start
    offs = [5, 100, 899, 900, 1000, 1500, 1800, 2000, 2700]
    return [(b + o * sec, 100.0 + i) for i, o in enumerate(offs)]


def _backfill(trades, cp, end, got, crash_on_call=None):
    srv = FakeKraken(trades, crash_on_call=crash_on_call)
    client = make_client(httpx.MockTransport(srv))
    n = ks.backfill_trades(client, "XBTUSD", BAR0, end, cp, got.append, sleep_s=0)
    return n, srv


def test_backfill_crash_resume_matches_clean_run(tmp_path):
    trades = _mk_trades()
    end = BAR0 + pd.Timedelta("1h")
    clean = []
    n_clean, _ = _backfill(trades, tmp_path / "clean.json", end, clean)
    expected = pd.concat(clean)
    assert n_clean == len(expected) == 4

    cp, got = tmp_path / "cp.json", []
    with pytest.raises(httpx.HTTPError):
        _backfill(trades, cp, end, got, crash_on_call=3)
    assert got, "completed bars must be emitted before the crash"
    n2, _ = _backfill(trades, cp, end, got)
    union = pd.concat(got)
    assert not union.index.duplicated().any()
    pd.testing.assert_frame_equal(union, expected)
    assert n2 > 0


def test_backfill_checkpoint_bar_aligned_and_bounded(tmp_path):
    trades = _mk_trades()
    end = BAR0 + pd.Timedelta("1h")
    cp, got = tmp_path / "cp.json", []
    with pytest.raises(httpx.HTTPError):
        _backfill(trades, cp, end, got, crash_on_call=3)
    saved = json.loads(cp.read_text())
    assert saved["since"] % (900 * 10**9) == 0 and saved["since"] <= end.value
    assert saved["pair"] == "XBTUSD" and saved["start"] == BAR0.value and saved["end"] == end.value
    assert not list(tmp_path.glob("*.tmp"))
    _, srv2 = _backfill(trades, cp, end, got)
    assert srv2.calls[0] == saved["since"] - 1  # exclusive cursor; refetches the held bar
    assert json.loads(cp.read_text())["since"] == end.value


def test_backfill_stops_at_end_and_drops_later_trades(tmp_path):
    end = BAR0 + pd.Timedelta("30min")  # excludes trades at 1800s and later
    got = []
    n, _ = _backfill(_mk_trades(), tmp_path / "c.json", end, got)
    out = pd.concat(got)
    assert n == 2 and out.index.max() == BAR0 + pd.Timedelta("15min")
    assert json.loads((tmp_path / "c.json").read_text())["since"] <= end.value


def test_backfill_mismatched_checkpoint_raises(tmp_path):
    cp = tmp_path / "cp.json"
    end = BAR0 + pd.Timedelta("1h")
    _backfill(_mk_trades(), cp, end, [])
    client = make_client(httpx.MockTransport(FakeKraken([])))
    with pytest.raises(ValueError):
        ks.backfill_trades(client, "ETHUSD", BAR0, end, cp, lambda b: None, sleep_s=0)
    with pytest.raises(ValueError):
        later = end + pd.Timedelta("15min")
        ks.backfill_trades(client, "XBTUSD", BAR0, later, cp, lambda b: None, sleep_s=0)


def test_backfill_empty_page_stops_without_bars(tmp_path):
    got = []
    n, srv = _backfill([], tmp_path / "c.json", BAR0 + pd.Timedelta("1h"), got)
    assert n == 0 and got == [] and srv.calls == [int(BAR0.value) - 1]


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


@pytest.fixture(autouse=True)
def _no_retry_sleep(monkeypatch):
    monkeypatch.setattr("luna.http.time.sleep", lambda s: None)
