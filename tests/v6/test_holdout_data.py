"""The holdout download/load path is unreachable without the authorization; with a token (tests
only, fabricated via the guard's own path) it keeps only the holdout window and writes once."""

import httpx
import pandas as pd
import pytest

from luna.v6 import guard
from luna.v6 import holdout_data as hd


def test_every_holdout_function_refuses_without_the_token(tmp_path):
    client = httpx.Client(transport=httpx.MockTransport(lambda r: pytest.fail("no request may be made")))
    for fn in (lambda: hd.fetch_candles(client, "trade", "BTC", None), lambda: hd.fetch_funding(client, "BTC", None),
               lambda: hd.fetch_spreads(client, "BTC", None), lambda: hd.fetch_spot(client, "BTC", None),
               lambda: hd.download(client, tmp_path, ["BTC"], None), lambda: hd.load_holdout(tmp_path, tmp_path, None)):
        with pytest.raises(guard.HoldoutBlocked):
            fn()


@pytest.fixture
def token(tmp_path, monkeypatch):
    monkeypatch.setattr(guard, "marker_paths", lambda repo=None: [tmp_path / "m" / "a", tmp_path / "m" / "b"])
    monkeypatch.setattr(guard, "preflight", lambda repo=None: {"head": "test"})
    return guard.authorize_holdout(guard.approval_phrase())


def test_funding_keeps_only_the_holdout_window(token):
    rates = [{"timestamp": t, "fundingRate": 0.1, "relativeFundingRate": 1e-5} for t in
             ("2026-06-30T23:00:00Z", "2026-07-01T00:00:00Z", "2026-10-09T10:00:00Z", "2026-10-09T11:00:00Z")]
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"rates": rates})))
    df = hd.fetch_funding(client, "BTC", token)
    assert list(df.index) == [pd.Timestamp("2026-07-01T00:00Z"), pd.Timestamp("2026-10-09T10:00Z")]
    assert set(df.columns) == {"funding_abs", "funding_rel"}


def test_candles_keep_bars_closed_inside_the_window(token):
    lo = guard.HOLDOUT_START
    times = [lo - pd.Timedelta("15min"), lo, guard.HOLDOUT_END.floor("15min") - pd.Timedelta("15min"),
             guard.HOLDOUT_END.floor("15min")]
    candles = [{"time": int(t.timestamp() * 1000), "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1} for t in times]
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"candles": candles})))
    df = hd.fetch_candles(client, "mark", "BTC", token)
    assert df.index.min() == lo and df.index.max() + pd.Timedelta("15min") <= guard.HOLDOUT_END


def test_parts_are_complete_only_with_a_matching_sidecar_hash(tmp_path):
    p = tmp_path / "x.parquet"
    assert hd._complete(p) is None
    sha = hd._write_part(p, pd.DataFrame({"a": [1]}))
    assert hd._complete(p) == sha
    p.write_bytes(p.read_bytes() + b"x")  # a changed or torn part is not complete -> fetched again
    assert hd._complete(p) is None
