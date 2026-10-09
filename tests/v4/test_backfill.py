"""`luna backfill-v4` (spec §7.1): real funding (abs + rel), mark / index 15m candles and perp
analytics spreads, every series hard-cut before 2026-07-01, with a provenance json per file.
Network is a httpx.MockTransport only."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import httpx
import pandas as pd
import pytest

from luna.v2.data import HoldoutLeak
from luna.v4 import backfill as bf
from luna.v4.constants import V4_DATA_END

FIXED_NOW = pd.Timestamp("2026-10-09T13:00:00Z")


def _funding_payload():
    # three rows before the cut and two at/after it (the API always returns the full history)
    ts = ["2026-06-30T22:00:00.000Z", "2026-06-30T23:00:00.000Z", "2026-06-30T21:00:00.000Z",
          "2026-07-01T00:00:00.000Z", "2026-07-02T05:00:00.000Z"]
    return {"result": "success", "rates": [
        {"timestamp": t, "fundingRate": 0.5 + i, "relativeFundingRate": (0.5 + i) / 1e5}
        for i, t in enumerate(ts)
    ]}


def _candles(lo_s: int, hi_s: int):
    out = []
    t = lo_s - lo_s % 900
    while t <= hi_s:
        out.append({"time": t * 1000, "open": "1", "high": "2", "low": "0.5", "close": "1.5", "volume": 0})
        t += 900
    return out


class Api:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        path = request.url.path
        self.calls.append((path, params))
        if path.endswith("/historicalfundingrates"):
            return httpx.Response(200, json=_funding_payload())
        if "/api/charts/v1/mark/" in path or "/api/charts/v1/spot/" in path:
            lo, hi = int(params["from"]), int(params["to"])
            return httpx.Response(200, json={"candles": _candles(lo, min(hi, lo + 900 * 3)),
                                             "more_candles": False})
        if "/api/charts/v1/analytics/" in path and path.endswith("/spreads"):
            lo, hi = int(params["since"]), int(params["to"])
            ts = list(range(lo - lo % 3600, hi + 1, 3600))
            return httpx.Response(200, json={"result": {"timestamp": ts, "data": [0.001] * len(ts),
                                                        "more": False}, "errors": []})
        return httpx.Response(404)


def _client(api):
    return httpx.Client(transport=httpx.MockTransport(api))


def test_funding_keeps_abs_and_rel_and_cuts_before_holdout():
    df, meta = bf.fetch_funding_history(_client(Api()), "BTC")
    assert list(df.columns) == ["funding_abs", "funding_rel"]
    assert df.index.max() < V4_DATA_END
    assert len(df) == 3 and df.index.is_monotonic_increasing
    assert meta["endpoint"].endswith("/derivatives/api/v4/historicalfundingrates")
    assert meta["params"] == {"symbol": "PF_XBTUSD"}
    assert meta["rows_dropped_at_or_after_cutoff"] == 2


def test_chart_candles_request_never_reaches_cutoff():
    api = Api()
    start = pd.Timestamp("2026-06-30T23:00:00Z")
    df, meta = bf.fetch_chart_candles(_client(api), "mark", "ETH", start, V4_DATA_END)
    assert df.index.max() <= V4_DATA_END - pd.Timedelta("15min")
    assert all(int(p["to"]) < int(V4_DATA_END.timestamp()) for _, p in api.calls)
    assert api.calls[0][0] == "/api/charts/v1/mark/PF_ETHUSD/15m"
    assert meta["tick_type"] == "mark"


def test_chart_candles_refuse_an_end_after_the_cutoff():
    with pytest.raises(HoldoutLeak):
        bf.fetch_chart_candles(_client(Api()), "spot", "ETH", pd.Timestamp("2026-06-01T00:00:00Z"),
                               V4_DATA_END + pd.Timedelta("1h"))


def test_spreads_are_requested_in_chunks_below_cutoff():
    api = Api()
    df, meta = bf.fetch_spreads(_client(api), "SOL", pd.Timestamp("2026-05-01T00:00:00Z"), V4_DATA_END)
    assert df.index.max() < V4_DATA_END
    assert (df["spread_full"] == 0.001).all()
    assert len(api.calls) >= 2  # chunked
    assert all(int(p["to"]) < int(V4_DATA_END.timestamp()) for _, p in api.calls)
    assert meta["params"]["interval"] == 3600


def test_parse_spreads_from_bid_ask_lists():
    payload = {"result": {"timestamp": [0, 3600], "data": {"bid": [99.0, 10.0], "ask": [101.0, 10.1]}}}
    df = bf.parse_spreads(payload)
    assert df["spread_full"].tolist() == pytest.approx([0.02, 0.1 / 10.05])


def test_parse_spreads_unknown_shape_fails_loudly():
    with pytest.raises(ValueError):
        bf.parse_spreads({"result": {"timestamp": [0], "data": {"weird": [1]}}})


def test_backfill_writes_parquet_and_provenance_per_file(tmp_path: Path):
    api = Api()
    done = bf.backfill_v4(_client(api), tmp_path, ["BTC"], start=pd.Timestamp("2026-06-30T22:00:00Z"),
                          now=FIXED_NOW)
    assert done == ["BTC"]
    root = tmp_path / "history_v4"
    for kind in ("funding", "mark", "index", "spreads"):
        pq = root / kind / "BTC.parquet"
        prov = json.loads((root / kind / "BTC.provenance.json").read_text(encoding="utf-8"))
        assert prov["sha256"] == hashlib.sha256(pq.read_bytes()).hexdigest()
        assert prov["fetched_at"] == FIXED_NOW.isoformat()
        assert prov["cutoff"] == V4_DATA_END.isoformat()
        assert prov["endpoint"].startswith("https://futures.kraken.com/")
        assert "params" in prov and prov["rows"] == len(pd.read_parquet(pq))
        assert pd.read_parquet(pq).index.max() < V4_DATA_END


def test_backfill_refuses_a_cutoff_other_than_the_holdout_start(tmp_path: Path):
    with pytest.raises(HoldoutLeak):
        bf.backfill_v4(_client(Api()), tmp_path, ["BTC"], start=pd.Timestamp("2026-06-30T22:00:00Z"),
                       now=FIXED_NOW, cutoff=V4_DATA_END + pd.Timedelta(days=1))


def test_backfill_cli_is_registered():
    from typer.testing import CliRunner

    from luna.cli import app

    out = CliRunner().invoke(app, ["backfill-v4", "--help"], terminal_width=200)
    assert out.exit_code == 0
    assert "--assets" in out.output
