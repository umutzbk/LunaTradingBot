import json
from pathlib import Path

import httpx
import pandas as pd
import pytest

from luna.config import CostConfig
from luna.data import kraken_futures as kf
from luna.http import get_json, make_client
from luna.instruments import asset_from_perp, perp_symbol, spot_pair

FIX = Path(__file__).resolve().parent.parent / "fixtures"
COSTS = CostConfig(maker_fee=0.0002, taker_fee=0.0005, half_spread_bps={"default": 3}, slippage_bps=2)


def fx(name):
    return json.loads((FIX / name).read_text())


def test_symbol_mapping():
    assert perp_symbol("BTC") == "PF_XBTUSD"
    assert perp_symbol("ETH") == "PF_ETHUSD"
    assert spot_pair("BTC") == "XBTUSD"
    assert spot_pair("SOL") == "SOLUSD"
    assert asset_from_perp("PF_ETHUSD") == "ETH"
    assert asset_from_perp("PF_XBTUSD") == "BTC"


def test_get_json_retries_on_429_then_succeeds():
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(429) if len(calls) < 3 else httpx.Response(200, json={"ok": 1})

    client = make_client(httpx.MockTransport(handler))
    assert get_json(client, "https://x.test/a", backoff=0) == {"ok": 1}
    assert len(calls) == 3


def test_get_json_raises_after_retries():
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(503)

    client = make_client(httpx.MockTransport(handler))
    with pytest.raises(httpx.HTTPError):
        get_json(client, "https://x.test/a", retries=2, backoff=0)
    assert len(calls) == 3


def test_get_json_does_not_retry_client_errors():
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(404)

    client = make_client(httpx.MockTransport(handler))
    with pytest.raises(httpx.HTTPError):
        get_json(client, "https://x.test/a", backoff=0)
    assert len(calls) == 1


def test_get_json_retries_timeouts():
    calls = []

    def handler(req):
        calls.append(req)
        if len(calls) == 1:
            raise httpx.ReadTimeout("slow", request=req)
        return httpx.Response(200, json={"ok": 2})

    client = make_client(httpx.MockTransport(handler))
    assert get_json(client, "https://x.test/a", backoff=0) == {"ok": 2}
    assert len(calls) == 2


def test_fetch_candles_paginates_until_no_more():
    pages = [fx("kf_candles_p1.json"), fx("kf_candles_p2.json")]
    seen = []

    def handler(req):
        seen.append(dict(req.url.params))
        assert req.url.path == "/api/charts/v1/trade/PF_ETHUSD/15m"
        return httpx.Response(200, json=pages[len(seen) - 1])

    client = make_client(httpx.MockTransport(handler))
    start, end = pd.Timestamp(1700000100, unit="s", tz="UTC"), pd.Timestamp(1700002800, unit="s", tz="UTC")
    df = kf.fetch_candles(client, "ETH", start, end)
    assert len(seen) == 2
    assert seen[0]["from"] == "1700000100" and seen[0]["to"] == "1700002800"
    assert seen[1]["from"] == str(1700001000 + 900)
    assert list(df.columns) == ["open", "high", "low", "close", "volume", "trades", "filled"]
    assert len(df) == 4 and df.index.is_unique and df.index.is_monotonic_increasing
    assert str(df.index.tz) == "UTC"
    assert df.index[0] == start and df.index[-1] == end
    assert df["close"].iloc[0] == 1982.0 and df["volume"].iloc[0] == 97.502
    assert (df["trades"] == 0).all() and not df["filled"].any()
    assert df["trades"].dtype == "int64" and df["filled"].dtype == bool


def test_fetch_candles_empty():
    client = make_client(
        httpx.MockTransport(lambda r: httpx.Response(200, json={"candles": [], "more_candles": False}))
    )
    start, end = pd.Timestamp("2024-01-01", tz="UTC"), pd.Timestamp("2024-01-02", tz="UTC")
    df = kf.fetch_candles(client, "BTC", start, end)
    assert df.empty and list(df.columns)[-1] == "filled"


def test_fetch_funding_parses_relative_rate():
    def handler(req):
        assert req.url.params["symbol"] == "PF_ETHUSD"
        return httpx.Response(200, json=fx("kf_funding.json"))

    df = kf.fetch_funding(make_client(httpx.MockTransport(handler)), "ETH")
    assert list(df.columns) == ["rate"]
    assert str(df.index.tz) == "UTC"
    assert df.index[0] == pd.Timestamp("2025-10-01T08:00:00Z")
    assert df.index.to_series().diff().dropna().eq(pd.Timedelta("1h")).all()
    assert df["rate"].tolist() == pytest.approx([6.01e-06, -2.7e-06, 0.0])


def test_list_perps_builds_instruments():
    def handler(req):
        if req.url.path.endswith("/instruments"):
            return httpx.Response(200, json=fx("kf_instruments.json"))
        assert req.url.path.endswith("/tickers")
        return httpx.Response(200, json=fx("kf_tickers.json"))

    out = kf.list_perps(make_client(httpx.MockTransport(handler)), COSTS)
    by = {i.asset: (i, v) for i, v in out}
    assert set(by) == {"BTC", "ETH"}  # FI_ excluded, ticker without volumeQuote skipped
    btc, vol = by["BTC"]
    assert vol == 375300000.0
    assert btc.symbol == "PF_XBTUSD" and btc.venue == "kraken_futures" and btc.kind == "perp"
    assert btc.min_qty == pytest.approx(1e-4) and btc.qty_step == pytest.approx(1e-4)
    assert btc.price_tick == 1 and btc.min_notional == 0
    assert btc.initial_margin == 0.01 and btc.maintenance_margin == 0.005
    assert btc.maker_fee == 0.0002 and btc.taker_fee == 0.0005
    assert by["ETH"][0].min_qty == pytest.approx(1e-3)


@pytest.mark.network
def test_live_candles_smoke():
    client = make_client()
    end = pd.Timestamp.now(tz="UTC").floor("15min") - pd.Timedelta("15min")
    df = kf.fetch_candles(client, "BTC", end - pd.Timedelta("1D") + pd.Timedelta("15min"), end)
    assert 90 <= len(df) <= 97
