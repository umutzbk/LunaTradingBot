"""§1/§4/§10.1-3: universe, envelope fields, raw payload preservation, error envelopes."""

import json
from pathlib import Path

import httpx

from luna.forward import config
from luna.forward.envelope import Request, fetch, iso_ns, ns_from_iso
from luna.forward.sources import filter_tickers, fut_tickers, spot_ticker
from luna.http import make_client

FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "forward_fut_tickers.json"
T0 = 1_791_547_200_000_000_000  # 2026-10-09T12:00:00Z in ns


class Clock:
    def __init__(self, ns: int) -> None:
        self.ns = ns

    def __call__(self) -> int:
        self.ns += 1_500_000  # every reading advances 1.5 ms
        return self.ns


def _client(handler):
    return make_client(httpx.MockTransport(handler))


def test_universe_is_the_fixed_21_assets():
    assert config.UNIVERSE == (
        "ADA", "AVAX", "BNB", "BTC", "DOGE", "ENA", "ETH", "FET", "HYPE", "LINK", "MUBARAK",
        "NEAR", "PEPE", "PUMP", "SOL", "SUI", "TAO", "UNI", "XLM", "XRP", "ZEC",
    )
    assert config.PERP_SYMBOLS[0] == "PF_ADAUSD" and "PF_XBTUSD" in config.PERP_SYMBOLS
    assert "XBTUSD" in config.SPOT_PAIRS and "XDGUSD" in config.SPOT_PAIRS
    assert len(config.PERP_SYMBOLS) == len(config.SPOT_PAIRS) == 21


def test_iso_ns_round_trip():
    assert iso_ns(T0 + 123_456_789) == "2026-10-09T12:00:00.123456789Z"
    assert ns_from_iso("2026-10-09T12:00:00.123456789Z") == T0 + 123_456_789
    assert ns_from_iso("2026-10-09T11:59:59.873Z") == T0 - 127_000_000


def test_raw_payload_is_preserved_exactly():
    """Golden fixture: the envelope payload equals the parsed response, types included."""
    raw = FIXTURE.read_text(encoding="utf-8")
    client = _client(lambda r: httpx.Response(200, text=raw, headers={"content-type": "application/json"}))
    env = fetch(client, Request("spot_time", "https://x/raw", None), seq=7, clock_ns=Clock(T0))
    line = json.dumps(env)
    back = json.loads(line)["payload"]
    assert back == json.loads(raw)
    t = back["tickers"][0]
    assert isinstance(t["ask"], int) and isinstance(t["bid"], float) and t["fundingRatePrediction"] == 1e-7
    assert t["suspended"] is False and back["tickers"][2]["fundingRate"] is None


def test_envelope_fields_and_timestamps():
    client = _client(lambda r: httpx.Response(200, json={"result": {"unixtime": 1}, "error": []}))
    env = fetch(client, Request("spot_time", "https://x/Time", {"a": 1}), seq=3, clock_ns=Clock(T0))
    assert list(env) == ["source", "seq", "req_ts", "recv_ts", "latency_ms", "ok", "error", "url",
                         "params", "filtered_to_universe", "payload"]
    assert env["source"] == "spot_time" and env["seq"] == 3 and env["ok"] is True and env["error"] is None
    assert env["req_ts"] == iso_ns(T0 + 1_500_000) and env["recv_ts"] == iso_ns(T0 + 3_000_000)
    assert env["latency_ms"] == 1.5
    assert env["params"] == {"a": 1} and env["filtered_to_universe"] is False


def test_pair_seq_only_when_given():
    client = _client(lambda r: httpx.Response(200, json={"tickers": []}))
    env = fetch(client, fut_tickers()[0], seq=1, pair_seq=9, clock_ns=Clock(T0))
    assert env["pair_seq"] == 9 and list(env).index("pair_seq") == 2


def test_universe_filter_keeps_only_universe_symbols_and_never_fills():
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    out = filter_tickers(raw)
    assert [t["symbol"] for t in out["tickers"]] == ["PF_XBTUSD", "PF_ETHUSD", "PF_MUBARAKUSD"]
    assert out["tickers"][0] == raw["tickers"][0]  # rows untouched
    assert out["serverTime"] == raw["serverTime"] and out["result"] == "success"

    client = _client(lambda r: httpx.Response(200, json=raw))
    env = fetch(client, fut_tickers()[0], seq=1, pair_seq=1, clock_ns=Clock(T0))
    assert env["filtered_to_universe"] is True
    assert len(env["payload"]["tickers"]) == 3  # 18 missing universe symbols are not invented


def test_spot_ticker_requests_all_21_pairs():
    (req,) = spot_ticker()
    assert req.url == "https://api.kraken.com/0/public/Ticker"
    assert req.params == {"pair": ",".join(config.SPOT_PAIRS)}


def test_http_500_gives_an_error_envelope():
    client = _client(lambda r: httpx.Response(500, text="boom"))
    env = fetch(client, fut_tickers()[0], seq=1, pair_seq=1, clock_ns=Clock(T0), retries=1, backoff=0)
    assert env["ok"] is False and env["payload"] is None and "500" in env["error"]
    assert env["filtered_to_universe"] is True


def test_timeout_gives_an_error_envelope():
    def handler(r):
        raise httpx.ReadTimeout("slow", request=r)

    env = fetch(_client(handler), spot_ticker()[0], seq=1, pair_seq=1, clock_ns=Clock(T0), retries=0)
    assert env["ok"] is False and env["payload"] is None and "ReadTimeout" in env["error"]


def test_exchange_level_errors_are_not_ok():
    spot = _client(lambda r: httpx.Response(200, json={"error": ["EQuery:Unknown asset pair"]}))
    env = fetch(spot, spot_ticker()[0], seq=1, clock_ns=Clock(T0))
    assert env["ok"] is False and "Unknown asset pair" in env["error"] and env["payload"] is None
    fut = _client(lambda r: httpx.Response(200, json={"result": "error", "error": "apiLimitExceeded"}))
    env = fetch(fut, fut_tickers()[0], seq=1, clock_ns=Clock(T0))
    assert env["ok"] is False and "apiLimitExceeded" in env["error"]


def test_invalid_json_gives_an_error_envelope():
    env = fetch(_client(lambda r: httpx.Response(200, text="<html>")), spot_ticker()[0], seq=1,
                clock_ns=Clock(T0))
    assert env["ok"] is False and env["payload"] is None
